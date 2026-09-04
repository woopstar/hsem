"""Low-level outbound OCPP 1.6 command senders.

Originally extracted from :mod:`ocpp_server` to satisfy the repository's
30 KB / 1000-line file limit; these methods mix into
:class:`~ocpp_server.OCPPServer`, so ``self`` and every attribute
reference (``_last_sent_target``, ``_last_sent_current_a``,
``_last_remote_start_attempt``, ``_last_remote_stop_attempt``,
``_flap_state``, ``_chargers``) resolve there. Also registers outbound
:data:`_TRACKED_RESPONSE_ACTIONS` message IDs on the session (issue #906)
so the matching CALLRESULT's ``status`` can be recorded once it arrives,
hosts :meth:`OCPPCommandsMixin._notify_significant_event` (issue #908),
and — moved here in the same size-limit rebalance — the retry-pacing
helpers, anti-flap reset, stall diagnostics, and the public
``send_set_charging_profile``/``send_remote_stop`` bypass API, which were
pushing :mod:`ocpp_server` back over the 30 KB limit.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from custom_components.hsem.models.ocpp_session import ChargerSession

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OCPP 1.6 JSON message type indicators (per OCPP-J 1.6 §4.2)
# ---------------------------------------------------------------------------
_CALL = 2  # Client → Server request (expects CALLRESULT or CALLERROR)
_CALLRESULT = 3  # Server → Client response

# OCPP 1.6 requires a non-empty idTag on RemoteStartTransaction. HSEM has no
# per-user RFID/app identity concept, so every session it authorizes uses the
# same fixed tag (issue #892).
_REMOTE_START_ID_TAG = "HSEM"

# Outbound actions whose CALLRESULT status HSEM tracks on the session (issue
# #906). HSEM previously logged every CALLRESULT at debug level without
# reading its ``status`` field, so a charger silently rejecting a command
# (e.g. "Rejected"/"NotSupported") was indistinguishable from acceptance —
# the diagnostic sensor's "requested current" only ever reflected what was
# *sent*, never what was actually applied.
_TRACKED_RESPONSE_ACTIONS = frozenset(
    {"RemoteStartTransaction", "SetChargingProfile", "RemoteStopTransaction"}
)

# Minimum seconds between RemoteStartTransaction retries while a session
# still hasn't confirmed a transaction (issue #892). Rejected, dropped, or
# unanswered start requests are retried on this cadence rather than only
# once.
_REMOTE_START_RETRY_INTERVAL_S = 60

# Minimum seconds between RemoteStopTransaction retries while a transaction
# stays open despite an attempted stop (issue #906). Mirrors
# _REMOTE_START_RETRY_INTERVAL_S: a rejected, dropped, or unanswered stop
# request is retried on this cadence instead of being assumed successful
# the moment the message is written to the socket.
_REMOTE_STOP_RETRY_INTERVAL_S = 60

# Minimum seconds between SetChargingProfile retries after a "Rejected"/
# "NotSupported" CALLRESULT (issue #906).
_PROFILE_RETRY_INTERVAL_S = 60

# StatusNotification values that indicate the charge *point* — not the EV —
# is withholding current (issue #894). "SuspendedEV" is deliberately
# excluded: it means the EV itself decided to pause (e.g. battery full,
# car-side scheduled charging), which is normal and must never be flagged.
_STALL_STATUSES = frozenset({"SuspendedEVSE", "Faulted", "Unavailable"})

# Minimum time a charger must stay in one of _STALL_STATUSES with an open
# transaction before it's considered stalled (issue #894). Long enough to
# not flag a transient flap (e.g. a few seconds in "SuspendedEVSE" before
# returning to "Charging"), short enough to be a useful diagnostic.
CHARGER_STALL_THRESHOLD_S = 300


def charger_appears_stalled(
    session: ChargerSession,
    now: datetime,
    threshold_s: float = CHARGER_STALL_THRESHOLD_S,
) -> bool:
    """Return whether *session* looks like a silently stalled charge (issue #894).

    ``True`` only when all of the following hold:

    - ``session.transaction_id`` is not ``None`` (a transaction is open —
      HSEM believes it authorized and profiled a charge).
    - ``session.status`` is one of ``"SuspendedEVSE"``, ``"Faulted"``, or
      ``"Unavailable"`` — a charge-point-side problem, not an EV-decided
      pause (``"SuspendedEV"`` is never flagged).
    - ``session.status_changed_at`` is set and older than *threshold_s*.

    Pure and diagnostics-only — never triggers a corrective OCPP call.

    Args:
        session: The charger session to evaluate.
        now: Current timestamp (injected for testability).
        threshold_s: Minimum seconds the status must have been unchanged.

    Returns:
        ``True`` if the session appears stalled.
    """
    if session.transaction_id is None:
        return False
    if session.status not in _STALL_STATUSES:
        return False
    if session.status_changed_at is None:
        return False
    elapsed = (now - session.status_changed_at).total_seconds()
    return elapsed >= threshold_s


class OCPPCommandsMixin:
    """Low-level senders for OCPP commands HSEM issues to a charger."""

    # Declared (not assigned) so mypy uses OCPPServer.__init__'s types
    # rather than inferring a narrower type from the assignments below.
    _last_remote_start_attempt: datetime | None
    _last_remote_stop_attempt: datetime | None
    _last_profile_retry_attempt: datetime | None
    _last_sent_target: float
    _last_sent_current_a: int
    _on_significant_event: Callable[[], Coroutine[Any, Any, None]] | None
    _flap_state: str
    _target_entered_at: datetime | None
    _zero_entered_at: datetime | None
    _target_power_w: float
    _stalled: bool
    _stall_logged: bool
    _chargers: dict[str, ChargerSession]

    async def _notify_significant_event(self) -> None:
        """Trigger a debounced coordinator refresh after a significant event.

        "Significant" means a state transition worth reflecting in HA
        promptly: a charger connecting/disconnecting, a
        ``StatusNotification`` status change, or a confirmed
        ``StartTransaction``/``StopTransaction`` (issue #908). Deliberately
        NOT called for ``MeterValues``/``Heartbeat``/``Authorize``, which
        arrive far more often and carry no state-transition information
        the planner needs faster than its normal cadence.

        Without this, a live ``ChargerSession`` mutation (e.g.
        ``session.status`` changing) would sit unreflected in
        ``sensor.hsem_ocpp_charger_status`` until the coordinator's next
        scheduled cycle — up to the full ``hsem_update_interval`` (default
        5 minutes) later. Cheap to await inline from the WebSocket message
        loop: the callback only manages debounce/task bookkeeping and
        returns immediately without blocking on the actual cycle.
        """
        if self._on_significant_event is not None:
            await self._on_significant_event()

    async def _send_response(
        self, session: ChargerSession, msg_id: str, payload: dict
    ) -> None:
        """Send a CALLRESULT (type 3) message back to the charger.

        Args:
            session: The charger session.
            msg_id: The original message ID being answered.
            payload: The response payload.
        """
        try:
            msg = json.dumps([_CALLRESULT, msg_id, payload])
            await session.websocket.send_str(msg)
        except Exception:
            _LOGGER.exception(
                "Failed to send OCPP response to charger %s", session.cpid
            )

    async def _send_call(
        self, session: ChargerSession, action: str, payload: dict
    ) -> bool:
        """Send a CALL (type 2) message to the charger.

        For actions in :data:`_TRACKED_RESPONSE_ACTIONS`, registers the
        message ID so the eventual CALLRESULT can be matched back to this
        action and its ``status`` recorded on
        :attr:`ChargerSession.last_call_status` (issue #906). Only the most
        recent pending call per action is kept — an earlier attempt's
        response (if it ever arrives) is no longer meaningful once a retry
        has been sent, and dropping it keeps the dict from growing across
        repeated retries.

        Args:
            session: The charger session.
            action: OCPP action name (e.g. "SetChargingProfile").
            payload: The message payload.

        Returns:
            ``True`` if the message was written to the socket, ``False``
            on any failure (issue #892) — callers must not update
            bookkeeping or commit an anti-flap state transition as if the
            charger received a command that was never actually sent.
        """
        try:
            msg_id = f"hsem-{datetime.now(UTC).timestamp()}"
            msg = json.dumps([_CALL, msg_id, action, payload])
            await session.websocket.send_str(msg)
            _LOGGER.debug(
                "OCPP CALL to %s (id=%s, action=%s): %s",
                session.cpid,
                msg_id,
                action,
                payload,
            )
            if action in _TRACKED_RESPONSE_ACTIONS:
                stale = [
                    pending_id
                    for pending_id, pending_action in session.pending_calls.items()
                    if pending_action == action
                ]
                for pending_id in stale:
                    del session.pending_calls[pending_id]
                session.pending_calls[msg_id] = action
            return True
        except Exception:
            _LOGGER.exception(
                "Failed to send OCPP call '%s' to charger %s",
                action,
                session.cpid,
            )
            return False

    async def _send_remote_start(
        self, session: ChargerSession, *, now: datetime | None = None
    ) -> bool:
        """Send a ``RemoteStartTransaction`` request.

        A ``SetChargingProfile`` alone only configures a ceiling for
        whichever transaction is active — it does not authorize or start
        one. Without an explicit start signal, a charger that requires
        central-system authorization (rather than free-vending on
        plug-in) sits in ``SuspendedEVSE`` indefinitely (issue #892).
        Callers must only invoke this when
        :attr:`ChargerSession.transaction_id` is ``None``, so an
        already-active transaction is never re-authorized. Records the
        attempt timestamp so :meth:`_remote_start_due` can pace retries,
        regardless of whether the send itself succeeds.

        Args:
            session: The charger session.
            now: Current timestamp (injected for testability).

        Returns:
            ``True`` if the message was written to the socket.
        """
        self._last_remote_start_attempt = now if now is not None else datetime.now(UTC)
        payload = {"idTag": _REMOTE_START_ID_TAG}
        sent = await self._send_call(session, "RemoteStartTransaction", payload)
        if sent:
            _LOGGER.debug(
                "Sent RemoteStartTransaction to %s (idTag=%s)",
                session.cpid,
                _REMOTE_START_ID_TAG,
            )
        return sent

    async def _send_set_charging_profile(
        self, session: ChargerSession, max_power_w: int, max_current_a: int = 16
    ) -> bool:
        """Send a ``SetChargingProfile`` request.

        Builds a TxDefaultProfile that limits charging to *max_current_a*
        amps, which at 230 V nominally equals *max_power_w*.

        Args:
            session: The charger session.
            max_power_w: Maximum charging power in watts.
            max_current_a: Maximum current in amperes.

        Returns:
            ``True`` if the message was written to the socket. Bookkeeping
            (:attr:`_last_sent_target`, :attr:`_last_sent_current_a`) is
            only updated on success (issue #892) — a failed send must not
            be remembered as the charger's current ceiling, or the
            material-change dedup filter would wrongly suppress a rightful
            retry.
        """
        # OCPP 1.6 ChargingProfile structure. chargingProfileKind MUST be
        # "Absolute" (or "Recurring") here, never "Relative" — per OCPP 1.6
        # §3.11 (ChargingProfileKindType), "Relative" is only valid on a
        # profile with purpose "TxProfile", where the schedule is anchored
        # to that transaction's own start time. This profile's purpose is
        # "TxDefaultProfile" (transaction-agnostic — it must apply to
        # whichever transaction becomes active, since at send time no
        # transaction/transactionId may exist yet), so there is no
        # transaction start to be relative to. Sending "Relative" here is a
        # spec-invalid combination (issue #920 follow-up): some chargers
        # accept the message (JSON-schema valid) but then never actually
        # apply the semantically-undefined schedule, which looked from the
        # outside like SetChargingProfile silently not taking effect despite
        # replying "Accepted". Omitting `startSchedule` under "Absolute"
        # means "effective immediately", which is what HSEM wants here.
        charging_profile = {
            "chargingProfileId": 1,
            "stackLevel": 0,
            "chargingProfilePurpose": "TxDefaultProfile",
            "chargingProfileKind": "Absolute",
            "chargingSchedule": {
                "chargingRateUnit": "A",
                "chargingSchedulePeriod": [
                    {
                        "startPeriod": 0,
                        "limit": max_current_a,
                    }
                ],
            },
        }

        payload = {
            "connectorId": 1,
            "csChargingProfiles": charging_profile,
        }

        sent = await self._send_call(session, "SetChargingProfile", payload)
        if sent:
            self._last_sent_target = float(max_power_w)
            self._last_sent_current_a = max_current_a
            _LOGGER.debug(
                "Sent SetChargingProfile to %s: max %d A (~%d W)",
                session.cpid,
                max_current_a,
                max_power_w,
            )
        return sent

    async def _send_remote_stop(
        self, session: ChargerSession, *, now: datetime | None = None
    ) -> bool:
        """Send a ``RemoteStopTransaction`` request.

        ``transactionId`` is a *mandatory* field on OCPP 1.6's
        ``RemoteStopTransaction.req`` — it is not optional. When the
        session has no active transaction (e.g. the anti-flap target
        flipped back to zero while still in the ``"starting"`` state,
        before the start window ever fired a ``RemoteStartTransaction``),
        there is nothing to stop: sending a payload without
        ``transactionId`` would violate the OCPP schema and most chargers
        reject or ignore it, so skip the call entirely instead (issue
        #892).

        Records the attempt timestamp (issue #906) so
        :meth:`_remote_stop_due` can pace retries while the charger hasn't
        yet confirmed the stop via its own ``StopTransaction`` call —
        mirroring how :meth:`_send_remote_start` paces start retries
        against :attr:`ChargerSession.transaction_id`.

        Args:
            session: The charger session.
            now: Current timestamp (injected for testability).

        Returns:
            ``True`` if the message was written to the socket, or if there
            was no active transaction (nothing to stop counts as success).
        """
        self._last_remote_stop_attempt = now if now is not None else datetime.now(UTC)
        self._last_sent_target = -1.0
        self._last_sent_current_a = -1
        if session.transaction_id is None:
            _LOGGER.debug(
                "OCPP %s has no active transaction — skipping "
                "RemoteStopTransaction (nothing to stop)",
                session.cpid,
            )
            return True
        payload = {"transactionId": session.transaction_id}
        sent = await self._send_call(session, "RemoteStopTransaction", payload)
        if not sent:
            return False
        _LOGGER.debug(
            "Sent RemoteStopTransaction to %s (tx=%s)",
            session.cpid,
            session.transaction_id,
        )
        return True

    def _remote_start_due(self, now: datetime) -> bool:
        """Return whether enough time has passed to retry RemoteStartTransaction.

        Args:
            now: Current timestamp.
        """
        if self._last_remote_start_attempt is None:
            return True
        elapsed = (now - self._last_remote_start_attempt).total_seconds()
        return elapsed >= _REMOTE_START_RETRY_INTERVAL_S

    def _remote_stop_due(self, now: datetime) -> bool:
        """Return whether enough time has passed to retry RemoteStopTransaction.

        Mirrors :meth:`_remote_start_due` (issue #906).

        Args:
            now: Current timestamp.
        """
        if self._last_remote_stop_attempt is None:
            return True
        elapsed = (now - self._last_remote_stop_attempt).total_seconds()
        return elapsed >= _REMOTE_STOP_RETRY_INTERVAL_S

    def _profile_retry_due(self, now: datetime) -> bool:
        """Return whether enough time has passed to retry a rejected profile.

        Paces :meth:`~ocpp_server.OCPPServer.update_charge_target`'s resend
        of ``SetChargingProfile`` after a "Rejected"/"NotSupported"
        CALLRESULT (issue #906).

        Args:
            now: Current timestamp.
        """
        if self._last_profile_retry_attempt is None:
            return True
        elapsed = (now - self._last_profile_retry_attempt).total_seconds()
        return elapsed >= _PROFILE_RETRY_INTERVAL_S

    def _reset_anti_flap_state(self) -> None:
        """Reset the anti-flap state machine to a clean idle state.

        Called when a charger disconnects (issue #892): the state machine
        assumes it is talking to one continuously-connected charger, so
        stale start/stop timers or a stale "charging" belief must not
        survive into a fresh connection — a reconnect goes through the
        normal start window again rather than resuming as if nothing
        happened.
        """
        self._flap_state = "idle"
        self._target_entered_at = None
        self._zero_entered_at = None
        self._target_power_w = 0.0
        self._last_sent_target = -1.0
        self._last_sent_current_a = -1
        self._last_remote_start_attempt = None
        self._last_remote_stop_attempt = None
        self._last_profile_retry_attempt = None
        self._stalled = False
        self._stall_logged = False

    async def send_remote_start(self, cpid: str) -> bool:
        """Directly send a ``RemoteStartTransaction`` to a charger.

        Bypasses the anti-flap state machine — see
        :meth:`send_set_charging_profile` for why the equivalent bypass
        methods are not used for normal planner-driven operation. Wired to
        the ``ocpp_debug_start_charging`` service (issue #920) for
        diagnosing a charger that won't start over OCPP, without waiting
        out the start window or the planner's own target.

        Args:
            cpid: Charge-point identifier.

        Returns:
            ``True`` if the message was written to the socket.
        """
        if cpid not in self._chargers:
            _LOGGER.warning(
                "Cannot send RemoteStartTransaction — charger %s not connected", cpid
            )
            return False
        return await self._send_remote_start(self._chargers[cpid])

    async def send_set_charging_profile(
        self, cpid: str, max_power_w: int, max_current_a: int = 16
    ) -> bool:
        """Directly send a ``SetChargingProfile`` to a charger.

        Bypasses the anti-flap state machine.  Use
        :meth:`~ocpp_server.OCPPServer.update_charge_target` for normal
        planner-driven operation.

        Wired to the ``ocpp_debug_start_charging`` service (issue #920) as a
        manual override for diagnosing a charger that won't start over
        OCPP — not used by the planner's own normal-operation path,
        which goes through :meth:`~ocpp_server.OCPPServer.update_charge_target`
        instead. Kept as public API for direct/test use.

        Args:
            cpid: Charge-point identifier.
            max_power_w: Maximum charging power in watts.
            max_current_a: Maximum current in amperes.

        Returns:
            ``True`` if the message was written to the socket.
        """
        if cpid not in self._chargers:
            _LOGGER.warning(
                "Cannot send SetChargingProfile — charger %s not connected", cpid
            )
            return False
        return await self._send_set_charging_profile(
            self._chargers[cpid], max_power_w, max_current_a
        )

    async def send_remote_stop(self, cpid: str) -> bool:
        """Directly send a ``RemoteStopTransaction`` to a charger.

        Bypasses the anti-flap state machine. Wired to the
        ``ocpp_debug_stop_charging`` service (issue #920) — see
        :meth:`send_set_charging_profile` for why bypassing the anti-flap
        window is reserved for manual debugging, not normal operation.

        Args:
            cpid: Charge-point identifier.

        Returns:
            ``True`` if the message was written to the socket (or there was
            no active transaction to stop).
        """
        if cpid not in self._chargers:
            _LOGGER.warning(
                "Cannot send RemoteStopTransaction — charger %s not connected", cpid
            )
            return False
        return await self._send_remote_stop(self._chargers[cpid])


__all__ = [
    "CHARGER_STALL_THRESHOLD_S",
    "OCPPCommandsMixin",
    "charger_appears_stalled",
]

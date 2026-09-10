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

# Upper bound on outstanding outbound calls remembered per session, so the
# pending_calls map stays small across retries while still keeping both
# halves of a same-action pair (TxDefaultProfile + TxProfile) resolvable
# when their CALLRESULTs arrive (issue #920 follow-up). Oldest entries are
# dropped first; a charger that never answers a call would otherwise leak
# one entry per attempt.
_MAX_PENDING_CALLS = 8

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
    _stalled: bool
    _stall_logged: bool
    _chargers: dict[str, ChargerSession]

    # Declared (not assigned) so mypy resolves these against
    # OCPPControlMixin, which composes into the same OCPPServer (issue
    # #920) — capability lookups read from the charger's own
    # GetConfiguration reply rather than assuming defaults.
    profile_stack_levels: Callable[[ChargerSession], tuple[int, int]]
    station_max_current_a: Callable[[ChargerSession], int | None]
    ensure_charging_allowed: Callable[[ChargerSession], Coroutine[Any, Any, None]]

    # Declared (not assigned) so mypy resolves this against
    # OCPPProfilesMixin — the generic, fully standards-only stop mechanism.
    _send_zero_current_profile: Callable[[ChargerSession], Coroutine[Any, Any, bool]]

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
            # Track every outbound action, not just the three whose status
            # feeds last_call_status — otherwise a CALLRESULT for anything
            # else logs as "action=None" and can't be matched to what it
            # answers, which is exactly what wire-level debugging needs
            # (issue #920 follow-up). Two calls of the *same* action sent
            # back-to-back (the TxDefaultProfile/TxProfile pair) must both
            # stay resolvable, so entries are bounded by count rather than
            # purged by action name.
            session.pending_calls[msg_id] = action
            while len(session.pending_calls) > _MAX_PENDING_CALLS:
                session.pending_calls.pop(next(iter(session.pending_calls)))
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
        # Also needed here, not just in the public bypass: the anti-flap
        # path reaches this directly, and a charger left locally forced off
        # would accept the start below and ignore it (issue #920).
        await self.ensure_charging_allowed(session)
        payload = {"idTag": _REMOTE_START_ID_TAG}
        sent = await self._send_call(session, "RemoteStartTransaction", payload)
        if sent:
            _LOGGER.debug(
                "Sent RemoteStartTransaction to %s (idTag=%s)",
                session.cpid,
                _REMOTE_START_ID_TAG,
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

        # Stopping is standard OCPP only (issue #920 follow-up):
        #
        #   1. a 0 A charging profile — the idiomatic, fully generic way an
        #      energy-management system says "draw nothing";
        #   2. RemoteStopTransaction, below — the correct protocol action
        #      for ending the transaction itself.
        #
        # No vendor key is written here. This was originally step 3 of a
        # three-step ladder (a vendor `ForceState` write, escalated to
        # automatically on every stop) — but user testing proved a bare 0 A
        # profile alone (no RemoteStop, no vendor write) stops a go-e
        # Charger V4 and is reported by the charger's own app as "stopped
        # by OCPP". Writing a vendor key unconditionally when the generic
        # mechanism already works contradicts the point of trying
        # generic-first: it would leave the charger locally forced off
        # after every single stop for no reason, so that step was removed
        # entirely. A charger that genuinely needs a vendor write to
        # actually stop is still reachable manually via the
        # `ocpp_debug_set_configuration` service.
        #
        # Step 1 runs before the transaction check because a charger can
        # free-vend with no transaction open at all, so "nothing to stop"
        # must not mean "do nothing" — but only when something plausibly
        # *is* charging. A TxDefaultProfile persists on the charger, so
        # writing 0 A when HSEM never started anything (e.g. a target that
        # flipped to zero before the start window even fired) would leave
        # a lasting block on a connector HSEM never commanded, and could
        # silently stop the user charging by hand.
        if session.transaction_id is not None or session.status == "Charging":
            await self._send_zero_current_profile(session)
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

        A charger rejects ``RemoteStartTransaction`` outright when the
        connector already has a transaction in progress, so an active
        transaction is skipped rather than re-authorized (issue #920
        follow-up) — the same precondition
        :meth:`_send_remote_start` documents for its own callers, and the
        mirror of :meth:`_send_remote_stop`'s "nothing to stop" skip.

        Args:
            cpid: Charge-point identifier.

        Returns:
            ``True`` if the message was written to the socket, or if a
            transaction was already running (nothing to start counts as
            success).
        """
        if cpid not in self._chargers:
            _LOGGER.warning(
                "Cannot send RemoteStartTransaction — charger %s not connected", cpid
            )
            return False
        session = self._chargers[cpid]
        # Take the charger back from any local "don't charge" state first,
        # or the start below is accepted and then ignored (issue #920).
        await self.ensure_charging_allowed(session)
        if session.transaction_id is not None:
            _LOGGER.info(
                "OCPP %s already has transaction %s in progress — skipping "
                "RemoteStartTransaction (a charger rejects it while one is "
                "open; stop it first to start a new one)",
                cpid,
                session.transaction_id,
            )
            return True
        return await self._send_remote_start(session)

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
    "TRACKED_RESPONSE_ACTIONS",
    "OCPPCommandsMixin",
    "charger_appears_stalled",
]

#: Public alias — :mod:`ocpp_server` needs this to decide whether a
#: CALLRESULT's status belongs in ``last_call_status`` now that every
#: outbound action is tracked in ``pending_calls`` (issue #920 follow-up).
TRACKED_RESPONSE_ACTIONS = _TRACKED_RESPONSE_ACTIONS

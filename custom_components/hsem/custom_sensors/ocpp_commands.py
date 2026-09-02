"""Low-level outbound OCPP 1.6 command senders.

Extracted from :mod:`ocpp_server` to satisfy the repository's 30 KB /
1000-line file limit. A pure move: these methods keep their exact
behaviour and mix back into :class:`~ocpp_server.OCPPServer`, so ``self``
and every attribute reference (``_last_sent_target``,
``_last_sent_current_a``, ``_last_remote_start_attempt``,
``_remote_start_due``) are unchanged.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

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


class OCPPCommandsMixin:
    """Low-level senders for OCPP commands HSEM issues to a charger."""

    # Declared (not assigned) so mypy uses OCPPServer.__init__'s types
    # rather than inferring a narrower type from the assignments below.
    _last_remote_start_attempt: datetime | None
    _last_sent_target: float
    _last_sent_current_a: int

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
        # OCPP 1.6 ChargingProfile structure
        charging_profile = {
            "chargingProfileId": 1,
            "stackLevel": 0,
            "chargingProfilePurpose": "TxDefaultProfile",
            "chargingProfileKind": "Relative",
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

    async def _send_remote_stop(self, session: ChargerSession) -> bool:
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

        Args:
            session: The charger session.

        Returns:
            ``True`` if the message was written to the socket, or if there
            was no active transaction (nothing to stop counts as success).
        """
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


__all__ = ["OCPPCommandsMixin"]

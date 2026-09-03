"""OCPP 1.6 charger-initiated message handlers.

Originally extracted from :mod:`ocpp_server` to satisfy the repository's
30 KB / 1000-line file limit; these handlers mix into
:class:`~ocpp_server.OCPPServer`. Most only read/write
``session``/``payload``, but :meth:`_handle_start_transaction` also
allocates from :attr:`~ocpp_server.OCPPServer._next_transaction_id`
(issue #906) to assign each transaction a real, CS-issued ID rather than
trusting whatever (if anything) the charger sent, and the status-change,
start-transaction, and stop-transaction handlers call
:meth:`~ocpp_server.OCPPServer._notify_significant_event` (issue #908) to
trigger a debounced coordinator refresh promptly rather than waiting for
the next scheduled cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from custom_components.hsem.models.ocpp_session import ChargerSession

_LOGGER = logging.getLogger(__name__)


class OCPPMessageHandlersMixin:
    """Handlers for OCPP messages initiated by a connected charger."""

    # Declared (not assigned) so mypy uses OCPPServer.__init__'s type rather
    # than inferring one from _handle_start_transaction's usage below.
    _next_transaction_id: int

    # Declared (not assigned) so mypy resolves this against
    # OCPPCommandsMixin._notify_significant_event() rather than reporting a
    # missing attribute (issue #908).
    _notify_significant_event: Callable[[], Coroutine[Any, Any, None]]

    async def _handle_boot_notification(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``BootNotification`` request.

        Records charger identity and returns an ``Accepted`` response with
        a 300-second heartbeat interval.

        Args:
            session: The charger session.
            payload: BootNotification payload.

        Returns:
            Response dict with status, interval, and currentTime.
        """
        session.vendor = payload.get("chargePointVendor", "")
        session.model = payload.get("chargePointModel", "")
        session.firmware = payload.get("firmwareVersion", "")
        session.serial = payload.get("chargePointSerialNumber", "")
        _LOGGER.info(
            "OCPP BootNotification from %s: vendor=%s, model=%s, fw=%s, serial=%s",
            session.cpid,
            session.vendor,
            session.model,
            session.firmware,
            session.serial,
        )
        return {
            "status": "Accepted",
            "interval": 300,
            "currentTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    async def _handle_heartbeat(self, session: ChargerSession, payload: dict) -> dict:
        """Handle a ``Heartbeat`` request.

        Args:
            session: The charger session.
            payload: Heartbeat payload (unused).

        Returns:
            Response dict with currentTime.
        """
        session.last_heartbeat = datetime.now(UTC)
        return {
            "currentTime": session.last_heartbeat.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    async def _handle_status_notification(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``StatusNotification`` request.

        Updates the charger's status based on connector status, and records
        when the status actually changed (issue #894) — a repeated
        StatusNotification carrying the same status leaves
        ``status_changed_at`` untouched, so it reflects how long the
        charger has been stuck in its current state rather than the time
        of the most recent heartbeat-style repeat.

        Args:
            session: The charger session.
            payload: StatusNotification payload.

        Returns:
            Empty dict (CALLRESULT per OCPP spec).
        """
        new_status = payload.get("status", "")
        if new_status:
            if new_status != session.status:
                session.status_changed_at = datetime.now(UTC)
                session.status = new_status
                _LOGGER.debug(
                    "OCPP charger %s status changed to '%s'", session.cpid, new_status
                )
                # Notify promptly (issue #908) — only on an actual change,
                # not a repeated StatusNotification carrying the same
                # status.
                await self._notify_significant_event()
            else:
                session.status = new_status
        return {}

    async def _handle_meter_values(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``MeterValues`` request.

        Parses power and energy readings from the meter values and updates
        the session state.

        Args:
            session: The charger session.
            payload: MeterValues payload.

        Returns:
            ``None`` (empty response per OCPP spec).
        """
        connector_id = payload.get("connectorId", 0)
        meter_values = payload.get("meterValue", [])

        for mv in meter_values:
            sampled_values = mv.get("sampledValue", [])
            for sv in sampled_values:
                measurand = sv.get("measurand", "")
                value = sv.get("value", "0")
                try:
                    numeric_value = float(value)
                except ValueError, TypeError:
                    continue

                if measurand == "Power.Active.Import":
                    session.current_power_w = numeric_value
                elif measurand == "Energy.Active.Import.Register":
                    session.current_energy_wh = numeric_value
                elif measurand == "":
                    # Many chargers send power in an unlabelled field
                    unit = sv.get("unit", "")
                    if unit == "W" or unit == "":
                        session.current_power_w = numeric_value

        _LOGGER.debug(
            "OCPP MeterValues from %s (connector %d): power=%.0fW, energy=%.0fWh",
            session.cpid,
            connector_id,
            session.current_power_w,
            session.current_energy_wh,
        )
        return {}

    async def _handle_authorize(self, session: ChargerSession, payload: dict) -> dict:
        """Handle an ``Authorize`` request.

        Always accepts — this is a LAN-only server with no authentication.

        Args:
            session: The charger session.
            payload: Authorize payload.

        Returns:
            Response dict with idTagInfo status.
        """
        id_tag = payload.get("idTag", "unknown")
        _LOGGER.debug("OCPP Authorize from %s: idTag=%s", session.cpid, id_tag)
        return {"idTagInfo": {"status": "Accepted"}}

    async def _handle_start_transaction(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``StartTransaction`` request.

        Allocates a fresh transaction ID and returns it in an ``Accepted``
        response.

        Per OCPP 1.6 §5.14, ``StartTransaction.req`` (charger → CS) has no
        ``transactionId`` field — allocating one is the CS's job, returned
        in ``StartTransaction.conf``. Echoing back
        ``payload.get("transactionId", 0)`` (issue #906) meant every real
        charger, which never sends this field, got assigned ``0``. Chargers
        that treat ``0`` as an unset/sentinel value would then reject or
        ignore a subsequent ``RemoteStopTransaction`` carrying
        ``transactionId: 0``, since it never numerically matched a
        transaction they considered active.

        Args:
            session: The charger session.
            payload: StartTransaction payload (unused — see above).

        Returns:
            Response dict with the CS-assigned transactionId and idTagInfo.
        """
        transaction_id = self._next_transaction_id
        self._next_transaction_id += 1
        session.transaction_id = transaction_id
        _LOGGER.info(
            "OCPP StartTransaction from %s: assigned tx=%d",
            session.cpid,
            transaction_id,
        )
        await self._notify_significant_event()
        return {
            "transactionId": transaction_id,
            "idTagInfo": {"status": "Accepted"},
        }

    async def _handle_stop_transaction(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``StopTransaction`` request.

        Clears the transaction ID and returns an ``Accepted`` response.

        Args:
            session: The charger session.
            payload: StopTransaction payload.

        Returns:
            Response dict with idTagInfo.
        """
        transaction_id = payload.get("transactionId")
        _LOGGER.info(
            "OCPP StopTransaction from %s: tx=%s",
            session.cpid,
            transaction_id,
        )
        session.transaction_id = None
        await self._notify_significant_event()
        return {"idTagInfo": {"status": "Accepted"}}

    async def _handle_unknown(
        self, session: ChargerSession, payload: dict
    ) -> dict | None:
        """Handle an unknown/unsupported OCPP action.

        Logs a warning and returns ``None`` so no CALLERROR is sent.

        Args:
            session: The charger session.
            payload: Message payload.

        Returns:
            ``None``.
        """
        _LOGGER.debug(
            "OCPP unknown action from charger %s: payload=%s",
            session.cpid,
            payload,
        )
        return None


__all__ = ["OCPPMessageHandlersMixin"]

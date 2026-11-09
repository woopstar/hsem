"""OCPP 1.6 charger-initiated message handlers.

Originally extracted from :mod:`ocpp_server` to satisfy the repository's
30 KB / 1000-line file limit; these handlers mix into
:class:`~ocpp_server.OCPPServer`. The transaction lifecycle
(``StartTransaction``, ``StopTransaction`` and adopting an open
transaction) lives in :class:`~ocpp_transactions.OCPPTransactionsMixin`,
which this mixin inherits (issue #1410). The status-change handler calls
:meth:`~ocpp_server.OCPPServer._notify_significant_event` (issue #908) to
trigger a debounced coordinator refresh promptly rather than waiting for
the next scheduled cycle.

:meth:`_handle_status_notification` also arms/releases the connect-time
"pending plan" gate (issue #969, :mod:`~ocpp_anti_flap`) on a status
transition into/out of ``"Available"`` — the earliest available signal
that a car was plugged in or unplugged, ahead of a self-authorizing
charger's own ``StartTransaction``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from custom_components.hsem.custom_sensors.ocpp_flap_state import FlapState
from custom_components.hsem.custom_sensors.ocpp_meter_values import PHASES
from custom_components.hsem.custom_sensors.ocpp_transactions import (
    STARTED_BY_HSEM,
    STARTED_BY_LOCAL,
    STARTED_BY_UNKNOWN,
    OCPPTransactionsMixin,
)
from custom_components.hsem.models.ocpp_session import ChargerSession

_LOGGER = logging.getLogger(__name__)


class OCPPMessageHandlersMixin(OCPPTransactionsMixin):
    """Handlers for OCPP messages initiated by a connected charger."""

    # Declared (not assigned) so mypy resolves this against
    # OCPPCommandsMixin._notify_significant_event() rather than reporting a
    # missing attribute (issue #908).
    _notify_significant_event: Callable[[], Coroutine[Any, Any, None]]

    # Declared (not assigned) so mypy resolves this against
    # OCPPControlMixin, which composes into the same OCPPServer.
    send_get_configuration: Callable[[str], Coroutine[Any, Any, bool]]

    # Declared (not assigned) so mypy resolves these against
    # OCPPAntiFlapMixin, which composes into the same OCPPServer — the
    # connect-time pending-plan gate (issue #969).
    _flap_state: FlapState
    _arm_connect_gate: Callable[[ChargerSession], None]
    _schedule_release_connect_gate: Callable[[ChargerSession], None]

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
        # Learn what this charger can actually do, rather than assuming
        # (issue #920): the reply drives charge-profile stack levels, the
        # station current cap, and whether a vendor "don't charge" key
        # needs clearing before a remote start can take effect. Scheduled
        # detached for the same reason as the profile resend — this
        # handler's return value is only sent as the BootNotification
        # CALLRESULT once it returns.
        task = asyncio.create_task(self._request_configuration(session))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return {
            "status": "Accepted",
            "interval": 300,
            "currentTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    async def _request_configuration(self, session: ChargerSession) -> None:
        """Background task: ask a newly-booted charger for its configuration.

        Args:
            session: The charger session.
        """
        try:
            await self.send_get_configuration(session.cpid)
        except Exception:
            _LOGGER.exception(
                "OCPP %s: GetConfiguration request after BootNotification failed",
                session.cpid,
            )

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

        ``connectorId`` is respected (issue #990): per OCPP 1.6,
        ``connectorId == 0`` reports the status of the *charge point
        itself*, not of the connector the car is plugged into. Those
        messages update ``session.charge_point_status`` only — they must
        never overwrite the connector-level ``session.status``, or a
        charge-point-level ``Available`` could be misread as "car
        unplugged" and release the connect gate while a connector-level
        session is active.

        Args:
            session: The charger session.
            payload: StatusNotification payload.

        Returns:
            Empty dict (CALLRESULT per OCPP spec).
        """
        new_status = payload.get("status", "")
        if not new_status:
            return {}

        # connectorId is mandatory per OCPP 1.6; a payload missing it is
        # treated as connector-level (1) to preserve the pre-#990 lenient
        # behaviour for non-conformant chargers.
        connector_id = payload.get("connectorId", 1)
        if connector_id == 0:
            if new_status != session.charge_point_status:
                session.charge_point_status = new_status
                _LOGGER.debug(
                    "OCPP charger %s charge-point status changed to '%s'",
                    session.cpid,
                    new_status,
                )
            return {}

        if new_status != session.status:
            old_status = session.status
            session.status_changed_at = datetime.now(UTC)
            session.status = new_status
            _LOGGER.debug(
                "OCPP charger %s status changed to '%s'", session.cpid, new_status
            )
            # Notify promptly (issue #908) — only on an actual change,
            # not a repeated StatusNotification carrying the same
            # status.
            await self._notify_significant_event()

            # Gate a fresh connection against free-vending before the
            # planner has had a chance to decide anything for it yet
            # (issue #969). The charger's own status leaving
            # "Available" is the earliest signal HSEM has that a car
            # was just plugged in — some chargers free-vend without
            # ever sending StartTransaction through HSEM first. Only
            # armed while HSEM's own anti-flap state is still "idle":
            # a transition HSEM itself caused (e.g. its own remote
            # start moving status to "Charging") is not a free-vend
            # risk and must not re-arm the gate.
            if (
                old_status == "Available"
                and new_status != "Available"
                and self._flap_state == FlapState.Idle
            ):
                self._arm_connect_gate(session)
            elif new_status == "Available" and (
                session.gate_pending_plan or session.hsem_zero_profile_active
            ):
                # The car was unplugged either before the planner ever
                # decided anything for it, or while HSEM was holding it at
                # an enforced zero (issue #990) — release the 0 A block
                # instead of leaving it standing on an idle connector.
                self._schedule_release_connect_gate(session)
        else:
            session.status = new_status
        if new_status == "Available" and session.transaction_id is not None:
            self._drop_transaction_on_available(session)
        return {}

    async def _handle_meter_values(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``MeterValues`` request.

        Parses power and energy readings from the meter values and updates
        the session state. Also adopts the charger's own ``transactionId``
        when HSEM has none recorded — see
        :meth:`_adopt_transaction_from_meter_values`.

        Args:
            session: The charger session.
            payload: MeterValues payload.

        Returns:
            ``None`` (empty response per OCPP spec).
        """
        connector_id = payload.get("connectorId", 0)
        meter_values = payload.get("meterValue", [])

        await self._adopt_transaction_from_meter_values(session, payload, meter_values)

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
                elif measurand == "Current.Import" and sv.get("phase") in PHASES:
                    # Per-phase current (issue #1391); a phase-less total
                    # says nothing about how many phases are in use.
                    session.phase_currents_a[sv["phase"]] = numeric_value
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

    async def _handle_data_transfer(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``DataTransfer`` request.

        HSEM implements no vendor extensions, so the OCPP 1.6 answer is
        ``UnknownVendorId`` (issue #1390). Before, the charger got no reply
        at all.

        Args:
            session: The charger session.
            payload: DataTransfer payload.

        Returns:
            Response dict with status ``UnknownVendorId``.
        """
        _LOGGER.debug(
            "OCPP DataTransfer from %s: vendorId=%s, messageId=%s",
            session.cpid,
            payload.get("vendorId"),
            payload.get("messageId"),
        )
        return {"status": "UnknownVendorId"}

    async def _handle_firmware_status(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``FirmwareStatusNotification`` request (issue #1390).

        Args:
            session: The charger session.
            payload: FirmwareStatusNotification payload.

        Returns:
            Empty response, as OCPP 1.6 defines it.
        """
        _LOGGER.info("OCPP %s: firmware status %s", session.cpid, payload.get("status"))
        return {}

    async def _handle_diagnostics_status(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``DiagnosticsStatusNotification`` request (issue #1390).

        Args:
            session: The charger session.
            payload: DiagnosticsStatusNotification payload.

        Returns:
            Empty response, as OCPP 1.6 defines it.
        """
        _LOGGER.info(
            "OCPP %s: diagnostics status %s", session.cpid, payload.get("status")
        )
        return {}

    async def _handle_security_event(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``SecurityEventNotification`` request (issue #1406).

        Sent by chargers with the OCPP 1.6 security extension, for example
        on a firmware update, tamper detection or a rejected certificate.
        Logged at warning, since it is a security event.

        Args:
            session: The charger session.
            payload: SecurityEventNotification payload.

        Returns:
            Empty response, as the security extension defines it.
        """
        _LOGGER.warning(
            "OCPP %s: security event %s at %s (techInfo: %r)",
            session.cpid,
            payload.get("type"),
            payload.get("timestamp"),
            payload.get("techInfo"),
        )
        return {}


# STARTED_BY_* are re-exported: they were defined here before issue #1410.
__all__ = [
    "STARTED_BY_HSEM",
    "STARTED_BY_LOCAL",
    "STARTED_BY_UNKNOWN",
    "OCPPMessageHandlersMixin",
]

"""Embedded OCPP 1.6 WebSocket server for EV charger control.

This module provides an optional, LAN-only OCPP 1.6 JSON WebSocket server
that listens for charger connections and dispatches :class:`SetChargingProfile`
commands based on HSEM's EV charging plan.

Architecture::

    EV Charger ──WebSocket──▶ OCPPServer (asyncio task)
                                  │
                                  ├── Reads EV plan from CoordinatorData
                                  ├── Writes charger state to CoordinatorData
                                  └── Dispatches SetChargingProfile commands

.. important::
    This server binds to ``0.0.0.0`` by default.  The port MUST NOT be
    exposed to the public internet — it is LAN-only by design and performs
    no authentication on incoming connections.

Usage
-----
The server is managed by the HSEM coordinator:

- Created in :meth:`HSEMDataUpdateCoordinator.async_setup` when
  ``ocpp_enabled`` is ``True``.
- Stopped in :meth:`HSEMDataUpdateCoordinator.async_teardown`.
- Charge targets are updated after each planner cycle via
  :meth:`OCPPServer.update_charge_target`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from custom_components.hsem.models.ocpp_session import ChargerSession

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OCPP 1.6 JSON message type indicators (per OCPP-J 1.6 §4.2)
# ---------------------------------------------------------------------------
_CALL = 2  # Client → Server request (expects CALLRESULT or CALLERROR)
_CALLRESULT = 3  # Server → Client response
_CALLERROR = 4  # Server → Client error

# ---------------------------------------------------------------------------
# Anti-flap defaults (seconds)
# ---------------------------------------------------------------------------
_DEFAULT_START_WINDOW_S = 60  # Sustained surplus required before starting
_DEFAULT_STOP_WINDOW_S = 180  # Sustained shortage required before stopping

# Per-slot epsilon for floating-point comparisons (kWh)
_SLOT_EPSILON = 1e-6


class OCPPServer:
    """Embedded OCPP 1.6 WebSocket server for LAN-only EV charger control.

    Listens on a configurable TCP port and handles OCPP 1.6 JSON messages
    from one or more chargers.  Charge targets are pushed from the HSEM
    planner via :meth:`update_charge_target`.

    Attributes:
        hass: The Home Assistant instance (used only for helper access).
        host: Bind address (default ``"0.0.0.0"``).
        port: TCP port (default ``9000``).
    """

    def __init__(
        self,
        hass: Any,
        host: str = "0.0.0.0",
        port: int = 9000,
        start_window_s: int = _DEFAULT_START_WINDOW_S,
        stop_window_s: int = _DEFAULT_STOP_WINDOW_S,
    ) -> None:
        """Initialise the OCPP server.

        Args:
            hass: The Home Assistant instance.
            host: Bind address.
            port: TCP port.
            start_window_s: Seconds of sustained surplus before starting
                a charge.
            stop_window_s: Seconds of sustained shortage before stopping
                a charge.
        """
        self._hass = hass
        self._host = host
        self._port = port
        self._start_window_s = start_window_s
        self._stop_window_s = stop_window_s

        # Runtime state
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._chargers: dict[str, ChargerSession] = {}

        # Charge target tracking (anti-flap)
        self._target_power_w: float = 0.0
        self._target_entered_at: datetime | None = None
        self._zero_entered_at: datetime | None = None
        self._last_sent_target: float = -1.0  # Track last sent to avoid duplicates

        # Anti-flap state machine: "idle", "starting", "charging", "stopping"
        self._flap_state: str = "idle"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the aiohttp WebSocket server.

        Creates an :class:`aiohttp.web.Application` with a single route,
        ``/``, that upgrades to WebSocket and delegates to
        :meth:`_handle_charger`.
        """
        app = web.Application()
        app.router.add_get("/", self._handle_charger)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        _LOGGER.info(
            "OCPP server started on %s:%d (LAN-only — do not expose to internet)",
            self._host,
            self._port,
        )

    async def stop(self) -> None:
        """Stop the server and close all charger connections."""
        # Close all charger sessions
        for cpid, session in list(self._chargers.items()):
            try:
                if session.websocket is not None:
                    await session.websocket.close()
            except Exception:
                _LOGGER.debug("Error closing charger %s WebSocket — ignoring", cpid)
        self._chargers.clear()

        # Stop the aiohttp site
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        _LOGGER.info("OCPP server stopped")

    @property
    def charger_sessions(self) -> dict[str, ChargerSession]:
        """Return a copy of the current charger sessions dict."""
        return dict(self._chargers)

    @property
    def active_chargers(self) -> list[str]:
        """Return list of CPIDs for currently connected chargers."""
        return list(self._chargers.keys())

    async def update_charge_target(
        self,
        cpid: str,
        target_power_kw: float,
        max_current_a: int = 16,
        now: datetime | None = None,
    ) -> None:
        """Update the charge target for a charger with anti-flap logic.

        When *target_power_kw* > 0 for longer than the start window, a
        ``SetChargingProfile`` message is sent to begin charging.  When
        *target_power_kw* == 0 for longer than the stop window, a
        ``RemoteStopTransaction`` message is sent.

        Args:
            cpid: Charge-point identifier.
            target_power_kw: Desired charging power in kW (0 = stop).
            max_current_a: Maximum charging current in amperes (used to
                build the charging profile).  Default 16 A.
            now: Current timestamp (injected for testability).
        """
        if cpid not in self._chargers:
            return

        session = self._chargers[cpid]
        if now is None:
            now = datetime.now(UTC)

        target_w = target_power_kw * 1000.0

        # Anti-flap state machine
        if target_w > _SLOT_EPSILON:
            # Target is non-zero — handle start window
            if self._flap_state == "idle" or self._flap_state == "stopping":
                if self._flap_state != "starting":
                    self._target_entered_at = now
                    self._flap_state = "starting"
                elapsed = (now - self._target_entered_at).total_seconds()
                if elapsed >= self._start_window_s:
                    self._flap_state = "charging"
                    await self._send_set_charging_profile(
                        session, int(target_w), max_current_a
                    )
                else:
                    _LOGGER.debug(
                        "OCPP anti-flap: waiting for start window "
                        "(elapsed=%.1fs, needed=%ds)",
                        elapsed,
                        self._start_window_s,
                    )
            elif self._flap_state == "charging":
                # Already charging — update if target changed materially
                if abs(target_w - self._last_sent_target) > 50.0:
                    await self._send_set_charging_profile(
                        session, int(target_w), max_current_a
                    )
            self._zero_entered_at = None
        else:
            # Target is zero — handle stop window
            if self._flap_state == "charging" or self._flap_state == "starting":
                if self._flap_state != "stopping":
                    self._zero_entered_at = now
                    self._flap_state = "stopping"
                elapsed = (now - self._zero_entered_at).total_seconds()
                if elapsed >= self._stop_window_s:
                    self._flap_state = "idle"
                    await self._send_remote_stop(session)
                else:
                    _LOGGER.debug(
                        "OCPP anti-flap: waiting for stop window "
                        "(elapsed=%.1fs, needed=%ds)",
                        elapsed,
                        self._stop_window_s,
                    )
            self._target_entered_at = None
            self._target_power_w = 0.0

    async def send_set_charging_profile(
        self, cpid: str, max_power_w: int, max_current_a: int = 16
    ) -> None:
        """Directly send a ``SetChargingProfile`` to a charger.

        Bypasses the anti-flap state machine.  Use
        :meth:`update_charge_target` for normal planner-driven operation.

        Args:
            cpid: Charge-point identifier.
            max_power_w: Maximum charging power in watts.
            max_current_a: Maximum current in amperes.
        """
        if cpid not in self._chargers:
            _LOGGER.warning(
                "Cannot send SetChargingProfile — charger %s not connected", cpid
            )
            return
        await self._send_set_charging_profile(
            self._chargers[cpid], max_power_w, max_current_a
        )

    async def send_remote_stop(self, cpid: str) -> None:
        """Directly send a ``RemoteStopTransaction`` to a charger.

        Args:
            cpid: Charge-point identifier.
        """
        if cpid not in self._chargers:
            _LOGGER.warning(
                "Cannot send RemoteStopTransaction — charger %s not connected", cpid
            )
            return
        await self._send_remote_stop(self._chargers[cpid])

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------

    async def _handle_charger(self, request: web.Request) -> web.WebSocketResponse:
        """Handle a charger WebSocket connection.

        Inspects the request path for a CPID (e.g. ``/<cpid>/``) and starts
        the OCPP message loop.

        Args:
            request: The incoming aiohttp request.

        Returns:
            A :class:`web.WebSocketResponse` that stays open for the
            duration of the charger session.
        """
        # Extract CPID from path — strip leading/trailing slashes
        cpid = request.path.strip("/") or "default"
        _LOGGER.info("OCPP charger connected: CPID=%s from %s", cpid, request.remote)

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        session = ChargerSession(
            cpid=cpid,
            websocket=ws,
            connected_at=datetime.now(UTC),
        )
        self._chargers[cpid] = session

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._handle_message(session, msg.data)
                elif msg.type == web.WSMsgType.ERROR:
                    _LOGGER.error(
                        "WebSocket error for charger %s: %s", cpid, ws.exception()
                    )
        except ConnectionResetError, asyncio.CancelledError:
            _LOGGER.debug("Charger %s disconnected", cpid)
        finally:
            self._chargers.pop(cpid, None)
            _LOGGER.info("OCPP charger %s session ended", cpid)

        return ws

    async def _handle_message(self, session: ChargerSession, raw: str) -> None:
        """Parse and dispatch an incoming OCPP JSON message.

        Args:
            session: The charger session.
            raw: Raw JSON string from the charger.
        """
        try:
            msg = json.loads(raw)
            if not isinstance(msg, list) or len(msg) < 3:
                _LOGGER.warning("Malformed OCPP message from %s: %s", session.cpid, raw)
                return

            msg_type = msg[0]  # OCPP message type indicator
            msg_id = msg[1]  # Unique message ID
            action = msg[2]  # e.g. "BootNotification", "Heartbeat"
            payload = msg[3] if len(msg) > 3 else {}

            await self._dispatch(session, msg_type, msg_id, action, payload)
        except json.JSONDecodeError:
            _LOGGER.warning("Invalid JSON from charger %s: %s", session.cpid, raw)
        except Exception:
            _LOGGER.exception(
                "Error handling OCPP message from charger %s", session.cpid
            )

    async def _dispatch(
        self,
        session: ChargerSession,
        msg_type: int,
        msg_id: str,
        action: str,
        payload: dict,
    ) -> None:
        """Route an OCPP message to the appropriate handler.

        Args:
            session: The charger session.
            msg_type: OCPP message type indicator.
            msg_id: Unique message ID.
            action: OCPP action name.
            payload: Message payload dict.
        """
        handlers: dict[str, Any] = {
            "BootNotification": self._handle_boot_notification,
            "Heartbeat": self._handle_heartbeat,
            "StatusNotification": self._handle_status_notification,
            "MeterValues": self._handle_meter_values,
            "Authorize": self._handle_authorize,
            "StartTransaction": self._handle_start_transaction,
            "StopTransaction": self._handle_stop_transaction,
        }

        handler = handlers.get(action, self._handle_unknown)
        if handler is None:
            handler = self._handle_unknown

        response = await handler(session, payload)
        if response is not None and msg_type == _CALL:
            await self._send_response(session, msg_id, response)

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
    ) -> None:
        """Send a CALL (type 2) message to the charger.

        Args:
            session: The charger session.
            action: OCPP action name (e.g. "SetChargingProfile").
            payload: The message payload.
        """
        try:
            msg_id = f"hsem-{datetime.now(UTC).timestamp()}"
            msg = json.dumps([_CALL, msg_id, action, payload])
            await session.websocket.send_str(msg)
        except Exception:
            _LOGGER.exception(
                "Failed to send OCPP call '%s' to charger %s",
                action,
                session.cpid,
            )

    async def _send_set_charging_profile(
        self, session: ChargerSession, max_power_w: int, max_current_a: int = 16
    ) -> None:
        """Send a ``SetChargingProfile`` request.

        Builds a TxDefaultProfile that limits charging to *max_current_a*
        amps, which at 230 V nominally equals *max_power_w*.

        Args:
            session: The charger session.
            max_power_w: Maximum charging power in watts.
            max_current_a: Maximum current in amperes.
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

        await self._send_call(session, "SetChargingProfile", payload)
        self._last_sent_target = float(max_power_w)
        _LOGGER.debug(
            "Sent SetChargingProfile to %s: max %d A (~%d W)",
            session.cpid,
            max_current_a,
            max_power_w,
        )

    async def _send_remote_stop(self, session: ChargerSession) -> None:
        """Send a ``RemoteStopTransaction`` request.

        Args:
            session: The charger session.
        """
        if session.transaction_id is not None:
            payload = {"transactionId": session.transaction_id}
        else:
            payload = {}
        await self._send_call(session, "RemoteStopTransaction", payload)
        self._last_sent_target = -1.0
        _LOGGER.debug(
            "Sent RemoteStopTransaction to %s (tx=%s)",
            session.cpid,
            session.transaction_id,
        )

    # ------------------------------------------------------------------
    # OCPP message handlers
    # ------------------------------------------------------------------

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

        Updates the charger's status based on connector status.

        Args:
            session: The charger session.
            payload: StatusNotification payload.

        Returns:
            Empty dict (CALLRESULT per OCPP spec).
        """
        new_status = payload.get("status", "")
        if new_status:
            session.status = new_status
            _LOGGER.debug(
                "OCPP charger %s status changed to '%s'", session.cpid, new_status
            )
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

        Records the transaction ID and returns an ``Accepted`` response.

        Args:
            session: The charger session.
            payload: StartTransaction payload.

        Returns:
            Response dict with transactionId and idTagInfo.
        """
        transaction_id = payload.get("transactionId", 0)
        session.transaction_id = transaction_id
        _LOGGER.info(
            "OCPP StartTransaction from %s: tx=%d",
            session.cpid,
            transaction_id,
        )
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

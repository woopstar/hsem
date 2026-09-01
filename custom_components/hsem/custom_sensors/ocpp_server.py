"""Embedded OCPP 1.6 WebSocket server for EV charger control.

This module provides an optional, LAN-only OCPP 1.6 JSON WebSocket server
that listens for charger connections and dispatches
:class:`RemoteStartTransaction`/:class:`SetChargingProfile`/
:class:`RemoteStopTransaction` commands based on HSEM's EV charging plan.

Architecture::

    EV Charger ──WebSocket──▶ OCPPServer (asyncio task)
                                  │
                                  ├── Reads EV plan from CoordinatorData
                                  ├── Writes charger state to CoordinatorData
                                  └── Dispatches RemoteStart/SetChargingProfile/
                                      RemoteStop commands

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

from custom_components.hsem.custom_sensors.ocpp_message_handlers import (
    OCPPMessageHandlersMixin,
)
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

# OCPP 1.6 requires a non-empty idTag on RemoteStartTransaction. HSEM has no
# per-user RFID/app identity concept, so every session it authorizes uses the
# same fixed tag (issue #892).
_REMOTE_START_ID_TAG = "HSEM"

# Minimum seconds between RemoteStartTransaction retries while a session
# still hasn't confirmed a transaction (issue #892). Rejected, dropped, or
# unanswered start requests are retried on this cadence rather than only
# once.
_REMOTE_START_RETRY_INTERVAL_S = 60


class OCPPServer(OCPPMessageHandlersMixin):
    """Embedded OCPP 1.6 WebSocket server for LAN-only EV charger control.

    Listens on a configurable TCP port and handles OCPP 1.6 JSON messages
    from one or more chargers.  Charge targets are pushed from the HSEM
    planner via :meth:`update_charge_target`. Charger-initiated message
    handlers (BootNotification, Heartbeat, etc.) live in
    :class:`OCPPMessageHandlersMixin`, split out to satisfy the 30 KB /
    1000-line file limit.

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
        self._last_sent_current_a: int = -1  # Last requested amps, -1 = none sent

        # Anti-flap state machine: "idle", "starting", "charging", "stopping"
        self._flap_state: str = "idle"

        # RemoteStartTransaction retry tracking (issue #892). HSEM never
        # correlates OCPP CALLRESULTs to a specific outbound request, so the
        # only reliable "did it actually start" signal is the charger's own
        # subsequent StartTransaction call (session.transaction_id). While
        # that stays None after a start attempt, retry on a cooldown rather
        # than assuming the single attempt succeeded.
        self._last_remote_start_attempt: datetime | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the aiohttp WebSocket server.

        Creates an :class:`aiohttp.web.Application` with a wildcard route
        that upgrades any path to WebSocket and delegates to
        :meth:`_handle_charger`, which derives the charge-point identifier
        from the connection path (``/`` → ``"default"``, ``/222819`` →
        ``"222819"``). A literal ``/`` route alone would 404 any charger
        connecting with its own CPID in the path (issue #892).
        """
        app = web.Application()
        app.router.add_get("/{tail:.*}", self._handle_charger)

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
    def host(self) -> str:
        """Return the configured bind address."""
        return self._host

    @property
    def port(self) -> int:
        """Return the configured TCP port."""
        return self._port

    @property
    def is_listening(self) -> bool:
        """Return True while the aiohttp WebSocket site is bound and active."""
        return self._site is not None

    @property
    def last_requested_current_a(self) -> int | None:
        """Return the amperage in the last ``SetChargingProfile`` sent.

        ``None`` until the first profile has been sent to a connected
        charger this session.
        """
        return self._last_sent_current_a if self._last_sent_current_a >= 0 else None

    @property
    def charger_sessions(self) -> dict[str, ChargerSession]:
        """Return a copy of the current charger sessions dict."""
        return dict(self._chargers)

    @property
    def active_chargers(self) -> list[str]:
        """Return list of CPIDs for currently connected chargers.

        Manual/diagnostic accessor — no HA service currently exposes this
        (see issue #843). Kept as public API for direct/test use alongside
        :meth:`send_set_charging_profile` and :meth:`send_remote_stop`.
        """
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
            if self._flap_state in ("idle", "stopping", "starting"):
                if self._flap_state != "starting":
                    self._target_entered_at = now
                    self._flap_state = "starting"
                target_at = self._target_entered_at
                if target_at is None:
                    target_at = now
                elapsed = (now - target_at).total_seconds()
                if elapsed >= self._start_window_s:
                    self._flap_state = "charging"
                    if session.transaction_id is None:
                        await self._send_remote_start(session, now=now)
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
                # Still no confirmed transaction from the charger — the
                # first RemoteStartTransaction may have been rejected,
                # dropped, or simply never answered. Retry on a cooldown
                # rather than leaving the session stuck (issue #892).
                if session.transaction_id is None and self._remote_start_due(now):
                    await self._send_remote_start(session, now=now)
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
                zero_at = self._zero_entered_at
                if zero_at is None:
                    zero_at = now
                elapsed = (now - zero_at).total_seconds()
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

    def _remote_start_due(self, now: datetime) -> bool:
        """Return whether enough time has passed to retry RemoteStartTransaction.

        Args:
            now: Current timestamp.
        """
        if self._last_remote_start_attempt is None:
            return True
        elapsed = (now - self._last_remote_start_attempt).total_seconds()
        return elapsed >= _REMOTE_START_RETRY_INTERVAL_S

    async def send_set_charging_profile(
        self, cpid: str, max_power_w: int, max_current_a: int = 16
    ) -> None:
        """Directly send a ``SetChargingProfile`` to a charger.

        Bypasses the anti-flap state machine.  Use
        :meth:`update_charge_target` for normal planner-driven operation.

        No HA service registers this as a manual override (see issue #843
        — deliberately left unwired: registering it would let a user bypass
        the anti-flap safety window with no corresponding product need).
        Kept as public API for direct/test use.

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

        Bypasses the anti-flap state machine — see
        :meth:`send_set_charging_profile` for why this is intentionally not
        wired to an HA service (issue #843).

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

        OCPP-J message shapes differ by type, so ``msg[2]`` cannot be
        blindly read as an action name for every message: a CALL is
        ``[2, id, action, payload]``, but a CALLRESULT is only
        ``[3, id, payload]`` — a reply to something *HSEM* sent (e.g.
        ``RemoteStartTransaction.conf``), not a new request. Treating a
        CALLRESULT's payload as an action name used to raise
        ``TypeError: unhashable type: 'dict'`` deep in :meth:`_dispatch`
        (issue #892), silently discarding every response to HSEM's own
        outbound calls.

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

            if msg_type == _CALL:
                msg_id = msg[1]
                action = msg[2]
                payload = msg[3] if len(msg) > 3 else {}
                await self._dispatch(session, msg_id, action, payload)
            elif msg_type == _CALLRESULT:
                # Response to an outbound HSEM call (RemoteStartTransaction,
                # SetChargingProfile, RemoteStopTransaction). HSEM does not
                # correlate these to a specific request today — the
                # ground-truth confirmation that a session actually started
                # is the charger's own subsequent StartTransaction call,
                # handled by _handle_start_transaction() and retried on a
                # cooldown by update_charge_target() while it never arrives.
                _LOGGER.debug(
                    "OCPP CALLRESULT from %s (id=%s): %s",
                    session.cpid,
                    msg[1],
                    msg[2],
                )
            elif msg_type == _CALLERROR:
                _LOGGER.warning(
                    "OCPP CALLERROR from %s (id=%s): %s",
                    session.cpid,
                    msg[1],
                    msg[2:],
                )
            else:
                _LOGGER.warning(
                    "Unknown OCPP message type %s from %s: %s",
                    msg_type,
                    session.cpid,
                    raw,
                )
        except json.JSONDecodeError:
            _LOGGER.warning("Invalid JSON from charger %s: %s", session.cpid, raw)
        except Exception:
            _LOGGER.exception(
                "Error handling OCPP message from charger %s", session.cpid
            )

    async def _dispatch(
        self,
        session: ChargerSession,
        msg_id: str,
        action: str,
        payload: dict,
    ) -> None:
        """Route an incoming OCPP CALL to the appropriate handler.

        Args:
            session: The charger session.
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
        if response is not None:
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

    async def _send_remote_start(
        self, session: ChargerSession, *, now: datetime | None = None
    ) -> None:
        """Send a ``RemoteStartTransaction`` request.

        A ``SetChargingProfile`` alone only configures a ceiling for
        whichever transaction is active — it does not authorize or start
        one. Without an explicit start signal, a charger that requires
        central-system authorization (rather than free-vending on
        plug-in) sits in ``SuspendedEVSE`` indefinitely (issue #892).
        Callers must only invoke this when
        :attr:`ChargerSession.transaction_id` is ``None``, so an
        already-active transaction is never re-authorized. Records the
        attempt timestamp so :meth:`_remote_start_due` can pace retries.

        Args:
            session: The charger session.
            now: Current timestamp (injected for testability).
        """
        self._last_remote_start_attempt = now if now is not None else datetime.now(UTC)
        payload = {"idTag": _REMOTE_START_ID_TAG}
        await self._send_call(session, "RemoteStartTransaction", payload)
        _LOGGER.debug(
            "Sent RemoteStartTransaction to %s (idTag=%s)",
            session.cpid,
            _REMOTE_START_ID_TAG,
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
        self._last_sent_current_a = max_current_a
        _LOGGER.debug(
            "Sent SetChargingProfile to %s: max %d A (~%d W)",
            session.cpid,
            max_current_a,
            max_power_w,
        )

    async def _send_remote_stop(self, session: ChargerSession) -> None:
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
        """
        self._last_sent_target = -1.0
        self._last_sent_current_a = -1
        if session.transaction_id is None:
            _LOGGER.debug(
                "OCPP %s has no active transaction — skipping "
                "RemoteStopTransaction (nothing to stop)",
                session.cpid,
            )
            return
        payload = {"transactionId": session.transaction_id}
        await self._send_call(session, "RemoteStopTransaction", payload)
        _LOGGER.debug(
            "Sent RemoteStopTransaction to %s (tx=%s)",
            session.cpid,
            session.transaction_id,
        )

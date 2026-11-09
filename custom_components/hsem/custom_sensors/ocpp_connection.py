"""WebSocket connection and OCPP-J frame handling for the OCPP server.

Split out of :mod:`ocpp_server` (issue #1402) to keep it under the 30 KB
file limit. :class:`OCPPConnectionMixin` accepts a charger's WebSocket
(mutual-TLS CN check, ``ocpp1.6`` subprotocol, aiohttp heartbeat), reads
its frames, and routes them by OCPP-J message type: a CALL goes to the
matching handler in :class:`~ocpp_message_handlers.OCPPMessageHandlersMixin`
and always gets a CALLRESULT or CALLERROR back (issue #1390), and a
CALLRESULT or CALLERROR is matched to the HSEM call it answers. It mixes
into :class:`~ocpp_server.OCPPServer`, which owns the state declared below.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from custom_components.hsem.custom_sensors.ocpp_commands import (
    _CALL,
    _CALLERROR,
    _CALLRESULT,
    TRACKED_RESPONSE_ACTIONS,
)
from custom_components.hsem.custom_sensors.ocpp_control import DIAGNOSTIC_ACTIONS
from custom_components.hsem.custom_sensors.ocpp_message_handlers import (
    OCPPMessageHandlersMixin,
)
from custom_components.hsem.models.ocpp_session import ChargerSession
from custom_components.hsem.utils.ocpp_tls import (
    cpid_matches_common_name,
    peer_common_name,
)

_LOGGER = logging.getLogger(__name__)

# aiohttp WebSocket-level ping interval (issue #892). Detects a charger that
# silently stops responding (dead TCP, network drop) without a clean close —
# aiohttp auto-pings at this interval and closes the connection if no pong
# arrives within half of it, independent of OCPP's own application-level
# Heartbeat message.
_WS_HEARTBEAT_INTERVAL_S = 30.0

# The WebSocket subprotocol OCPP-J 1.6 clients offer, and which the central
# system must select in the handshake response (OCPP-J 1.6 §3.1.2).
_OCPP_SUBPROTOCOL = "ocpp1.6"


class OCPPConnectionMixin(OCPPMessageHandlersMixin):
    """Accept charger WebSockets and route their OCPP-J frames."""

    # Declared (not assigned) so mypy uses OCPPServer.__init__'s types and
    # resolves the senders against OCPPCommandsMixin / OCPPControlMixin,
    # which compose into the same OCPPServer.
    _ssl_context: ssl.SSLContext | None
    _allow_cn_mismatch: bool
    _chargers: dict[str, ChargerSession]
    _reset_anti_flap_state: Callable[[], None]
    _send_response: Callable[[ChargerSession, str, dict], Coroutine[Any, Any, None]]
    _send_call_error: Callable[
        [ChargerSession, str, str, str], Coroutine[Any, Any, None]
    ]
    absorb_configuration_reply: Callable[[ChargerSession, dict[str, Any]], None]

    @property
    def _requires_client_cert(self) -> bool:
        """Return whether chargers must present a client certificate."""
        return (
            self._ssl_context is not None
            and self._ssl_context.verify_mode == ssl.CERT_REQUIRED
        )

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

        # Mutual TLS (issue #1341): the CA vouches for the certificate, but
        # only its CN ties it to a charge point. Checked before anything
        # else so another charger's certificate cannot displace this one.
        if self._requires_client_cert:
            transport = request.transport
            common_name = peer_common_name(
                transport.get_extra_info("peercert") if transport else None
            )
            if not cpid_matches_common_name(cpid, common_name):
                if not self._allow_cn_mismatch:
                    _LOGGER.warning(
                        "OCPP %s: refused — client certificate CN %r does not "
                        "match the charge point identity",
                        cpid,
                        common_name,
                    )
                    raise web.HTTPForbidden(
                        text="Client certificate does not match the charge point "
                        "identity"
                    )
                # Allowed by the user, but it means any certificate from the
                # client CA can connect under this CPID, so say so each time.
                _LOGGER.warning(
                    "OCPP %s: client certificate CN %r does not match the charge "
                    "point identity; accepted because CN mismatches are allowed",
                    cpid,
                    common_name,
                )

        # A reconnect under the same CPID must not silently orphan a still-
        # open previous WebSocket — close it before replacing (issue #892).
        existing = self._chargers.get(cpid)
        if existing is not None and existing.websocket is not None:
            _LOGGER.warning(
                "OCPP charger %s reconnecting — closing previous WebSocket", cpid
            )
            try:
                await existing.websocket.close()
            except Exception:
                _LOGGER.debug(
                    "Error closing previous WebSocket for %s — ignoring", cpid
                )

        # heartbeat= makes aiohttp auto-ping the peer and close the
        # connection if no pong arrives — detects a silently dead
        # connection without waiting on OCPP's own Heartbeat (issue #892).
        # Echo back the OCPP subprotocol. OCPP-J 1.6 §3.1.2 has the charge
        # point offer "ocpp1.6" in Sec-WebSocket-Protocol and *requires* the
        # central system to select it; a client that gets no subprotocol
        # back is entitled to close the connection immediately. Without
        # this, aiohttp logged "Client protocols ['ocpp1.6'] don't overlap
        # server-known ones ()" on every connect and completed the
        # handshake with no subprotocol at all — tolerated by this charger's
        # firmware, but a stricter client would simply refuse to talk.
        ws = web.WebSocketResponse(
            protocols=(_OCPP_SUBPROTOCOL,), heartbeat=_WS_HEARTBEAT_INTERVAL_S
        )
        await ws.prepare(request)
        if ws.ws_protocol != _OCPP_SUBPROTOCOL:
            # Not fatal — a client that offered nothing still works — but
            # worth surfacing, since a mismatch here explains a charger
            # that connects and then immediately drops.
            _LOGGER.warning(
                "OCPP %s: no '%s' subprotocol negotiated (got %r) — a strict "
                "charger may refuse the connection",
                cpid,
                _OCPP_SUBPROTOCOL,
                ws.ws_protocol,
            )

        session = ChargerSession(
            cpid=cpid,
            websocket=ws,
            connected_at=datetime.now(UTC),
        )
        self._chargers[cpid] = session
        await self._notify_significant_event()

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
            # The anti-flap state machine assumes one continuously-connected
            # charger — a stale "charging"/timer belief must not survive a
            # disconnect into whatever reconnects next (issue #892).
            self._reset_anti_flap_state()
            _LOGGER.info("OCPP charger %s session ended", cpid)
            await self._notify_significant_event()

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
                _LOGGER.debug(
                    "OCPP CALL from %s (id=%s, action=%s): %s",
                    session.cpid,
                    msg_id,
                    action,
                    payload,
                )
                await self._dispatch(session, msg_id, action, payload)
            elif msg_type == _CALLRESULT:
                # Response to an outbound HSEM call (RemoteStartTransaction,
                # SetChargingProfile, RemoteStopTransaction). The
                # ground-truth confirmation that a start/stop actually
                # happened is still the charger's own subsequent
                # StartTransaction/StopTransaction call (session.transaction_id
                # flipping non-None/None), retried on a cooldown by
                # update_charge_target() while it never arrives. But the
                # CALLRESULT's own "status" field (issue #906) is recorded
                # too — it's the charger's earliest signal that it rejected
                # a command outright, used to surface a diagnostic and to
                # retry a rejected SetChargingProfile without waiting on a
                # material target change.
                result_msg_id = msg[1]
                result_payload = msg[2] if isinstance(msg[2], dict) else {}
                action = session.pending_calls.pop(result_msg_id, None)
                if action is not None:
                    status = result_payload.get("status")
                    if status and action in TRACKED_RESPONSE_ACTIONS:
                        session.last_call_status[action] = status
                        if status != "Accepted":
                            _LOGGER.warning(
                                "OCPP %s: charger responded '%s' to %s",
                                session.cpid,
                                status,
                                action,
                            )
                if action == "GetConfiguration":
                    self.absorb_configuration_reply(session, result_payload)
                if action in DIAGNOSTIC_ACTIONS:
                    # The whole point of these calls is their answer, so it
                    # must be readable without turning on DEBUG.
                    _LOGGER.warning(
                        "OCPP %s: %s reply: %s",
                        session.cpid,
                        action,
                        result_payload,
                    )
                _LOGGER.debug(
                    "OCPP CALLRESULT from %s (id=%s, action=%s): %s",
                    session.cpid,
                    result_msg_id,
                    action,
                    result_payload,
                )
            elif msg_type == _CALLERROR:
                # Resolve which request errored — a bare id is unreadable,
                # and "the charger rejected exactly this" is the whole
                # value of the message (issue #920).
                error_action = session.pending_calls.pop(msg[1], None)
                _LOGGER.warning(
                    "OCPP CALLERROR from %s (id=%s, action=%s): %s",
                    session.cpid,
                    msg[1],
                    error_action,
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
            "DataTransfer": self._handle_data_transfer,
            "FirmwareStatusNotification": self._handle_firmware_status,
            "DiagnosticsStatusNotification": self._handle_diagnostics_status,
        }

        # Every CALL gets a reply (issue #1390): NotImplemented for an
        # action HSEM has no handler for, InternalError when one raises.
        handler = handlers.get(action) if isinstance(action, str) else None
        if handler is None:
            _LOGGER.info(
                "OCPP %s: unsupported action %s, answered NotImplemented",
                session.cpid,
                action,
            )
            await self._send_call_error(
                session, msg_id, "NotImplemented", f"{action} is not supported"
            )
            return
        try:
            response = await handler(session, payload)
        except Exception:
            _LOGGER.exception("OCPP %s: %s handler failed", session.cpid, action)
            await self._send_call_error(
                session, msg_id, "InternalError", f"{action} failed"
            )
            return
        await self._send_response(session, msg_id, response)

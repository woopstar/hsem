"""Tests for the OCPP server's WebSocket transport layer.

The message loop sits between an unmanaged device on the LAN and HSEM's EV
planning, so it has to survive whatever the charger sends: a reconnect that
orphans the previous socket, a frame that is not JSON, a JSON payload that is
not an OCPP envelope, and a peer that drops mid-session. None of those may
take the server down or leave a stale session behind.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.models.ocpp_session import ChargerSession

_MODULE = "custom_components.hsem.custom_sensors.ocpp_server"
_OCPP_SUBPROTOCOL = "ocpp1.6"


@pytest.fixture
def server() -> OCPPServer:
    """Return an OCPP server with no anti-flap delay."""
    return OCPPServer(
        hass=MagicMock(),
        host="127.0.0.1",
        port=19001,
        start_window_s=0,
        stop_window_s=0,
    )


class _FakeWebSocket:
    """A stand-in for ``web.WebSocketResponse`` driven by a fixed script."""

    def __init__(
        self,
        messages: list[Any] | None = None,
        *,
        protocol: str | None = _OCPP_SUBPROTOCOL,
        iteration_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._messages = messages or []
        self.ws_protocol = protocol
        self._iteration_error = iteration_error
        self._close_error = close_error
        self.closed = False
        self.prepared_with: Any = None

    async def prepare(self, request: Any) -> None:
        """Record the handshake instead of performing it."""
        self.prepared_with = request

    async def close(self) -> None:
        """Close the socket, or fail the way a dead peer does."""
        if self._close_error is not None:
            raise self._close_error
        self.closed = True

    def exception(self) -> BaseException:
        """Return the transport error aiohttp would report."""
        return RuntimeError("transport error")

    async def _iterate(self) -> AsyncIterator[Any]:
        if self._iteration_error is not None:
            raise self._iteration_error
        for message in self._messages:
            yield message

    def __aiter__(self) -> AsyncIterator[Any]:
        """Iterate the scripted frames."""
        return self._iterate()


def _text(payload: Any) -> MagicMock:
    """Return a TEXT frame carrying *payload* as JSON."""
    return MagicMock(type=web.WSMsgType.TEXT, data=json.dumps(payload))


def _request(path: str = "/CP-1/") -> MagicMock:
    """Return a fake aiohttp request for *path*."""
    request = MagicMock()
    request.path = path
    request.remote = "192.168.1.50"
    return request


async def _handle(server: OCPPServer, ws: _FakeWebSocket, path: str = "/CP-1/") -> None:
    """Run the charger handler against *ws*."""
    with patch(f"{_MODULE}.web.WebSocketResponse", return_value=ws):
        await server._handle_charger(_request(path))


def _session(cpid: str = "CP-1", ws: Any = None) -> ChargerSession:
    """Return a charger session bound to *ws*."""
    return ChargerSession(
        cpid=cpid, websocket=ws or AsyncMock(), connected_at=datetime.now(UTC)
    )


class TestServerProperties:
    """The bind address and listening state are exposed for diagnostics."""

    def test_the_configured_endpoint_is_reported(self, server: OCPPServer) -> None:
        """Host and port come straight from the config entry."""
        assert server.host == "127.0.0.1"
        assert server.port == 19001

    def test_listening_follows_the_bound_site(self, server: OCPPServer) -> None:
        """``is_listening`` is true only while the aiohttp site exists."""
        assert server.is_listening is False

        server._site = MagicMock()

        assert server.is_listening is True


class TestStop:
    """Unloading must leave no session behind, however the socket behaves."""

    @pytest.mark.asyncio
    async def test_a_socket_that_cannot_be_closed_is_still_dropped(
        self, server: OCPPServer
    ) -> None:
        """A dead peer must not block the unload or keep its session alive."""
        ws = _FakeWebSocket(close_error=ConnectionResetError("peer gone"))
        server._chargers["CP-1"] = _session(ws=ws)
        server._site = AsyncMock()
        server._runner = AsyncMock()

        with patch.object(server, "release_charging_profiles", AsyncMock()) as release:
            await server.stop()

        release.assert_awaited_once()
        assert server._chargers == {}
        assert server.is_listening is False
        assert server._runner is None

    @pytest.mark.asyncio
    async def test_a_session_without_a_socket_is_dropped(
        self, server: OCPPServer
    ) -> None:
        """A half-registered session has nothing to close."""
        server._chargers["CP-1"] = _session(ws=None)

        with patch.object(server, "release_charging_profiles", AsyncMock()):
            await server.stop()

        assert server._chargers == {}


class TestHandleCharger:
    """One CPID owns at most one live WebSocket."""

    @pytest.mark.asyncio
    async def test_a_reconnect_closes_the_previous_socket(
        self, server: OCPPServer
    ) -> None:
        """A reconnecting charger must not orphan its old socket (issue #892)."""
        previous = _FakeWebSocket()
        server._chargers["CP-1"] = _session(ws=previous)

        await _handle(server, _FakeWebSocket())

        assert previous.closed is True

    @pytest.mark.asyncio
    async def test_a_previous_socket_that_cannot_be_closed_is_ignored(
        self, server: OCPPServer
    ) -> None:
        """A stale socket that errors on close does not block the reconnect."""
        previous = _FakeWebSocket(close_error=RuntimeError("already gone"))
        server._chargers["CP-1"] = _session(ws=previous)

        with patch(f"{_MODULE}._LOGGER") as logger:
            await _handle(server, _FakeWebSocket())

        assert any(
            "Error closing previous WebSocket" in str(call.args[0])
            for call in logger.debug.call_args_list
        )

    @pytest.mark.asyncio
    async def test_a_missing_subprotocol_is_warned_about(
        self, server: OCPPServer
    ) -> None:
        """A handshake without ``ocpp1.6`` explains a charger that drops."""
        with patch(f"{_MODULE}._LOGGER") as logger:
            await _handle(server, _FakeWebSocket(protocol=None))

        assert any(
            "subprotocol negotiated" in str(call.args[0])
            for call in logger.warning.call_args_list
        )

    @pytest.mark.asyncio
    async def test_text_frames_are_dispatched(self, server: OCPPServer) -> None:
        """Each TEXT frame reaches the OCPP message handler."""
        ws = _FakeWebSocket([_text([2, "1", "Heartbeat", {}])])

        with patch.object(server, "_handle_message", AsyncMock()) as handle:
            await _handle(server, ws)

        handle.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_transport_error_frame_is_logged(self, server: OCPPServer) -> None:
        """An ERROR frame is reported with the transport's own exception."""
        ws = _FakeWebSocket([MagicMock(type=web.WSMsgType.ERROR, data=None)])

        with patch(f"{_MODULE}._LOGGER") as logger:
            await _handle(server, ws)

        assert any(
            "WebSocket error for charger" in str(call.args[0])
            for call in logger.error.call_args_list
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(ConnectionResetError("peer reset"), id="reset"),
            pytest.param(asyncio.CancelledError(), id="cancelled"),
        ],
    )
    async def test_a_dropped_peer_ends_the_session_cleanly(
        self, server: OCPPServer, error: BaseException
    ) -> None:
        """A disconnect clears the session and the anti-flap belief."""
        with patch.object(server, "_reset_anti_flap_state") as reset:
            await _handle(server, _FakeWebSocket(iteration_error=error))

        assert server._chargers == {}
        reset.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_pathless_connection_uses_the_default_cpid(
        self, server: OCPPServer
    ) -> None:
        """A charger that connects to ``/`` still gets a session."""
        ws = _FakeWebSocket([_text([2, "1", "Heartbeat", {}])])
        seen: list[str] = []

        async def _record(session: ChargerSession, raw: str) -> None:
            seen.append(session.cpid)

        with patch.object(server, "_handle_message", _record):
            await _handle(server, ws, path="/")

        assert seen == ["default"]


class TestHandleMessage:
    """A frame the server cannot understand is logged, never raised."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param('{"action": "Heartbeat"}', id="not_a_list"),
            pytest.param("[2]", id="too_short"),
        ],
    )
    async def test_a_non_envelope_payload_is_rejected(
        self, server: OCPPServer, raw: str
    ) -> None:
        """Only a well-formed OCPP envelope is acted on."""
        with patch(f"{_MODULE}._LOGGER") as logger:
            await server._handle_message(_session(), raw)

        assert any(
            "Malformed OCPP message" in str(call.args[0])
            for call in logger.warning.call_args_list
        )

    @pytest.mark.asyncio
    async def test_invalid_json_is_rejected(self, server: OCPPServer) -> None:
        """A truncated frame is reported, not propagated."""
        with patch(f"{_MODULE}._LOGGER") as logger:
            await server._handle_message(_session(), "{not json")

        assert any(
            "Invalid JSON from charger" in str(call.args[0])
            for call in logger.warning.call_args_list
        )

    @pytest.mark.asyncio
    async def test_a_failing_handler_does_not_kill_the_loop(
        self, server: OCPPServer
    ) -> None:
        """An unexpected handler error is logged with its traceback."""
        with (
            patch.object(
                server, "_dispatch", AsyncMock(side_effect=RuntimeError("boom"))
            ),
            patch(f"{_MODULE}._LOGGER") as logger,
        ):
            await server._handle_message(
                _session(), json.dumps([2, "1", "Heartbeat", {}])
            )

        logger.exception.assert_called_once()

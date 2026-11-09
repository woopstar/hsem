"""Tests that every charger CALL gets an OCPP-J reply (issue #1390)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.models.ocpp_session import ChargerSession


@pytest.fixture
def ocpp_server() -> OCPPServer:
    """Return an OCPPServer that is never started."""
    return OCPPServer(hass=MagicMock(), host="127.0.0.1", port=19000)


@pytest.fixture
def charger_session() -> ChargerSession:
    """Return a charger session with a mocked websocket."""
    return ChargerSession(
        cpid="cp1", websocket=AsyncMock(), connected_at=datetime.now(UTC)
    )


def _sent(session: ChargerSession) -> list[Any]:
    """Return the single frame written to the session's websocket."""
    session.websocket.send_str.assert_awaited_once()
    frame: list[Any] = json.loads(session.websocket.send_str.await_args.args[0])
    return frame


class TestCallReplies:
    """Each CALL gets a CALLRESULT or a CALLERROR."""

    @pytest.mark.asyncio
    async def test_unknown_action_gets_not_implemented(
        self, ocpp_server, charger_session
    ):
        """An action without a handler is answered with NotImplemented."""
        raw = json.dumps([2, "m1", "SignCertificate", {"csr": "x"}])
        await ocpp_server._handle_message(charger_session, raw)
        frame = _sent(charger_session)
        assert frame[:3] == [4, "m1", "NotImplemented"]
        assert "SignCertificate" in frame[3]
        assert frame[4] == {}

    @pytest.mark.asyncio
    async def test_non_string_action_gets_not_implemented(
        self, ocpp_server, charger_session
    ):
        """A malformed, non-string action is answered, not crashed on."""
        raw = json.dumps([2, "m2", {"bad": 1}, {}])
        await ocpp_server._handle_message(charger_session, raw)
        assert _sent(charger_session)[:3] == [4, "m2", "NotImplemented"]

    @pytest.mark.asyncio
    async def test_handler_exception_gets_internal_error(
        self, ocpp_server, charger_session, caplog
    ):
        """A handler that raises is answered with InternalError and logged."""
        ocpp_server._handle_heartbeat = AsyncMock(side_effect=RuntimeError("boom"))
        raw = json.dumps([2, "m3", "Heartbeat", {}])
        with caplog.at_level(logging.ERROR):
            await ocpp_server._handle_message(charger_session, raw)
        frame = _sent(charger_session)
        assert frame[:3] == [4, "m3", "InternalError"]
        assert "Heartbeat handler failed" in caplog.text

    @pytest.mark.asyncio
    async def test_data_transfer_gets_unknown_vendor(
        self, ocpp_server, charger_session
    ):
        """DataTransfer is answered with UnknownVendorId."""
        raw = json.dumps([2, "m4", "DataTransfer", {"vendorId": "go-e"}])
        await ocpp_server._handle_message(charger_session, raw)
        assert _sent(charger_session) == [3, "m4", {"status": "UnknownVendorId"}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "action", ["FirmwareStatusNotification", "DiagnosticsStatusNotification"]
    )
    async def test_status_notifications_get_empty_reply(
        self, ocpp_server, charger_session, caplog, action
    ):
        """Firmware and diagnostics status get an empty CALLRESULT."""
        raw = json.dumps([2, "m5", action, {"status": "Idle"}])
        with caplog.at_level(
            logging.INFO,
            logger="custom_components.hsem.custom_sensors.ocpp_message_handlers",
        ):
            await ocpp_server._handle_message(charger_session, raw)
        assert _sent(charger_session) == [3, "m5", {}]
        assert "status Idle" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            (
                {
                    "type": "TamperDetectionActivated",
                    "timestamp": "2026-11-09T08:00:00Z",
                    "techInfo": "lid opened",
                },
                "security event TamperDetectionActivated at "
                "2026-11-09T08:00:00Z (techInfo: 'lid opened')",
            ),
            ({}, "security event None at None (techInfo: None)"),
        ],
    )
    async def test_security_event_gets_empty_reply(
        self, ocpp_server, charger_session, caplog, payload, expected
    ):
        """SecurityEventNotification is acknowledged and logged (issue #1406)."""
        raw = json.dumps([2, "m6", "SecurityEventNotification", payload])
        with caplog.at_level(
            logging.WARNING,
            logger="custom_components.hsem.custom_sensors.ocpp_message_handlers",
        ):
            await ocpp_server._handle_message(charger_session, raw)
        assert _sent(charger_session) == [3, "m6", {}]
        assert expected in caplog.text

    @pytest.mark.asyncio
    async def test_call_error_send_failure_is_logged(
        self, ocpp_server, charger_session, caplog
    ):
        """A failed CALLERROR write is logged, not raised."""
        charger_session.websocket.send_str.side_effect = ConnectionError("gone")
        with caplog.at_level(logging.ERROR):
            await ocpp_server._send_call_error(
                charger_session, "m6", "NotImplemented", "x"
            )
        assert "Failed to send OCPP CALLERROR" in caplog.text

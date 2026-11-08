"""Tests for OCPP session energy and the completed-session log (issue #1392)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.custom_sensors.ocpp_session_log import (
    MAX_SESSION_LOG,
    parse_meter_wh,
    session_energy_kwh,
)
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


class TestParseMeterWh:
    """Meter reading parsing."""

    @pytest.mark.parametrize("raw", [None, "abc", -1, float("nan"), float("inf")])
    def test_invalid_readings_are_none(self, raw):
        """Missing, non-numeric, negative or non-finite readings give None."""
        assert parse_meter_wh(raw) is None

    def test_valid_reading(self):
        """Numeric strings and numbers parse."""
        assert parse_meter_wh("1500") == pytest.approx(1500.0)


class TestSessionEnergy:
    """Live energy of the open transaction."""

    @pytest.mark.asyncio
    async def test_energy_from_meter_start_and_register(
        self, ocpp_server, charger_session
    ):
        """Session energy is the register minus meterStart."""
        await ocpp_server._handle_start_transaction(
            charger_session, {"connectorId": 1, "idTag": "HSEM", "meterStart": 10000}
        )
        charger_session.current_energy_wh = 13456.0
        assert session_energy_kwh(charger_session) == pytest.approx(3.456)

    def test_no_transaction_is_none(self, charger_session):
        """Without an open transaction there is no session energy."""
        charger_session.meter_start_wh = 100.0
        assert session_energy_kwh(charger_session) is None

    def test_adopted_transaction_is_none(self, charger_session):
        """A transaction adopted without meterStart reports None."""
        charger_session.transaction_id = 7
        charger_session.current_energy_wh = 5000.0
        assert session_energy_kwh(charger_session) is None

    def test_meter_reset_is_none(self, charger_session):
        """A register below meterStart gives None, never a negative value."""
        charger_session.transaction_id = 7
        charger_session.meter_start_wh = 5000.0
        charger_session.current_energy_wh = 10.0
        assert session_energy_kwh(charger_session) is None


class TestSessionLog:
    """Completed-session log filled on StopTransaction."""

    @pytest.mark.asyncio
    async def test_stop_appends_entry_and_clears_start(
        self, ocpp_server, charger_session
    ):
        """A stop logs energy, duration and reason, then clears start data."""
        reply = await ocpp_server._handle_start_transaction(
            charger_session, {"connectorId": 1, "idTag": "HSEM", "meterStart": 2000}
        )
        charger_session.transaction_started_at = datetime.now(UTC) - timedelta(
            minutes=90
        )
        await ocpp_server._handle_stop_transaction(
            charger_session,
            {
                "transactionId": reply["transactionId"],
                "meterStop": 14500,
                "reason": "EVDisconnected",
            },
        )
        (entry,) = ocpp_server.completed_sessions
        assert entry["cpid"] == "cp1"
        assert entry["transaction_id"] == reply["transactionId"]
        assert entry["energy_kwh"] == pytest.approx(12.5)
        assert entry["duration_min"] == 90
        assert entry["reason"] == "EVDisconnected"
        assert entry["started_at"] is not None
        assert charger_session.meter_start_wh is None
        assert charger_session.transaction_started_at is None

    @pytest.mark.asyncio
    async def test_stop_of_unknown_start_logs_none(self, ocpp_server, charger_session):
        """A stop with no recorded start logs unknown energy and duration."""
        await ocpp_server._handle_stop_transaction(
            charger_session, {"transactionId": 3, "meterStop": 900}
        )
        (entry,) = ocpp_server.completed_sessions
        assert entry["energy_kwh"] is None
        assert entry["duration_min"] is None
        assert entry["started_at"] is None
        assert entry["reason"] == "Local"

    @pytest.mark.asyncio
    async def test_log_is_bounded(self, ocpp_server, charger_session):
        """Only the newest MAX_SESSION_LOG sessions are kept."""
        for tx in range(MAX_SESSION_LOG + 5):
            await ocpp_server._handle_stop_transaction(
                charger_session, {"transactionId": tx, "meterStop": 0}
            )
        log = ocpp_server.completed_sessions
        assert len(log) == MAX_SESSION_LOG
        assert log[-1]["transaction_id"] == MAX_SESSION_LOG + 4

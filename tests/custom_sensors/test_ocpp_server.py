"""Tests for the embedded OCPP 1.6 WebSocket server (issue #603).

Covers:
- BootNotification handling
- Heartbeat handling
- StatusNotification state transitions
- MeterValues parsing
- SetChargingProfile message construction
- Session lifecycle (connect → charge → disconnect)
- Server start/stop
- Anti-flap start/stop window logic
- Unknown action handling
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.models.ocpp_session import ChargerSession

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_hass():
    """Return a mock Home Assistant instance."""
    return MagicMock()


@pytest.fixture
def ocpp_server(mock_hass):
    """Return an OCPPServer with short anti-flap windows for faster tests."""
    return OCPPServer(
        hass=mock_hass,
        host="127.0.0.1",
        port=19000,
        start_window_s=0,
        stop_window_s=0,
    )


@pytest.fixture
def charger_session():
    """Return a minimal charger session for testing handlers."""
    ws = AsyncMock()
    session = ChargerSession(
        cpid="test-cpid",
        websocket=ws,
        connected_at=datetime.now(UTC),
    )
    return session


# ---------------------------------------------------------------------------
# BootNotification tests
# ---------------------------------------------------------------------------


class TestBootNotification:
    """Tests for BootNotification OCPP message handler."""

    @pytest.mark.asyncio
    async def test_boot_notification_accepted(self, ocpp_server, charger_session):
        """BootNotification should record charger info and return Accepted."""
        payload = {
            "chargePointVendor": "TestVendor",
            "chargePointModel": "TestModel",
            "firmwareVersion": "1.2.3",
            "chargePointSerialNumber": "SN12345",
        }
        result = await ocpp_server._handle_boot_notification(charger_session, payload)
        assert result["status"] == "Accepted"
        assert result["interval"] == 300
        assert "currentTime" in result
        assert charger_session.vendor == "TestVendor"
        assert charger_session.model == "TestModel"
        assert charger_session.firmware == "1.2.3"
        assert charger_session.serial == "SN12345"

    @pytest.mark.asyncio
    async def test_boot_notification_minimal(self, ocpp_server, charger_session):
        """BootNotification with minimal payload should still work."""
        payload = {}
        result = await ocpp_server._handle_boot_notification(charger_session, payload)
        assert result["status"] == "Accepted"
        assert charger_session.vendor == ""
        assert charger_session.model == ""


# ---------------------------------------------------------------------------
# Heartbeat tests
# ---------------------------------------------------------------------------


class TestHeartbeat:
    """Tests for Heartbeat OCPP message handler."""

    @pytest.mark.asyncio
    async def test_heartbeat_updates_timestamp(self, ocpp_server, charger_session):
        """Heartbeat should update last_heartbeat and return currentTime."""
        assert charger_session.last_heartbeat is None
        payload = {}
        result = await ocpp_server._handle_heartbeat(charger_session, payload)
        assert "currentTime" in result
        assert charger_session.last_heartbeat is not None

    @pytest.mark.asyncio
    async def test_heartbeat_timestamp_is_recent(self, ocpp_server, charger_session):
        """Heartbeat response should contain a recent timestamp."""
        before = datetime.now(UTC) - timedelta(seconds=10)
        result = await ocpp_server._handle_heartbeat(charger_session, {})
        ts_str = result["currentTime"]
        parsed = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        assert parsed > before


# ---------------------------------------------------------------------------
# StatusNotification tests
# ---------------------------------------------------------------------------


class TestStatusNotification:
    """Tests for StatusNotification OCPP message handler."""

    @pytest.mark.asyncio
    async def test_status_notification_updates_status(
        self, ocpp_server, charger_session
    ):
        """StatusNotification should update the session status."""
        assert charger_session.status == "Available"
        payload = {"connectorId": 1, "status": "Charging"}
        result = await ocpp_server._handle_status_notification(charger_session, payload)
        assert result == {}  # StatusNotification expects empty CALLRESULT
        assert charger_session.status == "Charging"

    @pytest.mark.asyncio
    async def test_status_notification_state_transitions(
        self, ocpp_server, charger_session
    ):
        """StatusNotification should handle multiple state transitions."""
        states = ["Preparing", "Charging", "Finishing", "Available"]
        for state in states:
            await ocpp_server._handle_status_notification(
                charger_session, {"status": state}
            )
            assert charger_session.status == state


# ---------------------------------------------------------------------------
# MeterValues tests
# ---------------------------------------------------------------------------


class TestMeterValues:
    """Tests for MeterValues OCPP message handler."""

    @pytest.mark.asyncio
    async def test_meter_values_power_parsing(self, ocpp_server, charger_session):
        """MeterValues should parse Power.Active.Import."""
        payload = {
            "connectorId": 1,
            "meterValue": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "sampledValue": [
                        {
                            "measurand": "Power.Active.Import",
                            "value": "7200.0",
                            "unit": "W",
                        }
                    ],
                }
            ],
        }
        result = await ocpp_server._handle_meter_values(charger_session, payload)
        assert result == {}
        assert charger_session.current_power_w == 7200.0

    @pytest.mark.asyncio
    async def test_meter_values_energy_parsing(self, ocpp_server, charger_session):
        """MeterValues should parse Energy.Active.Import.Register."""
        payload = {
            "connectorId": 1,
            "meterValue": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "sampledValue": [
                        {
                            "measurand": "Energy.Active.Import.Register",
                            "value": "15000.0",
                            "unit": "Wh",
                        }
                    ],
                }
            ],
        }
        await ocpp_server._handle_meter_values(charger_session, payload)
        assert charger_session.current_energy_wh == 15000.0

    @pytest.mark.asyncio
    async def test_meter_values_unlabelled_power(self, ocpp_server, charger_session):
        """MeterValues should parse power from unlabelled fields with W unit."""
        payload = {
            "connectorId": 1,
            "meterValue": [{"sampledValue": [{"value": "3600.0", "unit": "W"}]}],
        }
        await ocpp_server._handle_meter_values(charger_session, payload)
        assert charger_session.current_power_w == 3600.0

    @pytest.mark.asyncio
    async def test_meter_values_invalid_number(self, ocpp_server, charger_session):
        """MeterValues with non-numeric values should be ignored gracefully."""
        initial_power = charger_session.current_power_w
        payload = {
            "connectorId": 1,
            "meterValue": [
                {
                    "sampledValue": [
                        {"measurand": "Power.Active.Import", "value": "not-a-number"}
                    ]
                }
            ],
        }
        await ocpp_server._handle_meter_values(charger_session, payload)
        assert charger_session.current_power_w == initial_power


# ---------------------------------------------------------------------------
# Authorize tests
# ---------------------------------------------------------------------------


class TestAuthorize:
    """Tests for Authorize OCPP message handler."""

    @pytest.mark.asyncio
    async def test_authorize_always_accepted(self, ocpp_server, charger_session):
        """Authorize should always return Accepted (LAN-only, no auth)."""
        result = await ocpp_server._handle_authorize(
            charger_session, {"idTag": "test-tag"}
        )
        assert result["idTagInfo"]["status"] == "Accepted"


# ---------------------------------------------------------------------------
# StartTransaction / StopTransaction tests
# ---------------------------------------------------------------------------


class TestTransaction:
    """Tests for StartTransaction and StopTransaction handlers."""

    @pytest.mark.asyncio
    async def test_start_transaction_records_id(self, ocpp_server, charger_session):
        """StartTransaction should record the transaction ID."""
        assert charger_session.transaction_id is None
        result = await ocpp_server._handle_start_transaction(
            charger_session, {"transactionId": 42}
        )
        assert result["transactionId"] == 42
        assert result["idTagInfo"]["status"] == "Accepted"
        assert charger_session.transaction_id == 42

    @pytest.mark.asyncio
    async def test_stop_transaction_clears_id(self, ocpp_server, charger_session):
        """StopTransaction should clear the transaction ID."""
        charger_session.transaction_id = 99
        result = await ocpp_server._handle_stop_transaction(
            charger_session, {"transactionId": 99}
        )
        assert result["idTagInfo"]["status"] == "Accepted"
        assert charger_session.transaction_id is None


# ---------------------------------------------------------------------------
# SetChargingProfile message construction
# ---------------------------------------------------------------------------


class TestSetChargingProfile:
    """Tests for SetChargingProfile message construction."""

    @pytest.mark.asyncio
    async def test_set_charging_profile_format(self, ocpp_server, charger_session):
        """SetChargingProfile should send a correctly structured OCPP message."""
        await ocpp_server._send_set_charging_profile(
            charger_session, max_power_w=3680, max_current_a=16
        )
        # Verify that a WebSocket send was called
        charger_session.websocket.send_str.assert_called_once()
        sent_data = charger_session.websocket.send_str.call_args[0][0]
        msg = json.loads(sent_data)
        assert msg[0] == 2  # CALL
        assert msg[2] == "SetChargingProfile"
        payload = msg[3]
        assert payload["connectorId"] == 1
        profile = payload["csChargingProfiles"]
        assert profile["chargingProfileId"] == 1
        assert profile["stackLevel"] == 0
        assert profile["chargingProfilePurpose"] == "TxDefaultProfile"
        schedule = profile["chargingSchedule"]
        assert schedule["chargingRateUnit"] == "A"
        assert schedule["chargingSchedulePeriod"][0]["limit"] == 16
        assert schedule["chargingSchedulePeriod"][0]["startPeriod"] == 0


# ---------------------------------------------------------------------------
# Session lifecycle tests
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    """Integration tests for OCPP session lifecycle."""

    def test_session_initial_state(self, charger_session):
        """A new ChargerSession should have default values."""
        assert charger_session.status == "Available"
        assert charger_session.vendor == ""
        assert charger_session.current_power_w == 0.0
        assert charger_session.transaction_id is None

    def test_ocpp_server_charger_registry(self, ocpp_server, charger_session):
        """The server should track charger sessions by CPID."""
        ocpp_server._chargers["test-cpid"] = charger_session
        assert "test-cpid" in ocpp_server._chargers
        assert ocpp_server.active_chargers == ["test-cpid"]
        assert len(ocpp_server.charger_sessions) == 1


# ---------------------------------------------------------------------------
# Unknown action tests
# ---------------------------------------------------------------------------


class TestUnknownAction:
    """Tests for handling unknown OCPP actions."""

    @pytest.mark.asyncio
    async def test_unknown_action_handled_gracefully(
        self, ocpp_server, charger_session
    ):
        """Unknown actions should return None without raising."""
        result = await ocpp_server._handle_unknown(charger_session, {"some": "data"})
        assert result is None


# ---------------------------------------------------------------------------
# Anti-flap logic tests
# ---------------------------------------------------------------------------


class TestAntiFlap:
    """Tests for anti-flap start/stop window logic."""

    @pytest.mark.asyncio
    async def test_immediate_start_with_zero_window(self, ocpp_server, charger_session):
        """With start_window_s=0, charge should start immediately."""
        ocpp_server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now
        )
        # Flap state should be "charging" because window is 0
        assert ocpp_server._flap_state == "charging"

    @pytest.mark.asyncio
    async def test_immediate_stop_with_zero_window(self, ocpp_server, charger_session):
        """With stop_window_s=0, charge should stop immediately."""
        ocpp_server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        ocpp_server._flap_state = "charging"
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=now
        )
        assert ocpp_server._flap_state == "idle"

    @pytest.mark.asyncio
    async def test_no_charger_connected(self, ocpp_server):
        """update_charge_target should be a no-op when charger is not connected."""
        now = datetime.now(UTC)
        # Should not raise
        await ocpp_server.update_charge_target(
            "no-such-cpid", target_power_kw=7.2, now=now
        )

    @pytest.mark.asyncio
    async def test_start_window_delay(self, mock_hass, charger_session):
        """With a non-zero start window, charge should not start until window elapses."""
        server = OCPPServer(
            hass=mock_hass,
            start_window_s=60,
            stop_window_s=0,
        )
        server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        # First call — should enter "starting" state, not "charging"
        await server.update_charge_target("test-cpid", target_power_kw=7.2, now=now)
        assert server._flap_state == "starting"

        # Call again before window elapses — still "starting"
        await server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now + timedelta(seconds=30)
        )
        assert server._flap_state == "starting"

        # Call after window elapses — should now be "charging"
        await server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now + timedelta(seconds=60)
        )
        assert server._flap_state == "charging"


# ---------------------------------------------------------------------------
# Server start/stop tests
# ---------------------------------------------------------------------------


class TestServerStartStop:
    """Tests for OCPP server lifecycle management."""

    @pytest.mark.asyncio
    async def test_server_start_stop(self, mock_hass):
        """Server should start and stop without errors."""
        server = OCPPServer(
            hass=mock_hass,
            host="127.0.0.1",
            port=19001,
        )
        await server.start()
        assert server._runner is not None
        assert server._site is not None

        await server.stop()
        assert server._site is None
        assert server._runner is None

    @pytest.mark.asyncio
    async def test_server_stop_clears_chargers(self, ocpp_server, charger_session):
        """Stopping the server should clear all charger sessions."""
        ocpp_server._chargers["test-cpid"] = charger_session
        assert len(ocpp_server._chargers) == 1
        await ocpp_server.stop()
        assert len(ocpp_server._chargers) == 0

    @pytest.mark.asyncio
    async def test_send_charging_profile_to_unknown_charger(self, ocpp_server):
        """Sending SetChargingProfile to unknown CPID should be a no-op."""
        # Should not raise
        await ocpp_server.send_set_charging_profile("unknown", max_power_w=3600)

    @pytest.mark.asyncio
    async def test_send_remote_stop_to_unknown_charger(self, ocpp_server):
        """Sending RemoteStopTransaction to unknown CPID should be a no-op."""
        await ocpp_server.send_remote_stop("unknown")

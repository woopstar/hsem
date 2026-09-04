"""Tests for the embedded OCPP 1.6 WebSocket server (issue #603).

Covers:
- BootNotification handling
- Heartbeat handling
- StatusNotification state transitions
- MeterValues parsing
- SetChargingProfile message construction
- RemoteStartTransaction dispatch (issue #892)
- Per-charger CPID path routing (issue #892)
- Session lifecycle (connect → charge → disconnect)
- Server start/stop
- Anti-flap start/stop window logic
- Unknown action handling
- Failed-send rollback, anti-flap-state diagnostic, disconnect reset,
  duplicate-CPID reconnect, and WebSocket heartbeat (issue #892 stability)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web

from custom_components.hsem.custom_sensors.ocpp_server import (
    _WS_HEARTBEAT_INTERVAL_S,
    CHARGER_STALL_THRESHOLD_S,
    OCPPServer,
    charger_appears_stalled,
)
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

    @pytest.mark.asyncio
    async def test_status_changed_at_set_on_first_status(
        self, ocpp_server, charger_session
    ):
        """status_changed_at is recorded the first time a status arrives (#894)."""
        assert charger_session.status_changed_at is None
        await ocpp_server._handle_status_notification(
            charger_session, {"status": "Charging"}
        )
        assert charger_session.status_changed_at is not None

    @pytest.mark.asyncio
    async def test_status_changed_at_updates_only_on_actual_change(
        self, ocpp_server, charger_session
    ):
        """Repeating the same status must not bump status_changed_at (#894)."""
        await ocpp_server._handle_status_notification(
            charger_session, {"status": "Charging"}
        )
        first_changed_at = charger_session.status_changed_at

        # Repeat the same status — timestamp must stay put.
        await ocpp_server._handle_status_notification(
            charger_session, {"status": "Charging"}
        )
        assert charger_session.status_changed_at == first_changed_at

        # An actual change updates the timestamp.
        await ocpp_server._handle_status_notification(
            charger_session, {"status": "SuspendedEVSE"}
        )
        assert charger_session.status_changed_at != first_changed_at


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
        result = await ocpp_server._handle_start_transaction(charger_session, {})
        assert result["transactionId"] == charger_session.transaction_id
        assert result["idTagInfo"]["status"] == "Accepted"
        assert charger_session.transaction_id is not None

    @pytest.mark.asyncio
    async def test_start_transaction_allocates_own_id(
        self, ocpp_server, charger_session
    ):
        """StartTransaction must not trust the charger's inbound transactionId.

        Real chargers never send this field on StartTransaction.req (OCPP
        1.6 §5.14 — allocating it is the CS's job); a spec-noncompliant
        charger sending one must not be echoed back, since that used to
        collapse every session to id 0 (issue #906).
        """
        result = await ocpp_server._handle_start_transaction(
            charger_session, {"transactionId": 999}
        )
        assert result["transactionId"] != 999
        assert charger_session.transaction_id != 999

    @pytest.mark.asyncio
    async def test_start_transaction_ids_are_unique(self, ocpp_server, mock_hass):
        """Two sessions started in sequence must get distinct transaction IDs.

        A charger that treats id 0 as an unset sentinel silently rejects
        RemoteStopTransaction — the bug this test guards against (issue
        #906): every session used to be assigned 0.
        """
        session_a = ChargerSession(cpid="cp-a", websocket=AsyncMock())
        session_b = ChargerSession(cpid="cp-b", websocket=AsyncMock())
        await ocpp_server._handle_start_transaction(session_a, {})
        await ocpp_server._handle_start_transaction(session_b, {})
        assert session_a.transaction_id is not None
        assert session_a.transaction_id != 0
        assert session_b.transaction_id is not None
        assert session_b.transaction_id != session_a.transaction_id

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
# last_requested_current_a tracking (issue #886)
# ---------------------------------------------------------------------------


class TestLastRequestedCurrentA:
    """Tests for the diagnostic last-requested-amps accessor."""

    def test_none_before_any_profile_sent(self, ocpp_server):
        """No profile has been sent yet, so the accessor returns None."""
        assert ocpp_server.last_requested_current_a is None

    @pytest.mark.asyncio
    async def test_reflects_last_sent_amps(self, ocpp_server, charger_session):
        """The accessor mirrors the amps in the last SetChargingProfile sent."""
        await ocpp_server._send_set_charging_profile(
            charger_session, max_power_w=3680, max_current_a=16
        )
        assert ocpp_server.last_requested_current_a == 16

    @pytest.mark.asyncio
    async def test_via_update_charge_target_with_explicit_current(
        self, ocpp_server, charger_session
    ):
        """update_charge_target() publishes the caller-supplied amps, not 16A."""
        ocpp_server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=1.38, max_current_a=6, now=now
        )
        assert ocpp_server.last_requested_current_a == 6

    @pytest.mark.asyncio
    async def test_reset_to_none_after_stop(self, ocpp_server, charger_session):
        """RemoteStopTransaction clears the last-requested-amps accessor."""
        ocpp_server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, max_current_a=32, now=now
        )
        assert ocpp_server.last_requested_current_a == 32
        ocpp_server._flap_state = "charging"
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=now
        )
        assert ocpp_server.last_requested_current_a is None


# ---------------------------------------------------------------------------
# RemoteStartTransaction dispatch (issue #892)
# ---------------------------------------------------------------------------


def _sent_actions(charger_session: ChargerSession) -> list[str]:
    """Return the OCPP action names sent over the mock WebSocket, in order."""
    return [
        json.loads(call.args[0])[2]
        for call in charger_session.websocket.send_str.call_args_list
    ]


class TestRemoteStartTransaction:
    """Tests for RemoteStartTransaction dispatch when starting a session."""

    @pytest.mark.asyncio
    async def test_sent_when_no_active_transaction(self, ocpp_server, charger_session):
        """A fresh session with no transaction gets RemoteStartTransaction."""
        assert charger_session.transaction_id is None
        ocpp_server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now
        )
        actions = _sent_actions(charger_session)
        assert "RemoteStartTransaction" in actions
        assert "SetChargingProfile" in actions
        # Authorize the session before configuring its charging ceiling.
        assert actions.index("RemoteStartTransaction") < actions.index(
            "SetChargingProfile"
        )

    @pytest.mark.asyncio
    async def test_skipped_when_transaction_already_active(
        self, ocpp_server, charger_session
    ):
        """An already-active transaction must never be re-authorized."""
        charger_session.transaction_id = 42
        ocpp_server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now
        )
        actions = _sent_actions(charger_session)
        assert "RemoteStartTransaction" not in actions
        assert "SetChargingProfile" in actions

    @pytest.mark.asyncio
    async def test_payload_includes_non_empty_id_tag(
        self, ocpp_server, charger_session
    ):
        """OCPP 1.6 requires a non-empty idTag on RemoteStartTransaction."""
        ocpp_server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now
        )
        for call in charger_session.websocket.send_str.call_args_list:
            msg = json.loads(call.args[0])
            if msg[2] == "RemoteStartTransaction":
                assert msg[3]["idTag"]
                break
        else:
            pytest.fail("RemoteStartTransaction was never sent")

    @pytest.mark.asyncio
    async def test_not_sent_when_stopping(self, ocpp_server, charger_session):
        """Stopping a charge must never trigger a RemoteStartTransaction."""
        ocpp_server._chargers["test-cpid"] = charger_session
        ocpp_server._flap_state = "charging"
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=now
        )
        assert "RemoteStartTransaction" not in _sent_actions(charger_session)


# ---------------------------------------------------------------------------
# RemoteStartTransaction retry while unconfirmed (issue #892)
# ---------------------------------------------------------------------------


class TestRemoteStartRetry:
    """Tests for retrying RemoteStartTransaction while unconfirmed."""

    def test_due_initially(self, ocpp_server):
        """With no prior attempt, a retry is immediately due."""
        assert ocpp_server._remote_start_due(datetime.now(UTC)) is True

    def test_not_due_within_cooldown(self, ocpp_server):
        """A retry is withheld until the cooldown has elapsed."""
        now = datetime.now(UTC)
        ocpp_server._last_remote_start_attempt = now
        assert ocpp_server._remote_start_due(now + timedelta(seconds=10)) is False

    def test_due_after_cooldown(self, ocpp_server):
        """A retry becomes due once the cooldown has elapsed."""
        now = datetime.now(UTC)
        ocpp_server._last_remote_start_attempt = now
        assert ocpp_server._remote_start_due(now + timedelta(seconds=61)) is True

    @pytest.mark.asyncio
    async def test_retries_after_cooldown_when_unconfirmed(
        self, ocpp_server, charger_session
    ):
        """An unconfirmed session retries RemoteStartTransaction on cooldown."""
        ocpp_server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now
        )
        assert _sent_actions(charger_session).count("RemoteStartTransaction") == 1

        # Still unconfirmed (charger_session.transaction_id stays None) —
        # before the cooldown elapses, must not retry yet.
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now + timedelta(seconds=30)
        )
        assert _sent_actions(charger_session).count("RemoteStartTransaction") == 1

        # After the cooldown, retry.
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now + timedelta(seconds=61)
        )
        assert _sent_actions(charger_session).count("RemoteStartTransaction") == 2

    @pytest.mark.asyncio
    async def test_no_retry_once_transaction_confirmed(
        self, ocpp_server, charger_session
    ):
        """A confirmed transaction must never be re-authorized, even later."""
        ocpp_server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now
        )
        assert _sent_actions(charger_session).count("RemoteStartTransaction") == 1

        # Charger confirms via its own StartTransaction call.
        charger_session.transaction_id = 7
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now + timedelta(seconds=120)
        )
        assert _sent_actions(charger_session).count("RemoteStartTransaction") == 1


# ---------------------------------------------------------------------------
# RemoteStopTransaction dispatch (issue #892)
# ---------------------------------------------------------------------------


class TestRemoteStopTransaction:
    """Tests for RemoteStopTransaction dispatch when stopping a session."""

    @pytest.mark.asyncio
    async def test_sent_with_transaction_id_when_active(
        self, ocpp_server, charger_session
    ):
        """An active transaction is stopped with its transactionId."""
        charger_session.transaction_id = 99
        await ocpp_server._send_remote_stop(charger_session)
        charger_session.websocket.send_str.assert_called_once()
        msg = json.loads(charger_session.websocket.send_str.call_args[0][0])
        assert msg[2] == "RemoteStopTransaction"
        assert msg[3] == {"transactionId": 99}

    @pytest.mark.asyncio
    async def test_skipped_without_mandatory_transaction_id(
        self, ocpp_server, charger_session
    ):
        """No active transaction means nothing to stop — and OCPP 1.6
        requires transactionId on RemoteStopTransaction, so sending an
        empty payload would be a schema violation. Must not be sent."""
        assert charger_session.transaction_id is None
        await ocpp_server._send_remote_stop(charger_session)
        charger_session.websocket.send_str.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_tracking_reset_even_when_skipped(
        self, ocpp_server, charger_session
    ):
        """last_requested_current_a still clears when the stop is skipped."""
        ocpp_server._last_sent_target = 3680.0
        ocpp_server._last_sent_current_a = 16
        await ocpp_server._send_remote_stop(charger_session)
        assert ocpp_server.last_requested_current_a is None

    @pytest.mark.asyncio
    async def test_via_update_charge_target_stopping_from_starting(
        self, mock_hass, charger_session
    ):
        """A target that flips to zero before the start window fires must
        not send a malformed RemoteStopTransaction — no session was ever
        started, so there is nothing to stop."""
        server = OCPPServer(hass=mock_hass, start_window_s=60, stop_window_s=0)
        server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        # Enters "starting" — window hasn't elapsed, nothing sent yet.
        await server.update_charge_target("test-cpid", target_power_kw=7.2, now=now)
        assert server._flap_state == "starting"
        charger_session.websocket.send_str.assert_not_called()

        # Target drops back to zero before the start window elapses.
        await server.update_charge_target("test-cpid", target_power_kw=0.0, now=now)
        assert server._flap_state == "idle"
        charger_session.websocket.send_str.assert_not_called()


# ---------------------------------------------------------------------------
# Per-charger CPID path routing (issue #892)
# ---------------------------------------------------------------------------


class TestCpidPathRouting:
    """Tests that any WebSocket path reaches the handler, not just '/'."""

    @pytest.mark.asyncio
    async def test_custom_cpid_path_registers_charger(self, mock_hass):
        """A charger connecting on a non-root path is registered under it."""
        server = OCPPServer(hass=mock_hass, host="127.0.0.1", port=19010)
        await server.start()
        try:
            async with (
                aiohttp.ClientSession() as client,
                client.ws_connect("ws://127.0.0.1:19010/222819"),
            ):
                await asyncio.sleep(0.05)
                assert "222819" in server.active_chargers
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_root_path_still_resolves_to_default(self, mock_hass):
        """A bare root-path connection still resolves to cpid 'default'."""
        server = OCPPServer(hass=mock_hass, host="127.0.0.1", port=19011)
        await server.start()
        try:
            async with (
                aiohttp.ClientSession() as client,
                client.ws_connect("ws://127.0.0.1:19011/"),
            ):
                await asyncio.sleep(0.05)
                assert "default" in server.active_chargers
        finally:
            await server.stop()


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
# CALLRESULT / CALLERROR message-type handling (issue #892)
# ---------------------------------------------------------------------------


class TestCallResultHandling:
    """Tests for correctly parsing replies to HSEM's own outbound calls.

    A CALLRESULT (``[3, id, payload]``) or CALLERROR
    (``[4, id, errorCode, errorDescription, errorDetails]``) used to be
    parsed as if it were a CALL, reading the payload/errorCode as an
    "action" and crashing with ``TypeError: unhashable type: 'dict'``
    deep inside ``_dispatch`` when a dict landed there — silently
    discarding every response to HSEM's own RemoteStartTransaction /
    SetChargingProfile / RemoteStopTransaction calls.
    """

    @pytest.mark.asyncio
    async def test_callresult_does_not_raise_or_respond(
        self, ocpp_server, charger_session
    ):
        """A CALLRESULT reply must not crash and must not trigger a response."""
        raw = json.dumps([3, "hsem-123", {"status": "Accepted"}])
        await ocpp_server._handle_message(charger_session, raw)
        charger_session.websocket.send_str.assert_not_called()

    @pytest.mark.asyncio
    async def test_callerror_does_not_raise_or_respond(
        self, ocpp_server, charger_session
    ):
        """A CALLERROR reply must not crash and must not trigger a response."""
        raw = json.dumps([4, "hsem-123", "NotSupported", "unsupported action", {}])
        await ocpp_server._handle_message(charger_session, raw)
        charger_session.websocket.send_str.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_message_type_does_not_raise(
        self, ocpp_server, charger_session
    ):
        """An out-of-range message type must not crash the handler."""
        raw = json.dumps([9, "hsem-123", {}])
        await ocpp_server._handle_message(charger_session, raw)
        charger_session.websocket.send_str.assert_not_called()

    @pytest.mark.asyncio
    async def test_genuine_call_still_dispatches_and_responds(
        self, ocpp_server, charger_session
    ):
        """A real CALL from the charger is unaffected by the type split."""
        raw = json.dumps([2, "charger-1", "Heartbeat", {}])
        await ocpp_server._handle_message(charger_session, raw)
        charger_session.websocket.send_str.assert_called_once()
        sent = json.loads(charger_session.websocket.send_str.call_args[0][0])
        assert sent[0] == 3  # CALLRESULT
        assert sent[1] == "charger-1"


# ---------------------------------------------------------------------------
# Wire-level DEBUG logging (issue #920)
# ---------------------------------------------------------------------------


class TestWireLevelDebugLogging:
    """Every incoming/outgoing OCPP CALL is logged at DEBUG for diagnosing
    why a charger silently doesn't respond to a start/stop as expected."""

    @pytest.mark.asyncio
    async def test_incoming_call_logged_with_action_and_payload(
        self, ocpp_server, charger_session, caplog
    ):
        """An inbound CALL's action and payload are logged before dispatch."""
        raw = json.dumps(
            [2, "charger-1", "StatusNotification", {"status": "Available"}]
        )
        # "custom_components.hsem" has its level explicitly set to WARNING by
        # HSEM_LOGGER at import time (utils/logger.py) — an ancestor with a
        # non-NOTSET level short-circuits Python's effective-level walk
        # before it ever reaches root, so raising only root's level here
        # would not actually let DEBUG through for this module's logger.
        with caplog.at_level(
            logging.DEBUG, logger="custom_components.hsem.custom_sensors.ocpp_server"
        ):
            await ocpp_server._handle_message(charger_session, raw)
        assert "StatusNotification" in caplog.text
        assert "Available" in caplog.text

    @pytest.mark.asyncio
    async def test_outgoing_call_logged_with_action_and_payload(
        self, ocpp_server, charger_session, caplog
    ):
        """An outbound CALL's action and payload are logged on send."""
        with caplog.at_level(
            logging.DEBUG, logger="custom_components.hsem.custom_sensors.ocpp_commands"
        ):
            await ocpp_server._send_call(
                charger_session, "SetChargingProfile", {"connectorId": 1}
            )
        assert "SetChargingProfile" in caplog.text
        assert "connectorId" in caplog.text


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

    @pytest.mark.asyncio
    async def test_send_remote_start_to_unknown_charger(self, ocpp_server):
        """Sending RemoteStartTransaction to unknown CPID should be a no-op."""
        result = await ocpp_server.send_remote_start("unknown")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_remote_start_bypasses_anti_flap(
        self, ocpp_server, charger_session
    ):
        """send_remote_start (issue #920) sends immediately, no start window wait."""
        ocpp_server._chargers["test-cpid"] = charger_session
        result = await ocpp_server.send_remote_start("test-cpid")
        assert result is True
        charger_session.websocket.send_str.assert_called_once()
        msg = json.loads(charger_session.websocket.send_str.call_args[0][0])
        assert msg[2] == "RemoteStartTransaction"
        assert msg[3] == {"idTag": "HSEM"}
        # Bypass API must not touch the anti-flap state machine.
        assert ocpp_server.anti_flap_state == "idle"


# ---------------------------------------------------------------------------
# Failed-send rollback (issue #892)
# ---------------------------------------------------------------------------


class TestFailedSendRollback:
    """A failed send must never be treated as if the command succeeded."""

    @pytest.mark.asyncio
    async def test_start_transition_not_committed_on_send_failure(
        self, ocpp_server, charger_session
    ):
        """A failed send keeps flap_state at 'starting', not 'charging'."""
        ocpp_server._chargers["test-cpid"] = charger_session
        charger_session.websocket.send_str.side_effect = ConnectionResetError()
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now
        )
        assert ocpp_server._flap_state == "starting"

    @pytest.mark.asyncio
    async def test_start_transition_committed_once_send_succeeds(
        self, ocpp_server, charger_session
    ):
        """After a failure, the next successful cycle commits normally."""
        ocpp_server._chargers["test-cpid"] = charger_session
        charger_session.websocket.send_str.side_effect = ConnectionResetError()
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now
        )
        assert ocpp_server._flap_state == "starting"

        charger_session.websocket.send_str.side_effect = None
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now + timedelta(seconds=1)
        )
        assert ocpp_server._flap_state == "charging"

    @pytest.mark.asyncio
    async def test_failed_profile_send_not_remembered_as_sent(
        self, ocpp_server, charger_session
    ):
        """A failed SetChargingProfile must not update last_requested_current_a."""
        charger_session.websocket.send_str.side_effect = ConnectionResetError()
        sent = await ocpp_server._send_set_charging_profile(
            charger_session, max_power_w=3680, max_current_a=16
        )
        assert sent is False
        assert ocpp_server.last_requested_current_a is None

    @pytest.mark.asyncio
    async def test_stop_transition_not_committed_on_send_failure(
        self, ocpp_server, charger_session
    ):
        """A failed RemoteStopTransaction keeps flap_state at 'stopping'."""
        ocpp_server._chargers["test-cpid"] = charger_session
        ocpp_server._flap_state = "charging"
        charger_session.transaction_id = 5
        charger_session.websocket.send_str.side_effect = ConnectionResetError()
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=now
        )
        assert ocpp_server._flap_state == "stopping"

    @pytest.mark.asyncio
    async def test_remote_start_send_failure_returns_false(
        self, ocpp_server, charger_session
    ):
        """_send_remote_start() reports failure without raising."""
        charger_session.websocket.send_str.side_effect = ConnectionResetError()
        sent = await ocpp_server._send_remote_start(charger_session)
        assert sent is False


# ---------------------------------------------------------------------------
# anti_flap_state diagnostic (issue #892)
# ---------------------------------------------------------------------------


class TestAntiFlapStateDiagnostic:
    """Tests for the anti_flap_state diagnostic property."""

    def test_initial_state_is_idle(self, ocpp_server):
        """A fresh server reports 'idle'."""
        assert ocpp_server.anti_flap_state == "idle"

    @pytest.mark.asyncio
    async def test_reflects_charging_state(self, ocpp_server, charger_session):
        """The property reflects the live state machine, not a snapshot."""
        ocpp_server._chargers["test-cpid"] = charger_session
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=datetime.now(UTC)
        )
        assert ocpp_server.anti_flap_state == "charging"


# ---------------------------------------------------------------------------
# Charger-stall diagnostics (issue #894)
# ---------------------------------------------------------------------------


class TestChargerAppearsStalled:
    """Tests for the pure charger_appears_stalled() helper."""

    def _session(
        self,
        transaction_id: int | None = 42,
        status: str = "SuspendedEVSE",
        status_changed_at: datetime | None = None,
    ) -> ChargerSession:
        if status_changed_at is None:
            status_changed_at = datetime.now(UTC) - timedelta(
                seconds=CHARGER_STALL_THRESHOLD_S + 1
            )
        return ChargerSession(
            cpid="test-cpid",
            transaction_id=transaction_id,
            status=status,
            status_changed_at=status_changed_at,
        )

    def test_stalled_when_over_threshold(self):
        """SuspendedEVSE held past the threshold with an open tx is stalled."""
        session = self._session()
        assert charger_appears_stalled(session, datetime.now(UTC)) is True

    def test_not_stalled_when_under_threshold(self):
        """A transient status flap under the threshold is not stalled."""
        now = datetime.now(UTC)
        session = self._session(status_changed_at=now - timedelta(seconds=5))
        assert charger_appears_stalled(session, now) is False

    def test_exactly_at_threshold_is_stalled(self):
        """The boundary is inclusive: >= threshold counts as stalled."""
        now = datetime.now(UTC)
        session = self._session(
            status_changed_at=now - timedelta(seconds=CHARGER_STALL_THRESHOLD_S)
        )
        assert charger_appears_stalled(session, now) is True

    def test_no_transaction_never_stalled(self):
        """Without an open transaction, nothing can be flagged as stalled."""
        session = self._session(transaction_id=None)
        assert charger_appears_stalled(session, datetime.now(UTC)) is False

    def test_suspended_ev_never_flagged(self):
        """SuspendedEV is EV-decided and must never be flagged, at any age."""
        session = self._session(
            status="SuspendedEV",
            status_changed_at=datetime.now(UTC) - timedelta(hours=1),
        )
        assert charger_appears_stalled(session, datetime.now(UTC)) is False

    @pytest.mark.parametrize("status", ["Faulted", "Unavailable"])
    def test_faulted_and_unavailable_flagged(self, status):
        """Faulted/Unavailable are unambiguous problems once past threshold."""
        session = self._session(status=status)
        assert charger_appears_stalled(session, datetime.now(UTC)) is True

    def test_charging_status_never_stalled(self):
        """A charger actively reporting 'Charging' is never stalled."""
        session = self._session(status="Charging")
        assert charger_appears_stalled(session, datetime.now(UTC)) is False

    def test_no_status_changed_at_never_stalled(self):
        """Without a recorded status_changed_at, there's nothing to compare."""
        session = ChargerSession(
            cpid="test-cpid",
            transaction_id=42,
            status="SuspendedEVSE",
            status_changed_at=None,
        )
        assert charger_appears_stalled(session, datetime.now(UTC)) is False

    def test_custom_threshold_respected(self):
        """A caller-supplied threshold overrides the module default."""
        now = datetime.now(UTC)
        session = self._session(status_changed_at=now - timedelta(seconds=10))
        assert charger_appears_stalled(session, now, threshold_s=5) is True
        assert charger_appears_stalled(session, now, threshold_s=20) is False


class TestIsStalledWiring:
    """Tests for is_stalled surfacing through update_charge_target()."""

    @pytest.mark.asyncio
    async def test_is_stalled_true_while_charging_and_stuck(
        self, ocpp_server, charger_session
    ):
        """A charging session stuck in SuspendedEVSE is surfaced as stalled."""
        ocpp_server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now
        )
        assert ocpp_server._flap_state == "charging"
        assert ocpp_server.is_stalled is False

        charger_session.transaction_id = 1
        charger_session.status = "SuspendedEVSE"
        charger_session.status_changed_at = now - timedelta(
            seconds=CHARGER_STALL_THRESHOLD_S + 1
        )
        await ocpp_server.update_charge_target(
            "test-cpid",
            target_power_kw=7.2,
            now=now + timedelta(seconds=1),
        )
        assert ocpp_server.is_stalled is True

    @pytest.mark.asyncio
    async def test_is_stalled_false_for_suspended_ev(
        self, ocpp_server, charger_session
    ):
        """SuspendedEV (EV-decided pause) is never surfaced as stalled."""
        ocpp_server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now
        )
        charger_session.transaction_id = 1
        charger_session.status = "SuspendedEV"
        charger_session.status_changed_at = now - timedelta(
            seconds=CHARGER_STALL_THRESHOLD_S + 1
        )
        await ocpp_server.update_charge_target(
            "test-cpid",
            target_power_kw=7.2,
            now=now + timedelta(seconds=1),
        )
        assert ocpp_server.is_stalled is False

    @pytest.mark.asyncio
    async def test_warning_logged_once_not_every_cycle(
        self, ocpp_server, charger_session, caplog
    ):
        """The stall warning is logged once, not on every subsequent cycle."""
        ocpp_server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now
        )
        charger_session.transaction_id = 1
        charger_session.status = "SuspendedEVSE"
        charger_session.status_changed_at = now - timedelta(
            seconds=CHARGER_STALL_THRESHOLD_S + 1
        )

        with caplog.at_level(logging.WARNING):
            await ocpp_server.update_charge_target(
                "test-cpid",
                target_power_kw=7.2,
                now=now + timedelta(seconds=1),
            )
            await ocpp_server.update_charge_target(
                "test-cpid",
                target_power_kw=7.2,
                now=now + timedelta(seconds=2),
            )

        stall_warnings = [r for r in caplog.records if "appears stalled" in r.message]
        assert len(stall_warnings) == 1

    @pytest.mark.asyncio
    async def test_is_stalled_resets_when_status_recovers(
        self, ocpp_server, charger_session
    ):
        """Once the charger reports 'Charging' again, stalled clears."""
        ocpp_server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now
        )
        charger_session.transaction_id = 1
        charger_session.status = "SuspendedEVSE"
        charger_session.status_changed_at = now - timedelta(
            seconds=CHARGER_STALL_THRESHOLD_S + 1
        )
        await ocpp_server.update_charge_target(
            "test-cpid",
            target_power_kw=7.2,
            now=now + timedelta(seconds=1),
        )
        assert ocpp_server.is_stalled is True

        charger_session.status = "Charging"
        charger_session.status_changed_at = now + timedelta(seconds=2)
        await ocpp_server.update_charge_target(
            "test-cpid",
            target_power_kw=7.2,
            now=now + timedelta(seconds=2),
        )
        assert ocpp_server.is_stalled is False

    @pytest.mark.asyncio
    async def test_no_stall_before_threshold_elapsed(
        self, ocpp_server, charger_session
    ):
        """A brief SuspendedEVSE flap under the threshold is not flagged."""
        ocpp_server._chargers["test-cpid"] = charger_session
        now = datetime.now(UTC)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=now
        )
        charger_session.transaction_id = 1
        charger_session.status = "SuspendedEVSE"
        charger_session.status_changed_at = now

        await ocpp_server.update_charge_target(
            "test-cpid",
            target_power_kw=7.2,
            now=now + timedelta(seconds=5),
        )
        assert ocpp_server.is_stalled is False


# ---------------------------------------------------------------------------
# Reset anti-flap state on disconnect (issue #892)
# ---------------------------------------------------------------------------


class TestResetAntiFlapStateOnDisconnect:
    """Tests for resetting anti-flap bookkeeping when a charger disconnects."""

    def test_reset_clears_all_bookkeeping(self, ocpp_server):
        """_reset_anti_flap_state() returns every field to its idle default."""
        ocpp_server._flap_state = "charging"
        ocpp_server._target_entered_at = datetime.now(UTC)
        ocpp_server._zero_entered_at = datetime.now(UTC)
        ocpp_server._target_power_w = 7200.0
        ocpp_server._last_sent_target = 7200.0
        ocpp_server._last_sent_current_a = 32
        ocpp_server._last_remote_start_attempt = datetime.now(UTC)
        ocpp_server._last_remote_stop_attempt = datetime.now(UTC)
        ocpp_server._last_profile_retry_attempt = datetime.now(UTC)
        ocpp_server._stalled = True
        ocpp_server._stall_logged = True

        ocpp_server._reset_anti_flap_state()

        assert ocpp_server._flap_state == "idle"
        assert ocpp_server._target_entered_at is None
        assert ocpp_server._zero_entered_at is None
        assert ocpp_server._target_power_w == 0.0
        assert ocpp_server.last_requested_current_a is None
        assert ocpp_server._last_remote_start_attempt is None
        assert ocpp_server._last_remote_stop_attempt is None
        assert ocpp_server._last_profile_retry_attempt is None
        assert ocpp_server.is_stalled is False

    @pytest.mark.asyncio
    async def test_disconnect_resets_state_via_real_connection(self, mock_hass):
        """A real WebSocket disconnect triggers the reset in production code."""
        server = OCPPServer(hass=mock_hass, host="127.0.0.1", port=19015)
        await server.start()
        try:
            async with (
                aiohttp.ClientSession() as client,
                client.ws_connect("ws://127.0.0.1:19015/reset-test"),
            ):
                await asyncio.sleep(0.05)
                server._flap_state = "charging"
            await asyncio.sleep(0.05)
            assert server._flap_state == "idle"
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# Duplicate-CPID reconnect closes the stale WebSocket (issue #892)
# ---------------------------------------------------------------------------


class TestDuplicateCpidReconnect:
    """Tests that a reconnect under the same CPID closes the old socket."""

    @pytest.mark.asyncio
    async def test_reconnect_closes_previous_websocket(self, mock_hass):
        """The previous session's websocket.close() is awaited on reconnect."""
        server = OCPPServer(hass=mock_hass, host="127.0.0.1", port=19016)
        await server.start()
        try:
            stale_ws = AsyncMock()
            server._chargers["dup"] = ChargerSession(
                cpid="dup", websocket=stale_ws, connected_at=datetime.now(UTC)
            )
            async with (
                aiohttp.ClientSession() as client,
                client.ws_connect("ws://127.0.0.1:19016/dup"),
            ):
                await asyncio.sleep(0.05)
                stale_ws.close.assert_awaited_once()
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# WebSocket-level heartbeat (issue #892)
# ---------------------------------------------------------------------------


class TestWebSocketHeartbeat:
    """Tests that the WebSocket response is configured with a heartbeat."""

    @pytest.mark.asyncio
    async def test_heartbeat_interval_configured(self, mock_hass):
        """web.WebSocketResponse() is constructed with the heartbeat interval."""
        server = OCPPServer(hass=mock_hass, host="127.0.0.1", port=19017)
        await server.start()
        try:
            with patch(
                "custom_components.hsem.custom_sensors.ocpp_server.web"
                ".WebSocketResponse",
                wraps=web.WebSocketResponse,
            ) as mock_ws_cls:
                async with (
                    aiohttp.ClientSession() as client,
                    client.ws_connect("ws://127.0.0.1:19017/hb-test"),
                ):
                    await asyncio.sleep(0.05)
                mock_ws_cls.assert_called_once()
                _, kwargs = mock_ws_cls.call_args
                assert kwargs.get("heartbeat") == _WS_HEARTBEAT_INTERVAL_S
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# Significant-event notification (issue #908)
# ---------------------------------------------------------------------------


class TestNotifySignificantEvent:
    """Tests for the on_significant_event callback plumbing.

    Verifies HSEM notifies promptly on state transitions worth reflecting
    in HA right away (connect, disconnect, status change, confirmed
    start/stop) and deliberately does NOT notify on high-frequency
    messages that carry no transition information (MeterValues,
    Heartbeat).
    """

    @pytest.mark.asyncio
    async def test_noop_when_no_callback(self, ocpp_server):
        """Without a callback configured, notifying is a safe no-op."""
        await ocpp_server._notify_significant_event()

    @pytest.mark.asyncio
    async def test_invokes_configured_callback(self, mock_hass):
        """A configured callback is awaited."""
        callback = AsyncMock()
        server = OCPPServer(hass=mock_hass, on_significant_event=callback)
        await server._notify_significant_event()
        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_status_change_triggers_notification(
        self, mock_hass, charger_session
    ):
        """A StatusNotification status change notifies significant-event."""
        callback = AsyncMock()
        server = OCPPServer(hass=mock_hass, on_significant_event=callback)
        await server._handle_status_notification(
            charger_session, {"status": "Preparing"}
        )
        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_repeated_status_does_not_trigger_notification(
        self, mock_hass, charger_session
    ):
        """A repeated StatusNotification with the same status is a no-op."""
        callback = AsyncMock()
        server = OCPPServer(hass=mock_hass, on_significant_event=callback)
        charger_session.status = "Preparing"
        await server._handle_status_notification(
            charger_session, {"status": "Preparing"}
        )
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_transaction_triggers_notification(
        self, mock_hass, charger_session
    ):
        """A StartTransaction notifies significant-event."""
        callback = AsyncMock()
        server = OCPPServer(hass=mock_hass, on_significant_event=callback)
        await server._handle_start_transaction(charger_session, {"transactionId": 1})
        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_transaction_triggers_notification(
        self, mock_hass, charger_session
    ):
        """A StopTransaction notifies significant-event."""
        callback = AsyncMock()
        server = OCPPServer(hass=mock_hass, on_significant_event=callback)
        charger_session.transaction_id = 1
        await server._handle_stop_transaction(charger_session, {"transactionId": 1})
        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_meter_values_does_not_trigger_notification(
        self, mock_hass, charger_session
    ):
        """MeterValues must not trigger a refresh — too frequent, no
        state-transition information."""
        callback = AsyncMock()
        server = OCPPServer(hass=mock_hass, on_significant_event=callback)
        await server._handle_meter_values(
            charger_session,
            {
                "connectorId": 1,
                "meterValue": [
                    {
                        "sampledValue": [
                            {"measurand": "Power.Active.Import", "value": "1000"}
                        ]
                    }
                ],
            },
        )
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_trigger_notification(
        self, mock_hass, charger_session
    ):
        """Heartbeat must not trigger a refresh."""
        callback = AsyncMock()
        server = OCPPServer(hass=mock_hass, on_significant_event=callback)
        await server._handle_heartbeat(charger_session, {})
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_connect_and_disconnect_trigger_notification(self, mock_hass):
        """Both a charger WebSocket connect and disconnect notify
        significant-event — regression coverage for issue #908: without
        this, a plugged-in/unplugged EV wouldn't be reflected in
        sensor.hsem_ocpp_charger_status until the next scheduled cycle."""
        callback = AsyncMock()
        server = OCPPServer(
            hass=mock_hass,
            host="127.0.0.1",
            port=19018,
            on_significant_event=callback,
        )
        await server.start()
        try:
            async with (
                aiohttp.ClientSession() as client,
                client.ws_connect("ws://127.0.0.1:19018/connect-test"),
            ):
                await asyncio.sleep(0.05)
                assert callback.await_count == 1
            await asyncio.sleep(0.05)
            assert callback.await_count == 2
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# RemoteStopTransaction retry while unconfirmed (issue #906)
# ---------------------------------------------------------------------------


class TestRemoteStopRetry:
    """Tests for retrying RemoteStopTransaction while unconfirmed."""

    def test_due_initially(self, ocpp_server):
        """With no prior attempt, a retry is immediately due."""
        assert ocpp_server._remote_stop_due(datetime.now(UTC)) is True

    def test_not_due_within_cooldown(self, ocpp_server):
        """A retry is withheld until the cooldown has elapsed."""
        now = datetime.now(UTC)
        ocpp_server._last_remote_stop_attempt = now
        assert ocpp_server._remote_stop_due(now + timedelta(seconds=10)) is False

    def test_due_after_cooldown(self, ocpp_server):
        """A retry becomes due once the cooldown has elapsed."""
        now = datetime.now(UTC)
        ocpp_server._last_remote_stop_attempt = now
        assert ocpp_server._remote_stop_due(now + timedelta(seconds=61)) is True

    @pytest.mark.asyncio
    async def test_retries_after_cooldown_when_unconfirmed(
        self, ocpp_server, charger_session
    ):
        """A stop the charger never confirmed keeps retrying on cooldown.

        Regression test for issue #906: the anti-flap guard used to omit
        "stopping" from its outer condition, so once the state machine
        entered "stopping" this whole block was skipped on every later
        cycle — a rejected or silently-ignored RemoteStopTransaction was
        attempted once and then never retried.
        """
        ocpp_server._chargers["test-cpid"] = charger_session
        ocpp_server._flap_state = "charging"
        charger_session.transaction_id = 42
        now = datetime.now(UTC)

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=now
        )
        assert ocpp_server._flap_state == "stopping"
        assert _sent_actions(charger_session).count("RemoteStopTransaction") == 1

        # Charger never confirmed (transaction_id stays set) — before the
        # cooldown elapses, must not retry yet.
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=now + timedelta(seconds=30)
        )
        assert ocpp_server._flap_state == "stopping"
        assert _sent_actions(charger_session).count("RemoteStopTransaction") == 1

        # After the cooldown, retry.
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=now + timedelta(seconds=91)
        )
        assert ocpp_server._flap_state == "stopping"
        assert _sent_actions(charger_session).count("RemoteStopTransaction") == 2

    @pytest.mark.asyncio
    async def test_retries_when_send_itself_fails(self, ocpp_server, charger_session):
        """A failed socket write (not just an unconfirmed stop) is retried.

        Before the fix, entering "stopping" made this block unreachable on
        the next cycle regardless of whether the failure was a send error
        or a silently-ignored command.
        """
        ocpp_server._chargers["test-cpid"] = charger_session
        ocpp_server._flap_state = "charging"
        charger_session.transaction_id = 42
        charger_session.websocket.send_str = AsyncMock(
            side_effect=[Exception("boom"), None]
        )
        now = datetime.now(UTC)

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=now
        )
        assert ocpp_server._flap_state == "stopping"
        assert charger_session.websocket.send_str.call_count == 1

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=now + timedelta(seconds=61)
        )
        assert charger_session.websocket.send_str.call_count == 2

    @pytest.mark.asyncio
    async def test_transitions_to_idle_once_charger_confirms(
        self, ocpp_server, charger_session
    ):
        """Once the charger's own StopTransaction clears transaction_id,
        the state machine settles to idle and stops retrying."""
        ocpp_server._chargers["test-cpid"] = charger_session
        ocpp_server._flap_state = "charging"
        charger_session.transaction_id = 42
        now = datetime.now(UTC)

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=now
        )
        assert ocpp_server._flap_state == "stopping"

        # Charger confirms via its own StopTransaction call.
        charger_session.transaction_id = None
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=now + timedelta(seconds=30)
        )
        assert ocpp_server._flap_state == "idle"
        assert _sent_actions(charger_session).count("RemoteStopTransaction") == 1


# ---------------------------------------------------------------------------
# CALLRESULT status tracking (issue #906)
# ---------------------------------------------------------------------------


class TestCallResultStatusTracking:
    """Tests for recording CALLRESULT status on outbound commands."""

    @pytest.mark.asyncio
    async def test_records_status_for_tracked_action(
        self, ocpp_server, charger_session
    ):
        """A CALLRESULT for a tracked outbound call records its status."""
        await ocpp_server._send_set_charging_profile(
            charger_session, max_power_w=3680, max_current_a=16
        )
        assert charger_session.pending_calls
        msg_id = next(iter(charger_session.pending_calls))

        await ocpp_server._handle_message(
            charger_session,
            json.dumps([3, msg_id, {"status": "Rejected"}]),
        )
        assert charger_session.last_call_status["SetChargingProfile"] == "Rejected"
        assert charger_session.pending_calls == {}

    @pytest.mark.asyncio
    async def test_logs_warning_on_non_accepted_status(
        self, ocpp_server, charger_session, caplog
    ):
        """A non-Accepted status is logged as a warning, not just recorded."""
        await ocpp_server._send_remote_start(charger_session)
        msg_id = next(iter(charger_session.pending_calls))

        with caplog.at_level(logging.WARNING):
            await ocpp_server._handle_message(
                charger_session,
                json.dumps([3, msg_id, {"status": "Rejected"}]),
            )
        assert "Rejected" in caplog.text
        assert "RemoteStartTransaction" in caplog.text

    @pytest.mark.asyncio
    async def test_accepted_status_recorded_without_warning(
        self, ocpp_server, charger_session, caplog
    ):
        """An Accepted status is recorded silently (no warning)."""
        await ocpp_server._send_set_charging_profile(
            charger_session, max_power_w=3680, max_current_a=16
        )
        msg_id = next(iter(charger_session.pending_calls))

        with caplog.at_level(logging.WARNING):
            await ocpp_server._handle_message(
                charger_session,
                json.dumps([3, msg_id, {"status": "Accepted"}]),
            )
        assert charger_session.last_call_status["SetChargingProfile"] == "Accepted"
        assert "Rejected" not in caplog.text

    @pytest.mark.asyncio
    async def test_unknown_msg_id_ignored(self, ocpp_server, charger_session):
        """A CALLRESULT for an untracked/unknown message ID is a no-op."""
        await ocpp_server._handle_message(
            charger_session,
            json.dumps([3, "unknown-id", {"status": "Accepted"}]),
        )
        assert charger_session.last_call_status == {}

    @pytest.mark.asyncio
    async def test_only_latest_pending_call_kept_per_action(
        self, ocpp_server, charger_session
    ):
        """A retried call drops the earlier attempt's pending entry.

        Otherwise pending_calls would grow without bound across retries,
        and a stale CALLRESULT could be misattributed to an abandoned
        earlier attempt.
        """
        await ocpp_server._send_set_charging_profile(
            charger_session, max_power_w=3680, max_current_a=16
        )
        first_id = next(iter(charger_session.pending_calls))
        await ocpp_server._send_set_charging_profile(
            charger_session, max_power_w=7360, max_current_a=32
        )
        assert first_id not in charger_session.pending_calls
        assert len(charger_session.pending_calls) == 1


# ---------------------------------------------------------------------------
# SetChargingProfile retried after rejection (issue #906)
# ---------------------------------------------------------------------------


class TestSetChargingProfileRejectedRetry:
    """Tests for retrying a rejected SetChargingProfile without waiting on
    a material target change."""

    @pytest.mark.asyncio
    async def test_retries_on_cooldown_when_rejected(
        self, ocpp_server, charger_session
    ):
        """A charger-rejected profile is resent on a cooldown, not forgotten.

        Without this, HSEM's diagnostic sensor would keep showing a
        "requested" current the charger already refused, with no way for
        it to ever converge on a value the charger accepts.
        """
        ocpp_server._chargers["test-cpid"] = charger_session
        ocpp_server._flap_state = "charging"
        ocpp_server._last_sent_target = 3680.0
        charger_session.transaction_id = 1
        charger_session.last_call_status["SetChargingProfile"] = "Rejected"
        now = datetime.now(UTC)

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=3.68, now=now
        )
        assert _sent_actions(charger_session).count("SetChargingProfile") == 1

        # Cooldown not elapsed — must not spam the charger every cycle.
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=3.68, now=now + timedelta(seconds=10)
        )
        assert _sent_actions(charger_session).count("SetChargingProfile") == 1

        # Cooldown elapsed — retry.
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=3.68, now=now + timedelta(seconds=61)
        )
        assert _sent_actions(charger_session).count("SetChargingProfile") == 2

    @pytest.mark.asyncio
    async def test_no_retry_when_status_unknown(self, ocpp_server, charger_session):
        """No prior CALLRESULT and no material change means no resend."""
        ocpp_server._chargers["test-cpid"] = charger_session
        ocpp_server._flap_state = "charging"
        ocpp_server._last_sent_target = 3680.0
        charger_session.transaction_id = 1
        now = datetime.now(UTC)

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=3.68, now=now
        )
        assert "SetChargingProfile" not in _sent_actions(charger_session)

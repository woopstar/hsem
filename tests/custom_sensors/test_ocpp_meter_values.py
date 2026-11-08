"""Tests for MeterValues configuration and per-phase current (issue #1391)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hsem.custom_sensors.ocpp_meter_values import (
    SAMPLE_INTERVAL_KEY,
    SAMPLED_DATA_KEY,
    SAMPLED_DATA_MAX_LENGTH_KEY,
    active_phase_count,
    meter_config_changes,
)
from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.models.ocpp_session import ChargerSession

FULL = "Energy.Active.Import.Register,Power.Active.Import,Current.Import"


@pytest.fixture
def ocpp_server() -> OCPPServer:
    """Return an OCPPServer that is never started."""
    return OCPPServer(hass=MagicMock(), host="127.0.0.1", port=19000)


@pytest.fixture
def charger_session(ocpp_server: OCPPServer) -> ChargerSession:
    """Return a registered charger session with a mocked websocket."""
    session = ChargerSession(
        cpid="cp1", websocket=AsyncMock(), connected_at=datetime.now(UTC)
    )
    ocpp_server._chargers["cp1"] = session
    return session


class TestMeterConfigChanges:
    """Which configuration keys HSEM writes."""

    @pytest.mark.parametrize("value", ["0", "300", "abc", ""])
    def test_interval_written_when_off_slow_or_invalid(self, value):
        """0, slower than 60 s, or unparsable intervals are replaced."""
        assert meter_config_changes({SAMPLE_INTERVAL_KEY: value}, set()) == {
            SAMPLE_INTERVAL_KEY: "60"
        }

    @pytest.mark.parametrize("value", ["60", "10"])
    def test_interval_kept_when_fast_enough(self, value):
        """A charger already sampling at least once a minute is left alone."""
        assert meter_config_changes({SAMPLE_INTERVAL_KEY: value}, set()) == {}

    def test_missing_keys_are_never_written(self):
        """Keys the charger does not report are not invented."""
        assert meter_config_changes({"Other": "1"}, set()) == {}

    def test_readonly_keys_are_never_written(self):
        """Read-only keys are skipped."""
        config = {SAMPLE_INTERVAL_KEY: "0", SAMPLED_DATA_KEY: ""}
        assert meter_config_changes(config, set(config)) == {}

    def test_sampled_data_gets_current_import(self):
        """A measurand list without Current.Import is extended."""
        config = {SAMPLED_DATA_KEY: "Energy.Active.Import.Register"}
        assert meter_config_changes(config, set()) == {SAMPLED_DATA_KEY: FULL}

    def test_sampled_data_superset_is_kept(self):
        """A list that already has every needed measurand is left alone."""
        config = {SAMPLED_DATA_KEY: FULL + ",Voltage, SoC"}
        assert meter_config_changes(config, set()) == {}

    def test_sampled_data_respects_max_length(self):
        """A short max length keeps the most important measurands."""
        config = {SAMPLED_DATA_KEY: "", SAMPLED_DATA_MAX_LENGTH_KEY: "2"}
        assert meter_config_changes(config, set()) == {
            SAMPLED_DATA_KEY: "Energy.Active.Import.Register,Power.Active.Import"
        }


class TestActivePhaseCount:
    """Active phase count from per-phase current."""

    def test_no_data_is_none(self):
        """No per-phase readings means unknown, not zero."""
        assert active_phase_count({}) is None

    def test_counts_phases_above_threshold(self):
        """Noise on idle phases does not count."""
        assert active_phase_count({"L1": 15.9, "L2": 0.3, "L3": 16.0}) == 2


class TestParsing:
    """Per-phase Current.Import parsing in MeterValues."""

    @pytest.mark.asyncio
    async def test_phase_current_parsed(self, ocpp_server, charger_session):
        """Current.Import with a phase is recorded; without one it is ignored."""
        payload = {
            "connectorId": 1,
            "meterValue": [
                {
                    "sampledValue": [
                        {"measurand": "Current.Import", "phase": "L1", "value": "15.8"},
                        {"measurand": "Current.Import", "phase": "L2", "value": "0.2"},
                        {"measurand": "Current.Import", "value": "16.0"},
                        {"measurand": "Current.Import", "phase": "N", "value": "1.0"},
                    ]
                }
            ],
        }
        await ocpp_server._handle_meter_values(charger_session, payload)
        assert charger_session.phase_currents_a == {
            "L1": pytest.approx(15.8),
            "L2": pytest.approx(0.2),
        }


class TestConfigurationReply:
    """GetConfiguration reply triggers the MeterValues writes."""

    @pytest.mark.asyncio
    async def test_reply_writes_needed_keys(self, ocpp_server, charger_session):
        """Writable keys that differ are written once each."""
        ocpp_server.send_change_configuration = AsyncMock(return_value=True)
        ocpp_server.absorb_configuration_reply(
            charger_session,
            {
                "configurationKey": [
                    {"key": SAMPLE_INTERVAL_KEY, "value": "0", "readonly": False},
                    {"key": SAMPLED_DATA_KEY, "value": "Power.Active.Import"},
                    {"key": "HeartbeatInterval", "value": "300", "readonly": True},
                ]
            },
        )
        assert charger_session.readonly_config_keys == {"HeartbeatInterval"}
        await asyncio.gather(*ocpp_server._background_tasks)
        calls = ocpp_server.send_change_configuration.await_args_list
        assert [c.args for c in calls] == [
            ("cp1", SAMPLE_INTERVAL_KEY, "60"),
            ("cp1", SAMPLED_DATA_KEY, FULL),
        ]

    @pytest.mark.asyncio
    async def test_reply_without_changes_writes_nothing(
        self, ocpp_server, charger_session
    ):
        """A charger already configured gets no ChangeConfiguration."""
        ocpp_server.send_change_configuration = AsyncMock(return_value=True)
        ocpp_server.absorb_configuration_reply(
            charger_session,
            {
                "configurationKey": [
                    {"key": SAMPLE_INTERVAL_KEY, "value": "60"},
                    {"key": SAMPLED_DATA_KEY, "value": FULL},
                ]
            },
        )
        assert not ocpp_server._background_tasks
        ocpp_server.send_change_configuration.assert_not_awaited()

"""Tests for the four OCPP charger sensors, for both EVs.

``tests/custom_sensors/test_ocpp_per_ev.py`` covers the entity naming. These
tests cover what each sensor publishes: status (including the not-configured
and disconnected cases), charging power (zeroed once the connector leaves
``Charging``), charger info, and the session count — always reading only the
server belonging to its own EV.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.helpers.network import NoURLAvailableError

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.ocpp_flap_state import FlapState
from custom_components.hsem.custom_sensors.ocpp_sensors import (
    HSEMOCPPChargerInfoSensor,
    HSEMOCPPChargerPowerSensor,
    HSEMOCPPChargerSessionsSensor,
    HSEMOCPPChargerStatusSensor,
    _connection_url,
    _is_listening,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity
from custom_components.hsem.models.ocpp_session import ChargerSession
from custom_components.hsem.models.sensor_config import SensorConfig

_MODULE = "custom_components.hsem.custom_sensors.ocpp_sensors"
_ENTRY_ID = "test_entry"
_CONNECTED_AT = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)


def _entry() -> MagicMock:
    """Return a minimal config entry."""
    entry = MagicMock()
    entry.entry_id = _ENTRY_ID
    entry.options = {}
    entry.data = {}
    return entry


def _coordinator(data: CoordinatorData | None) -> Any:
    """Return a coordinator stand-in publishing *data*."""
    coordinator = MagicMock()
    coordinator.last_update_success = data is not None
    coordinator.data = data
    return coordinator


def _sensor(sensor_cls: Any, data: CoordinatorData | None, charger_index: int) -> Any:
    """Construct an OCPP sensor for *charger_index* against *data*."""
    sensor = sensor_cls(_entry(), _coordinator(data), charger_index=charger_index)
    sensor.hass = MagicMock()
    return sensor


async def _restore(sensor: Any, state: str | None) -> None:
    """Run ``async_added_to_hass`` with *state* as the restored state."""
    restored = None if state is None else MagicMock(state=state, attributes={})
    sensor.async_get_last_state = AsyncMock(return_value=restored)
    with patch.object(HSEMCoordinatorEntity, "async_added_to_hass", AsyncMock()):
        await sensor.async_added_to_hass()


def _session(status: str = "Charging", power_w: float = 7400.0) -> ChargerSession:
    """Return a charger session in *status* drawing *power_w*."""
    return ChargerSession(
        cpid="charger01",
        websocket=None,
        status=status,
        vendor="ACME",
        model="Wallbox 11",
        firmware="1.2.3",
        serial="SN-1",
        current_power_w=power_w,
        transaction_id=42,
        connected_at=_CONNECTED_AT,
        status_changed_at=_CONNECTED_AT,
    )


def _enabled_cfg() -> SensorConfig:
    """Return a config with OCPP enabled for both EVs."""
    return SensorConfig(
        ocpp_enabled=True,
        ocpp_second_enabled=True,
        ocpp_port=9000,
        ocpp_second_port=9001,
        ocpp_cpid="charger01",
        ocpp_second_cpid="charger02",
    )


def _data(charger_index: int, session: ChargerSession | None = None) -> CoordinatorData:
    """Return a snapshot whose *charger_index* server has *session*."""
    chargers = {"charger01": session} if session is not None else {}
    data = CoordinatorData(cfg=_enabled_cfg())
    prefix = "ocpp_second" if charger_index == 2 else "ocpp"
    setattr(data, f"{prefix}_chargers", chargers)
    setattr(data, f"{prefix}_listening", True)
    setattr(data, f"{prefix}_last_requested_current_a", 16)
    setattr(data, f"{prefix}_anti_flap_state", FlapState.Charging.value)
    return data


def _sessions_data(charger_index: int, sessions: list[Any]) -> CoordinatorData:
    """Return a snapshot whose *charger_index* server recorded *sessions*."""
    data = CoordinatorData(cfg=_enabled_cfg())
    prefix = "ocpp_second" if charger_index == 2 else "ocpp"
    setattr(data, f"{prefix}_sessions", sessions)
    return data


_INDEXES = [pytest.param(1, id="primary"), pytest.param(2, id="second")]


_ALL_OCPP_SENSORS = [
    HSEMOCPPChargerStatusSensor,
    HSEMOCPPChargerPowerSensor,
    HSEMOCPPChargerInfoSensor,
    HSEMOCPPChargerSessionsSensor,
]


class TestSharedOcppSensorContract:
    """All four sensors are push-driven and keyed per config entry and EV."""

    @pytest.mark.parametrize("sensor_cls", _ALL_OCPP_SENSORS, ids=lambda c: c.__name__)
    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_is_push_driven_with_a_per_ev_unique_id(
        self, sensor_cls: Any, charger_index: int
    ) -> None:
        """Each EV's sensor has its own unique id and never polls."""
        sensor = _sensor(sensor_cls, _data(charger_index, _session()), charger_index)
        other = _sensor(
            sensor_cls, _data(charger_index, _session()), 2 if charger_index == 1 else 1
        )

        assert sensor.should_poll is False
        assert sensor.available is True
        assert _ENTRY_ID in sensor.unique_id
        assert sensor.unique_id != other.unique_id


class TestListeningHelper:
    """The listening helper fails closed without a snapshot."""

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_no_snapshot_is_not_listening(self, charger_index: int) -> None:
        """Before the first cycle no server is known to be listening."""
        assert _is_listening(None, charger_index) is False


class TestChargerStatusSensor:
    """Status reflects configuration, connection, and the charger's own state."""

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_reports_the_connected_charger_status(self, charger_index: int) -> None:
        """A connected charger's status string is published verbatim."""
        sensor = _sensor(
            HSEMOCPPChargerStatusSensor, _data(charger_index, _session()), charger_index
        )

        assert sensor.state == "Charging"
        assert sensor.available is True
        assert sensor.should_poll is False
        assert _ENTRY_ID in sensor.unique_id

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_no_charger_connected_is_disconnected(self, charger_index: int) -> None:
        """An enabled server with no charger reads as disconnected."""
        sensor = _sensor(
            HSEMOCPPChargerStatusSensor, _data(charger_index), charger_index
        )

        assert sensor.state == "disconnected"

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_blank_charger_status_is_disconnected(self, charger_index: int) -> None:
        """A session with no status yet reads as disconnected."""
        sensor = _sensor(
            HSEMOCPPChargerStatusSensor,
            _data(charger_index, _session(status="")),
            charger_index,
        )

        assert sensor.state == "disconnected"

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_disabled_ocpp_is_not_configured(self, charger_index: int) -> None:
        """With OCPP off the sensor says so instead of claiming disconnected."""
        sensor = _sensor(
            HSEMOCPPChargerStatusSensor,
            CoordinatorData(cfg=SensorConfig()),
            charger_index,
        )

        assert sensor.state == "not_configured"
        assert sensor.extra_state_attributes == {}

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_other_evs_charger_is_ignored(self, charger_index: int) -> None:
        """A charger on the other EV's server never leaks across."""
        other = 2 if charger_index == 1 else 1
        sensor = _sensor(
            HSEMOCPPChargerStatusSensor, _data(other, _session()), charger_index
        )

        assert sensor.state == "disconnected"

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_attributes_describe_the_server_and_session(
        self, charger_index: int
    ) -> None:
        """Port, anti-flap state, and per-charger details are exposed."""
        sensor = _sensor(
            HSEMOCPPChargerStatusSensor, _data(charger_index, _session()), charger_index
        )

        with patch(
            f"{_MODULE}.get_url", return_value="http://homeassistant.local:8123"
        ):
            attributes = sensor.extra_state_attributes

        assert attributes["listening"] is True
        assert attributes["port"] == (9001 if charger_index == 2 else 9000)
        assert attributes["requested_current_a"] == 16
        assert attributes["anti_flap_state"] == FlapState.Charging.value
        assert attributes["stalled"] is False
        expected_cpid = "charger02" if charger_index == 2 else "charger01"
        assert attributes["url"] == (
            f"ws://homeassistant.local:{attributes['port']}/{expected_cpid}"
        )
        session_attrs = attributes["charger01"]
        assert session_attrs["status"] == "Charging"
        assert session_attrs["power_w"] == pytest.approx(7400.0)
        assert session_attrs["transaction_id"] == 42
        assert session_attrs["connected_at"] == _CONNECTED_AT.isoformat()

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_attributes_omit_the_url_when_it_cannot_be_resolved(
        self, charger_index: int
    ) -> None:
        """Without a reachable HA URL the host/port are shown alone."""
        sensor = _sensor(
            HSEMOCPPChargerStatusSensor, _data(charger_index, _session()), charger_index
        )

        with patch(f"{_MODULE}.get_url", side_effect=NoURLAvailableError):
            attributes = sensor.extra_state_attributes

        assert "url" not in attributes

    @pytest.mark.asyncio
    @pytest.mark.parametrize("charger_index", _INDEXES)
    async def test_previous_state_is_restored(self, charger_index: int) -> None:
        """Before the first cycle the previous status is shown."""
        sensor = _sensor(HSEMOCPPChargerStatusSensor, None, charger_index)

        await _restore(sensor, "Available")

        assert sensor.state == "Available"
        assert sensor.available is True

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_cold_start_is_disconnected_and_unavailable(
        self, charger_index: int
    ) -> None:
        """No snapshot and no restored state means disconnected."""
        sensor = _sensor(HSEMOCPPChargerStatusSensor, None, charger_index)

        assert sensor.state == "disconnected"
        assert sensor.available is False


class TestChargerPowerSensor:
    """Power is only published while the connector is actually charging."""

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_reports_kilowatts_while_charging(self, charger_index: int) -> None:
        """Watts from MeterValues are published as kW."""
        sensor = _sensor(
            HSEMOCPPChargerPowerSensor,
            _data(charger_index, _session(power_w=7400.0)),
            charger_index,
        )

        assert sensor.state == pytest.approx(7.4)

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_stale_reading_is_zeroed_when_not_charging(
        self, charger_index: int
    ) -> None:
        """Leaving ``Charging`` zeroes the last reading (issue #969)."""
        sensor = _sensor(
            HSEMOCPPChargerPowerSensor,
            _data(charger_index, _session(status="SuspendedEVSE", power_w=7400.0)),
            charger_index,
        )

        assert sensor.state == pytest.approx(0.0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("charger_index", _INDEXES)
    async def test_cold_start_falls_back_to_zero_or_restored(
        self, charger_index: int
    ) -> None:
        """Without a charger the restored value (else ``"0.0"``) is shown."""
        cold = _sensor(HSEMOCPPChargerPowerSensor, None, charger_index)
        assert cold.state == "0.0"

        restored = _sensor(HSEMOCPPChargerPowerSensor, None, charger_index)
        await _restore(restored, "3.2")
        assert restored.state == "3.2"
        assert restored.available is True


class TestChargerInfoSensor:
    """Charger identity comes from the BootNotification payload."""

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_reports_the_model_and_identity_attributes(
        self, charger_index: int
    ) -> None:
        """The model is the state; vendor/firmware/serial are attributes."""
        sensor = _sensor(
            HSEMOCPPChargerInfoSensor, _data(charger_index, _session()), charger_index
        )

        assert sensor.state == "Wallbox 11"
        assert sensor.extra_state_attributes == {
            "vendor": "ACME",
            "model": "Wallbox 11",
            "firmware": "1.2.3",
            "serial": "SN-1",
            "cpid": "charger01",
        }

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_charger_without_a_model_is_unknown(self, charger_index: int) -> None:
        """A charger that never sent a model reads as unknown."""
        session = _session()
        session.model = ""
        sensor = _sensor(
            HSEMOCPPChargerInfoSensor, _data(charger_index, session), charger_index
        )

        assert sensor.state == "unknown"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("charger_index", _INDEXES)
    async def test_cold_start_is_disconnected_or_restored(
        self, charger_index: int
    ) -> None:
        """Without a charger the restored model (else disconnected) is shown."""
        cold = _sensor(HSEMOCPPChargerInfoSensor, None, charger_index)
        assert cold.state == "disconnected"
        assert cold.extra_state_attributes == {}

        restored = _sensor(HSEMOCPPChargerInfoSensor, None, charger_index)
        await _restore(restored, "Wallbox 22")
        assert restored.state == "Wallbox 22"


class TestChargerSessionsSensor:
    """The sessions sensor counts completed charging sessions."""

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_counts_the_sessions_of_its_own_ev(self, charger_index: int) -> None:
        """The state is the number of recorded sessions."""
        sessions = [{"id": 1}, {"id": 2}, {"id": 3}]
        sensor = _sensor(
            HSEMOCPPChargerSessionsSensor,
            _sessions_data(charger_index, sessions),
            charger_index,
        )

        assert sensor.state == "3"
        assert sensor.extra_state_attributes == {"sessions": sessions}

    @pytest.mark.parametrize("charger_index", _INDEXES)
    def test_other_evs_sessions_are_ignored(self, charger_index: int) -> None:
        """Sessions recorded for the other EV are not counted."""
        other = 2 if charger_index == 1 else 1
        sensor = _sensor(
            HSEMOCPPChargerSessionsSensor,
            _sessions_data(other, [{"id": 1}]),
            charger_index,
        )

        assert sensor.state == "0"
        assert sensor.extra_state_attributes == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("charger_index", _INDEXES)
    async def test_cold_start_counts_zero_or_restores(self, charger_index: int) -> None:
        """Without a snapshot the restored count (else ``"0"``) is shown."""
        cold = _sensor(HSEMOCPPChargerSessionsSensor, None, charger_index)
        assert cold.state == "0"
        assert cold.available is False

        restored = _sensor(HSEMOCPPChargerSessionsSensor, None, charger_index)
        await _restore(restored, "5")
        assert restored.state == "5"
        assert restored.available is True


class TestConnectionUrl:
    """The advertised URL must be dialable and carry the configured CPID."""

    def test_builds_a_websocket_url_from_has_own_host(self) -> None:
        """HA's LAN host replaces the server's ``0.0.0.0`` bind address."""
        with patch(f"{_MODULE}.get_url", return_value="http://192.168.1.10:8123"):
            assert (
                _connection_url(MagicMock(), 9000, "charger01")
                == "ws://192.168.1.10:9000/charger01"
            )

    def test_no_resolvable_url_returns_none(self) -> None:
        """Without a reachable HA URL there is nothing to advertise."""
        with patch(f"{_MODULE}.get_url", side_effect=NoURLAvailableError):
            assert _connection_url(MagicMock(), 9000, "charger01") is None

    def test_url_without_a_host_returns_none(self) -> None:
        """A base URL with no host cannot produce a dialable address."""
        with patch(f"{_MODULE}.get_url", return_value="not-a-url"):
            assert _connection_url(MagicMock(), 9000, "charger01") is None

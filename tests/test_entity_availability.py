"""Tests for configured input-entity availability transition logging."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State

from custom_components.hsem.entity_availability import (
    EntityAvailabilityTracker,
    configured_entity_references,
)
from custom_components.hsem.models.sensor_config import SensorConfig

_MODULE = "custom_components.hsem.entity_availability"
_ENTITY_ID = "sensor.house_power"
_CONFIG_KEY = "hsem_house_consumption_power"


def _config() -> SensorConfig:
    """Return a config with one monitored input entity."""
    return SensorConfig(house_consumption_power=_ENTITY_ID)


def test_logs_each_unavailable_and_recovery_transition_once() -> None:
    """Unchanged polling cycles must not repeat transition messages."""
    current_state = State(_ENTITY_ID, "1200")
    hass = MagicMock()
    hass.states.get.side_effect = lambda _entity_id: current_state
    tracker = EntityAvailabilityTracker()

    with patch(f"{_MODULE}.async_log") as async_log:
        tracker.track(hass, _config())
        assert async_log.call_count == 0

        current_state = State(_ENTITY_ID, STATE_UNAVAILABLE)
        tracker.track(hass, _config())
        tracker.track(hass, _config())

        current_state = State(_ENTITY_ID, STATE_UNKNOWN)
        tracker.track(hass, _config())

        current_state = State(_ENTITY_ID, "900")
        tracker.track(hass, _config())
        tracker.track(hass, _config())

    assert async_log.call_args_list == [
        call(
            "info",
            "Input entity %s (%s) became unavailable",
            _ENTITY_ID,
            _CONFIG_KEY,
        ),
        call(
            "info",
            "Input entity %s (%s) is available again",
            _ENTITY_ID,
            _CONFIG_KEY,
        ),
    ]


def test_initially_missing_entity_logs_once_then_logs_recovery() -> None:
    """An input absent on the first collection is reported without poll spam."""
    current_state: State | None = None
    hass = MagicMock()
    hass.states.get.side_effect = lambda _entity_id: current_state
    tracker = EntityAvailabilityTracker()

    with patch(f"{_MODULE}.async_log") as async_log:
        tracker.track(hass, _config())
        tracker.track(hass, _config())

        current_state = State(_ENTITY_ID, "500")
        tracker.track(hass, _config())

    assert async_log.call_args_list == [
        call(
            "info",
            "Input entity %s (%s) became unavailable",
            _ENTITY_ID,
            _CONFIG_KEY,
        ),
        call(
            "info",
            "Input entity %s (%s) is available again",
            _ENTITY_ID,
            _CONFIG_KEY,
        ),
    ]


def test_removed_configuration_drops_previous_availability() -> None:
    """Reconfigured entities start with a fresh availability history."""
    hass = MagicMock()
    hass.states.get.return_value = State(_ENTITY_ID, STATE_UNAVAILABLE)
    tracker = EntityAvailabilityTracker()

    with patch(f"{_MODULE}.async_log") as async_log:
        tracker.track(hass, _config())
        tracker.track(hass, SensorConfig())
        tracker.track(hass, _config())

    assert async_log.call_count == 2


def test_opt_in_feature_entities_are_tracked_only_when_enabled() -> None:
    """The phase-limiter and ML inputs are monitored only once opted into.

    Both blocks are gated on a feature flag, so a config with the flags off
    must not claim to monitor entities HSEM never reads — and one with them
    on must not silently leave those inputs unwatched.
    """
    cfg = SensorConfig(
        house_consumption_power=_ENTITY_ID,
        huawei_solar_batteries_charge_discharge_power="sensor.battery_power",
        huawei_solar_batteries_grid_charge_maximum_power="number.grid_charge_max",
        huawei_solar_power_meter_phase_a_active_power="sensor.phase_a",
        huawei_solar_power_meter_phase_b_active_power="sensor.phase_b",
        huawei_solar_power_meter_phase_c_active_power="sensor.phase_c",
        ml_consumption_energy_entity="sensor.ml_energy",
        ml_consumption_temperature_entity="sensor.ml_temperature",
        ml_consumption_weather_forecast_entity="weather.home",
    )

    cfg.phase_aware_charging_enabled = False
    cfg.ml_consumption_enabled = False
    disabled = {ref.entity_id for ref in configured_entity_references(cfg)}

    cfg.phase_aware_charging_enabled = True
    cfg.ml_consumption_enabled = True
    enabled = {ref.entity_id for ref in configured_entity_references(cfg)}

    assert disabled == {_ENTITY_ID}
    assert enabled - disabled == {
        "sensor.battery_power",
        "number.grid_charge_max",
        "sensor.phase_a",
        "sensor.phase_b",
        "sensor.phase_c",
        "sensor.ml_energy",
        "sensor.ml_temperature",
        "weather.home",
    }


def test_each_tracked_entity_is_reported_under_its_config_key() -> None:
    """A transition log is only actionable if it names the option to fix."""
    cfg = SensorConfig(house_consumption_power=_ENTITY_ID)
    cfg.ml_consumption_enabled = True
    cfg.ml_consumption_energy_entity = "sensor.ml_energy"

    keys = {ref.entity_id: ref.config_key for ref in configured_entity_references(cfg)}

    assert keys[_ENTITY_ID] == _CONFIG_KEY
    assert keys["sensor.ml_energy"] == "hsem_ml_consumption_energy_entity"

"""Tests for configured input-entity availability transition logging."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State

from custom_components.hsem.entity_availability import EntityAvailabilityTracker
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

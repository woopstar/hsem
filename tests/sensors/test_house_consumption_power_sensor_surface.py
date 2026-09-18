"""Tests for the house-consumption power sensor's published surface.

``test_house_consumption_sensor_lifecycle.py`` covers the child-entity
lifecycle. These tests cover what the sensor itself publishes: its state and
availability, the error attributes it shows while a source entity is missing,
the live readings it subtracts, and the state-change subscriptions it registers
for each configured charger.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_house_consumption_sensor_lifecycle import (
    _attach_hass,
    _make_sensor,
    _mock_config_entry,
)

_MODULE = "custom_components.hsem.custom_sensors.house_consumption_power_sensor"
_HOUSE = "sensor.house_power"
_EV = "sensor.ev_charger_power"
_EV2 = "sensor.ev_second_charger_power"


def _sensor_with_chargers() -> Any:
    """Return a sensor configured with a house meter and both chargers."""
    entry = _mock_config_entry(
        hsem_house_consumption_power=_HOUSE,
        hsem_ev_charger_power=_EV,
        hsem_ev_second_charger_power=_EV2,
    )
    sensor, _added = _make_sensor(config_entry=entry)
    _attach_hass(sensor)
    return sensor


class TestPublishedSurface:
    """The sensor reports its own name, state and availability."""

    def test_name_state_and_polling(self) -> None:
        """A freshly built sensor polls and is unavailable until it updates."""
        sensor, _added = _make_sensor(hour_start=14)
        _attach_hass(sensor)

        assert "14-15" in sensor.name
        assert sensor.state is None
        assert sensor.should_poll is True
        assert sensor.available is False
        assert sensor.unique_id is not None

    def test_state_and_availability_follow_the_measurement(self) -> None:
        """Once a measurement lands the sensor reports it and is available."""
        sensor, _added = _make_sensor()
        _attach_hass(sensor)
        sensor._state = 1234.5
        sensor._available = True

        assert sensor.state == pytest.approx(1234.5)
        assert sensor.available is True


class TestAttributes:
    """Attributes either explain the misconfiguration or show the readings."""

    def test_missing_inputs_are_reported_as_an_error(self) -> None:
        """Before any successful read the attributes say what is wrong."""
        sensor, _added = _make_sensor()
        _attach_hass(sensor)

        attributes = sensor.extra_state_attributes

        assert attributes["status"] == "error"
        assert "missing or not reporting a state" in attributes["description"]
        assert attributes["unique_id"] == sensor.unique_id

    def test_live_readings_are_exposed_once_available(self) -> None:
        """The source entities and their rounded readings are published."""
        sensor = _sensor_with_chargers()
        sensor._missing_input_entities = False
        sensor._hsem_house_consumption_power_state = 1234.567
        sensor._hsem_ev_charger_power_state = 7400.4

        attributes = sensor.extra_state_attributes

        assert attributes["house_consumption_power_entity"] == _HOUSE
        assert attributes["house_consumption_power_state"] == pytest.approx(1234.57)
        assert attributes["ev_charger_power_entity"] == _EV
        assert attributes["ev_charger_power_state"] == pytest.approx(7400.4)
        assert "status" not in attributes


class TestUpdateEntryPoints:
    """Both manual update and an options change re-run the measurement."""

    @pytest.mark.asyncio
    async def test_async_update_delegates_to_the_handler(self) -> None:
        """A poll runs the same handler a state change would."""
        sensor, _added = _make_sensor()
        _attach_hass(sensor)
        handler = AsyncMock()
        sensor._async_handle_update = handler  # type: ignore[method-assign]  # test spy

        await sensor.async_update()

        handler.assert_awaited_once_with(None)

    @pytest.mark.asyncio
    async def test_options_update_rereads_settings_then_updates(self) -> None:
        """An options save re-reads config before measuring again."""
        sensor, _added = _make_sensor()
        _attach_hass(sensor)
        handler = AsyncMock()
        sensor._async_handle_update = handler  # type: ignore[method-assign]  # test spy
        update_settings = MagicMock()
        sensor._update_settings = update_settings  # type: ignore[method-assign]  # test spy

        await sensor.async_options_updated(_mock_config_entry())

        update_settings.assert_called_once()
        handler.assert_awaited_once_with(None)


class TestEntityTracking:
    """Each configured charger is subscribed to exactly once."""

    @pytest.mark.asyncio
    async def test_both_chargers_are_tracked_once(self) -> None:
        """A second pass does not re-subscribe an already tracked entity."""
        sensor = _sensor_with_chargers()

        with patch(f"{_MODULE}.async_track_state_change_event") as track:
            track.return_value = MagicMock()
            await sensor._async_track_entities()
            first_pass = track.call_count
            await sensor._async_track_entities()

        tracked = sensor._tracked_entities
        assert {_HOUSE, _EV, _EV2} <= tracked
        # The second pass added no new subscriptions.
        assert track.call_count == first_pass

    @pytest.mark.asyncio
    async def test_unconfigured_chargers_are_not_tracked(self) -> None:
        """Only entities the user configured are subscribed to."""
        sensor, _added = _make_sensor()
        _attach_hass(sensor)

        with patch(f"{_MODULE}.async_track_state_change_event") as track:
            track.return_value = MagicMock()
            await sensor._async_track_entities()

        assert _EV not in sensor._tracked_entities
        assert _EV2 not in sensor._tracked_entities


class TestFetchSensorStates:
    """Live power is read from every configured source entity."""

    @pytest.mark.asyncio
    async def test_house_and_both_chargers_are_read(self) -> None:
        """Each configured entity contributes its own reading."""
        sensor = _sensor_with_chargers()
        readings = {_HOUSE: 1500.0, _EV: 7400.0, _EV2: 3700.0}

        with patch(
            f"{_MODULE}.ha_get_entity_state_and_convert",
            side_effect=lambda _self, entity_id, _type: readings[entity_id],
        ):
            await sensor._async_fetch_sensor_states()

        assert sensor._missing_input_entities is False
        assert sensor._hsem_house_consumption_power_state == pytest.approx(1500.0)
        assert sensor._hsem_ev_charger_power_state == pytest.approx(7400.0)
        assert sensor._hsem_ev_second_charger_power_state == pytest.approx(3700.0)

    @pytest.mark.asyncio
    async def test_an_unreadable_entity_reads_as_zero_power(self) -> None:
        """An unavailable source contributes no power rather than raising."""
        sensor = _sensor_with_chargers()

        with patch(f"{_MODULE}.ha_get_entity_state_and_convert", return_value=None):
            await sensor._async_fetch_sensor_states()

        assert sensor._hsem_house_consumption_power_state == pytest.approx(0.0)
        assert sensor._hsem_ev_charger_power_state == pytest.approx(0.0)

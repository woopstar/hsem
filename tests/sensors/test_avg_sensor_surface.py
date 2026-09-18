"""Tests for the rolling-average sensor's surface and history pruning.

The average sensor keeps one measurement per day for its configured window and
must never grow beyond it — the measurements dict is persisted as a state
attribute, so an unbounded dict would eventually exceed HA's attribute limit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor
from tests.test_avg_sensor_negative_guard import _avg_sensor


class TestPublishedSurface:
    """Identity, polling and availability are reported from local state."""

    def test_identity_and_polling(self) -> None:
        """The sensor polls and carries the ids it was constructed with."""
        sensor = _avg_sensor()

        assert sensor.unique_id == "test_average"
        assert sensor.name == "Test average"
        assert sensor.should_poll is True

    def test_availability_follows_a_computed_state(self) -> None:
        """The sensor is unavailable until it has computed an average."""
        sensor = _avg_sensor()

        assert sensor.available is False

        sensor._state = 1.5
        assert sensor.available is True

    def test_attributes_expose_the_window_and_measurements(self) -> None:
        """The tracked entity, window and stored measurements are published."""
        sensor = _avg_sensor(average=3)
        sensor._measurements = {"2026-06-01": 1.0}

        attributes = sensor.extra_state_attributes

        assert attributes["tracked_entity"] == "sensor.utility"
        assert attributes["average"] == 3
        assert attributes["hour_start"] == 14
        assert attributes["hour_end"] == 15
        assert attributes["measurements"] == {"2026-06-01": 1.0}

    @pytest.mark.asyncio
    async def test_poll_delegates_to_the_update_handler(self) -> None:
        """A manual poll runs the same handler a state change would."""
        sensor = _avg_sensor()
        handler = AsyncMock()
        sensor._async_handle_update = handler  # type: ignore[method-assign]  # test spy

        await sensor.async_update()

        handler.assert_awaited_once_with(None)


class TestMeasurementPruning:
    """History is bounded to the configured number of days."""

    @pytest.mark.asyncio
    async def test_oldest_days_are_dropped_first(self) -> None:
        """A 3-day average keeps the three most recent days."""
        sensor = _avg_sensor(average=3)
        sensor._measurements = {
            "2026-05-29": 1.0,
            "2026-05-30": 2.0,
            "2026-05-31": 3.0,
            "2026-06-01": 4.0,
            "2026-06-02": 5.0,
        }

        await HSEMAvgSensor._async_cleanup_old_measurements(sensor)

        assert sorted(sensor._measurements) == [
            "2026-05-31",
            "2026-06-01",
            "2026-06-02",
        ]

    @pytest.mark.asyncio
    async def test_history_within_the_window_is_kept(self) -> None:
        """Nothing is dropped while the history still fits."""
        sensor = _avg_sensor(average=3)
        sensor._measurements = {"2026-06-01": 1.0, "2026-06-02": 2.0}

        await HSEMAvgSensor._async_cleanup_old_measurements(sensor)

        assert len(sensor._measurements) == 2

    @pytest.mark.asyncio
    async def test_no_history_yet_is_safe(self) -> None:
        """Pruning before the first measurement does nothing."""
        sensor = _avg_sensor()
        sensor._measurements = None

        await HSEMAvgSensor._async_cleanup_old_measurements(sensor)

        assert sensor._measurements is None

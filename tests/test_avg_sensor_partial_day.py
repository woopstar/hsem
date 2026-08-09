"""Regression tests for issue #720 follow-up — partial-day average inflation.

Bug
---
``HSEMAvgSensor._async_store_utility_meter_value`` sampled the daily
utility meter every 5 minutes and stored the reading under the current
date, regardless of whether the day's hour block had completed.  The
utility meter resets at ``hour_start`` and accumulates energy only during
the ``hour_start`` → ``hour_end`` block, so a sample taken mid-day is a
**partial** day.

For a new energy sensor with limited history, partial days quickly fill
the rolling window.  The reporter's sensor read 14.267 kWh at 05:45
(accumulated since midnight); after three such partial days the 3-day
average converged to ~14 kWh, and HSEM forecast ~4.7 kWh per 15-min slot
(~18 kW continuous) for a house actually drawing ~260 W.

Fix
---
The sample is only persisted once the day's hour block is complete
(``now.hour >= hour_end``; overnight 23→00 block: any hour except 23).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor


def _make_sensor(
    hour_start: int,
    hour_end: int,
    measurements: dict[str, float] | None = None,
) -> MagicMock:
    sensor = MagicMock(spec=HSEMAvgSensor)
    sensor.hass = MagicMock()
    sensor._tracked_entity = "sensor.daily_kwh"
    sensor._measurements = measurements if measurements is not None else {}
    sensor._average = 14
    sensor._hour_start = hour_start
    sensor._hour_end = hour_end
    sensor._async_cleanup_old_measurements = AsyncMock()
    return sensor


async def _store(sensor: MagicMock, now: datetime, meter_value: float) -> None:
    with (
        patch(
            "custom_components.hsem.custom_sensors.avg_sensor.dt_util.now",
            return_value=now,
        ),
        patch(
            "custom_components.hsem.custom_sensors.avg_sensor"
            ".ha_get_entity_state_and_convert",
            return_value=meter_value,
        ),
    ):
        await HSEMAvgSensor._async_store_utility_meter_value(sensor)


class TestPartialDayNotStored:
    """Mid-block samples must not be persisted as complete days."""

    @pytest.mark.asyncio
    async def test_morning_sample_not_stored(self) -> None:
        """A 05:45 sample of a 14→15 block must not be stored (issue #720)."""
        sensor = _make_sensor(hour_start=14, hour_end=15)
        await _store(sensor, datetime(2026, 8, 8, 5, 45, tzinfo=UTC), 14.267)
        assert sensor._measurements == {}

    @pytest.mark.asyncio
    async def test_sample_inside_block_not_stored(self) -> None:
        """A sample taken during the active block hour is partial."""
        sensor = _make_sensor(hour_start=14, hour_end=15)
        await _store(sensor, datetime(2026, 8, 8, 14, 30, tzinfo=UTC), 0.4)
        assert sensor._measurements == {}

    @pytest.mark.asyncio
    async def test_sample_after_block_stored(self) -> None:
        """Once the block closes the final value is stored."""
        sensor = _make_sensor(hour_start=14, hour_end=15)
        await _store(sensor, datetime(2026, 8, 8, 15, 5, tzinfo=UTC), 0.42)
        assert sensor._measurements == {"2026-08-08": 0.42}

    @pytest.mark.asyncio
    async def test_reporter_scenario_average_stays_realistic(self) -> None:
        """Replay the issue scenario: partial-day samples must not inflate
        the rolling average used for the consumption forecast.

        The reporter's 14→15 block was sampled at 05:45 with 14.267 kWh
        accumulated since the meter's 14:00 reset the previous day — a
        partial day that must not be stored.  Only samples taken at/after
        15:00 (block complete) are valid.
        """
        sensor = _make_sensor(hour_start=14, hour_end=15)

        # Three mornings in a row, meter sampled at 05:45 — block not
        # complete, nothing stored.
        for day in (6, 7, 8):
            await _store(sensor, datetime(2026, 8, day, 5, 45, tzinfo=UTC), 14.267)

        assert sensor._measurements == {}

        # After the 14→15 block closes (15:05), the real hourly energy is stored.
        await _store(sensor, datetime(2026, 8, 8, 15, 5, tzinfo=UTC), 0.42)
        assert sensor._measurements == {"2026-08-08": 0.42}


class TestOvernightBlock:
    """The 23→00 block closes at midnight; post-midnight samples belong to
    the previous date."""

    @pytest.mark.asyncio
    async def test_during_block_not_stored(self) -> None:
        sensor = _make_sensor(hour_start=23, hour_end=0)
        await _store(sensor, datetime(2026, 8, 8, 23, 30, tzinfo=UTC), 0.5)
        assert sensor._measurements == {}

    @pytest.mark.asyncio
    async def test_after_midnight_stored_under_previous_date(self) -> None:
        sensor = _make_sensor(hour_start=23, hour_end=0)
        await _store(sensor, datetime(2026, 8, 9, 0, 5, tzinfo=UTC), 0.5)
        assert sensor._measurements == {"2026-08-08": 0.5}

    @pytest.mark.asyncio
    async def test_later_same_day_stored_under_that_date(self) -> None:
        """A sample at 23:xx of the next day starts a new measurement."""
        sensor = _make_sensor(hour_start=23, hour_end=0)
        await _store(sensor, datetime(2026, 8, 9, 23, 30, tzinfo=UTC), 0.0)
        # 23:30 is inside the block — not stored
        assert sensor._measurements == {}

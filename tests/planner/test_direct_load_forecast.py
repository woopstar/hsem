"""A supplied load forecast is planned with as is (issue #1485).

The ML predictor forecasts the house load itself. Before issue #1485 it
reached the planner by writing the same value into all four window averages,
so the blend of four equal values returned it. That made the 1d/3d/7d/14d
fields useless in ML mode. ``HourlyConsumptionAverage.forecast`` now carries
the forecast, and the windows are informational when it is set.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.planner.slot_population import (
    populate_consumption,
    weighted_avg_consumption,
)

_NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
_WEIGHTS = (25, 30, 30, 15)
_WINDOWS = (1.68, 1.56, 1.44, 1.32)


def _average(
    hour: int, forecast: float | None, *, day_offset: int = 0
) -> HourlyConsumptionAverage:
    """Return an hour with four different windows and an optional forecast."""
    return HourlyConsumptionAverage(
        hour=hour,
        avg_1d=1.68,
        avg_3d=1.56,
        avg_7d=1.44,
        avg_14d=1.32,
        day_offset=day_offset,
        forecast=forecast,
    )


def _indexed_slots(hours: int) -> tuple[TimeSeriesIndex, list[PlannedSlot]]:
    tsi = TimeSeriesIndex.from_now(_NOW, interval_minutes=15, horizon_hours=hours)
    return tsi, [PlannedSlot(start=meta.start, end=meta.end) for meta in tsi]


class TestSuppliedForecastOnTheSlotGrid:
    """The ``TimeSeriesIndex`` path, which every planner run uses."""

    def test_forecast_is_the_slot_load_and_windows_are_kept(self) -> None:
        tsi, slots = _indexed_slots(24)
        averages = [_average(hour, 0.8) for hour in range(24)]

        populate_consumption(slots, averages, *_WEIGHTS, 15, tsi)

        blended, _ = weighted_avg_consumption(*_WINDOWS, *_WEIGHTS)
        assert blended != pytest.approx(0.8)
        for slot in slots:
            assert slot.avg_house_consumption_kwh == pytest.approx(0.2)
            assert slot.avg_house_consumption_1d_kwh == pytest.approx(0.42)
            assert slot.avg_house_consumption_3d_kwh == pytest.approx(0.39)
            assert slot.avg_house_consumption_7d_kwh == pytest.approx(0.36)
            assert slot.avg_house_consumption_14d_kwh == pytest.approx(0.33)
        assert not tsi.missing_slots

    def test_forecast_does_not_depend_on_the_window_weights(self) -> None:
        """All-zero weights blend to zero, a supplied forecast does not."""
        tsi, slots = _indexed_slots(24)
        averages = [_average(hour, 0.8) for hour in range(24)]

        populate_consumption(slots, averages, 0, 0, 0, 0, 15, tsi)

        assert [s.avg_house_consumption_kwh for s in slots] == pytest.approx(
            [0.2] * len(slots)
        )

    def test_each_day_keeps_its_own_forecast(self) -> None:
        tsi, slots = _indexed_slots(48)
        averages = [
            _average(hour, 0.4 if day == 0 else 1.2, day_offset=day)
            for day in range(2)
            for hour in range(24)
        ]

        populate_consumption(slots, averages, *_WEIGHTS, 15, tsi)

        tomorrow = (_NOW + timedelta(days=1)).date()
        for slot in slots:
            expected = 0.3 if slot.start.date() == tomorrow else 0.1
            assert slot.avg_house_consumption_kwh == pytest.approx(expected)

    def test_hour_without_a_forecast_is_blended(self) -> None:
        tsi, slots = _indexed_slots(24)
        averages = [_average(hour, 0.8 if hour != 5 else None) for hour in range(24)]

        populate_consumption(slots, averages, *_WEIGHTS, 15, tsi)

        blended, _ = weighted_avg_consumption(*_WINDOWS, *_WEIGHTS)
        for slot in slots:
            expected = blended / 4 if slot.start.hour == 5 else 0.2
            assert slot.avg_house_consumption_kwh == pytest.approx(expected, abs=1e-3)


class TestSuppliedForecastByHour:
    """The hour-keyed path used without a time index."""

    def test_forecast_is_the_slot_load_and_windows_are_kept(self) -> None:
        start = _NOW.replace(hour=10)
        slots = [PlannedSlot(start=start, end=start + timedelta(minutes=15))]

        populate_consumption(slots, [_average(10, 0.8)], *_WEIGHTS, 15, None)

        assert slots[0].avg_house_consumption_kwh == pytest.approx(0.2)
        assert slots[0].avg_house_consumption_1d_kwh == pytest.approx(0.42)
        assert slots[0].avg_house_consumption_14d_kwh == pytest.approx(0.33)

    def test_hour_without_a_forecast_is_blended(self) -> None:
        start = _NOW.replace(hour=10)
        slots = [PlannedSlot(start=start, end=start + timedelta(minutes=15))]

        populate_consumption(slots, [_average(10, None)], *_WEIGHTS, 15, None)

        blended, _ = weighted_avg_consumption(*_WINDOWS, *_WEIGHTS)
        assert slots[0].avg_house_consumption_kwh == pytest.approx(
            blended / 4, abs=1e-3
        )

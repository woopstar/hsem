"""A supplied load forecast is planned with as is, slot by slot (issues #1485, #1490).

The ML predictor forecasts the house load itself. Before issue #1485 it
reached the planner by writing the same value into all four window averages,
so the blend of four equal values returned it. That made the 1d/3d/7d/14d
fields useless in ML mode.

The forecast then got a field per hour, taken from the first slot of the hour.
At 15-minute slots the planner therefore planned all four quarters with the
first quarter's forecast (issue #1490). ``PlannerInput.load_forecast_slots``
now carries one value per slot, and the windows are informational where a
slot has one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.load_forecast_slot import LoadForecastSlot
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.planner.slot_population import (
    populate_consumption,
    weighted_avg_consumption,
)

_NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
_WEIGHTS = (25, 30, 30, 15)
_WINDOWS = (1.68, 1.56, 1.44, 1.32)
#: The four quarter-hour forecasts of the issue's example hour.
_QUARTERS = (0.0937, 0.1487, 0.2, 0.25)


def _averages(days: int = 1) -> list[HourlyConsumptionAverage]:
    """Return every hour of *days* days with four different windows."""
    return [
        HourlyConsumptionAverage(hour, *_WINDOWS, day_offset=day)
        for day in range(days)
        for hour in range(24)
    ]


def _indexed_slots(hours: int) -> tuple[TimeSeriesIndex, list[PlannedSlot]]:
    tsi = TimeSeriesIndex.from_now(_NOW, interval_minutes=15, horizon_hours=hours)
    return tsi, [PlannedSlot(start=meta.start, end=meta.end) for meta in tsi]


def _forecast(
    tsi: TimeSeriesIndex, *, skip_hour: int | None = None
) -> list[LoadForecastSlot]:
    """Return a per-slot forecast that repeats ``_QUARTERS`` in every hour."""
    return [
        LoadForecastSlot(
            hour=meta.hour,
            day_offset=meta.key.day_offset,
            slot_in_day=meta.key.slot_in_day,
            load_kwh=_QUARTERS[meta.key.slot_in_day % 4] + meta.key.day_offset,
        )
        for meta in tsi
        if meta.hour != skip_hour
    ]


class TestSuppliedForecastOnTheSlotGrid:
    """The ``TimeSeriesIndex`` path, which every planner run uses."""

    def test_each_quarter_hour_gets_its_own_forecast(self) -> None:
        """The #1490 symptom: four quarters planned with the first one."""
        tsi, slots = _indexed_slots(24)

        populate_consumption(slots, _averages(), *_WEIGHTS, 15, tsi, _forecast(tsi))

        first_hour = [s.avg_house_consumption_kwh for s in slots[:4]]
        assert first_hour == pytest.approx([0.094, 0.149, 0.2, 0.25])
        for slot, meta in zip(slots, tsi, strict=True):
            assert slot.avg_house_consumption_kwh == pytest.approx(
                _QUARTERS[meta.key.slot_in_day % 4], abs=5e-4
            )
        assert not tsi.missing_slots

    def test_windows_are_kept_for_display(self) -> None:
        tsi, slots = _indexed_slots(24)

        populate_consumption(slots, _averages(), *_WEIGHTS, 15, tsi, _forecast(tsi))

        for slot in slots:
            assert slot.avg_house_consumption_1d_kwh == pytest.approx(0.42)
            assert slot.avg_house_consumption_3d_kwh == pytest.approx(0.39)
            assert slot.avg_house_consumption_7d_kwh == pytest.approx(0.36)
            assert slot.avg_house_consumption_14d_kwh == pytest.approx(0.33)

    def test_forecast_does_not_depend_on_the_window_weights(self) -> None:
        """All-zero weights blend to zero, a supplied forecast does not."""
        tsi, slots = _indexed_slots(24)

        populate_consumption(slots, _averages(), 0, 0, 0, 0, 15, tsi, _forecast(tsi))

        assert [s.avg_house_consumption_kwh for s in slots[:4]] == pytest.approx(
            [0.094, 0.149, 0.2, 0.25]
        )

    def test_each_day_keeps_its_own_forecast(self) -> None:
        tsi, slots = _indexed_slots(48)

        populate_consumption(slots, _averages(2), *_WEIGHTS, 15, tsi, _forecast(tsi))

        tomorrow = (_NOW + timedelta(days=1)).date()
        for slot, meta in zip(slots, tsi, strict=True):
            quarter = _QUARTERS[meta.key.slot_in_day % 4]
            expected = quarter + (1.0 if slot.start.date() == tomorrow else 0.0)
            assert slot.avg_house_consumption_kwh == pytest.approx(expected, abs=5e-4)

    def test_slot_without_a_forecast_is_blended(self) -> None:
        tsi, slots = _indexed_slots(24)

        populate_consumption(
            slots, _averages(), *_WEIGHTS, 15, tsi, _forecast(tsi, skip_hour=5)
        )

        blended, _ = weighted_avg_consumption(*_WINDOWS, *_WEIGHTS)
        for slot, meta in zip(slots, tsi, strict=True):
            expected = (
                blended / 4
                if slot.start.hour == 5
                else _QUARTERS[meta.key.slot_in_day % 4]
            )
            assert slot.avg_house_consumption_kwh == pytest.approx(expected, abs=1e-3)

    def test_no_forecast_blends_every_slot(self) -> None:
        tsi, slots = _indexed_slots(24)

        populate_consumption(slots, _averages(), *_WEIGHTS, 15, tsi)

        blended, _ = weighted_avg_consumption(*_WINDOWS, *_WEIGHTS)
        assert [s.avg_house_consumption_kwh for s in slots] == pytest.approx(
            [round(blended / 4, 3)] * len(slots)
        )


def test_hour_keyed_path_has_no_slot_axis_and_blends() -> None:
    """Without a time index there is no slot axis to key the forecast on."""
    start = _NOW.replace(hour=10)
    slots = [PlannedSlot(start=start, end=start + timedelta(minutes=15))]
    forecast = [LoadForecastSlot(hour=10, day_offset=0, slot_in_day=40, load_kwh=0.2)]

    populate_consumption(slots, _averages(), *_WEIGHTS, 15, None, forecast)

    blended, _ = weighted_avg_consumption(*_WINDOWS, *_WEIGHTS)
    assert slots[0].avg_house_consumption_kwh == pytest.approx(blended / 4, abs=1e-3)

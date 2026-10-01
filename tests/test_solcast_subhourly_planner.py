"""Tests for issue #1191 stage 2 — sub-hourly PV reaches the planner.

Stage 1 made the *hour's* PV right for any Solcast cadence, but the planner
still received one value per hour and split it evenly over the hour's slots.
A half-hourly forecast of 0.4 kW then 0.8 kW was planned as four quarter-hours
of 0.15 kWh instead of 0.1, 0.1, 0.2, 0.2.

``SolcastSlot`` now carries an optional ``slot_in_day`` (as ``PricePoint`` has
since issue #720).  ``build_planner_input`` emits one entry per slot when the
source is finer than an hour, and the planner turns each entry's average power
into the slot's energy.

Every test that claims production behaviour goes through the real populator,
``build_planner_input`` and ``run_planner``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import homeassistant.util.dt as dt_util

from custom_components.hsem.coordinator_builder import (
    build_planner_input,
    generate_recommendation_intervals,
)
from custom_components.hsem.coordinator_tracking import register_forecasts_from_planner
from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    populate_price_and_solcast_from_snapshot,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.slot_population import populate_solcast
from custom_components.hsem.utils.forecast_tracker import ForecastTracker
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2026, 6, 1, 0, 5, tzinfo=_TZ)
_H8 = datetime(2026, 6, 1, 8, 0, tzinfo=_TZ)
_SOLCAST_TODAY = "sensor.solcast_pv_forecast_forecast_today"


def _source(
    attribute: str, start: datetime, step_minutes: int, values: list[float]
) -> dict[str, Any]:
    """Return a Solcast attribute dict with one point per *step_minutes*."""
    return {
        attribute: [
            {
                "period_start": (
                    start + timedelta(minutes=step_minutes * i)
                ).isoformat(),
                "pv_estimate": value,
            }
            for i, value in enumerate(values)
        ]
    }


# 08:00-09:00 holds 0.6 kWh and 09:00-10:00 holds 1.2 kWh in every source.
_HOURLY = _source("detailedHourly", _H8, 60, [0.6, 1.2])
_HALF_HOURLY = _source("detailedForecast", _H8, 30, [0.4, 0.8, 1.0, 1.4])
_QUARTER_HOURLY = _source(
    "detailedForecast", _H8, 15, [0.2, 0.4, 0.8, 1.0, 0.9, 1.1, 1.3, 1.5]
)


@pytest.fixture(autouse=True)
def copenhagen_ha_tz() -> Iterator[None]:
    """Run each test with Home Assistant's local timezone set to Copenhagen."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(_TZ)
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


def _recommendations(
    interval_minutes: int, now: datetime = _NOW
) -> list[HourlyRecommendation]:
    """Return a fresh, priced 24 h recommendation grid for *now*'s day."""
    with patch("homeassistant.util.dt.now", return_value=now):
        recs = generate_recommendation_intervals(interval_minutes, 24)
    for rec in recs:
        rec.import_price = 1.0
        rec.export_price = 0.05
    return recs


def _populate_pv(
    recs: list[HourlyRecommendation], *sources: dict[str, Any]
) -> list[HourlyRecommendation]:
    """Populate PV from each source in turn through the snapshot populator."""
    cfg = SensorConfig()
    cfg.solcast_pv_forecast_forecast_today = _SOLCAST_TODAY
    cfg.solcast_pv_forecast_forecast_likelihood = "pv_estimate"
    for attributes in sources:
        snapshot = StateSnapshot(
            live=LiveState(), sensor_attributes={_SOLCAST_TODAY: attributes}
        )
        populate_price_and_solcast_from_snapshot(recs, snapshot, cfg)
    return recs


def _planner_input(
    recs: list[HourlyRecommendation], interval_minutes: int, now: datetime = _NOW
) -> PlannerInput:
    """Build the planner input exactly as the coordinator does."""
    cfg = SensorConfig()
    cfg.recommendation_interval_minutes = interval_minutes
    cfg.recommendation_interval_length = 24
    with patch("homeassistant.util.dt.now", return_value=now):
        return build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=recs,
            previous_winner_name=None,
            previous_winner_score=0.0,
        )


def _input_for(
    interval_minutes: int, *sources: dict[str, Any], now: datetime = _NOW
) -> PlannerInput:
    """Populate a fresh grid from *sources* and build the planner input."""
    recs = _populate_pv(_recommendations(interval_minutes, now), *sources)
    return _planner_input(recs, interval_minutes, now)


def _planned_pv(inp: PlannerInput, hour: datetime) -> list[float]:
    """Run the planner and return the PV energy of each slot inside *hour*."""
    out = run_planner(inp)
    return [
        slot.solcast_pv_estimate_kwh
        for slot in out.slots
        if hour <= slot.start < hour + timedelta(hours=1)
    ]


class TestBuilderEmission:
    """What ``build_planner_input`` emits for each source cadence."""

    def test_half_hourly_source_emits_one_entry_per_quarter_hour_slot(self) -> None:
        inp = _input_for(15, _HALF_HOURLY)

        assert len(inp.solcast_slots) == 96
        assert all(sc.slot_in_day is not None for sc in inp.solcast_slots)
        by_slot = {sc.slot_in_day: sc.pv_estimate for sc in inp.solcast_slots}
        assert [by_slot[32 + i] for i in range(4)] == pytest.approx(
            [0.4, 0.4, 0.8, 0.8]
        )

    def test_quarter_hourly_source_emits_its_own_value_per_slot(self) -> None:
        inp = _input_for(15, _QUARTER_HOURLY)

        by_slot = {sc.slot_in_day: sc.pv_estimate for sc in inp.solcast_slots}
        assert [by_slot[32 + i] for i in range(8)] == pytest.approx(
            [0.2, 0.4, 0.8, 1.0, 0.9, 1.1, 1.3, 1.5]
        )

    def test_per_slot_entries_carry_hour_and_day(self) -> None:
        inp = _input_for(30, _HALF_HOURLY)

        entry = next(sc for sc in inp.solcast_slots if sc.slot_in_day == 17)
        assert (entry.day_offset, entry.hour) == (0, 8)
        assert entry.pv_estimate == pytest.approx(0.8)

    @pytest.mark.parametrize("interval_minutes", [15, 30, 60])
    def test_hourly_source_keeps_one_entry_per_hour(
        self, interval_minutes: int
    ) -> None:
        """An hourly sensor gives exactly the planner input it gave before."""
        inp = _input_for(interval_minutes, _HOURLY)

        expected = [SolcastSlot(hour=hour, pv_estimate=0.0) for hour in range(24)]
        expected[8] = SolcastSlot(hour=8, pv_estimate=0.6)
        expected[9] = SolcastSlot(hour=9, pv_estimate=1.2)
        assert inp.solcast_slots == expected
        assert all(sc.slot_in_day is None for sc in inp.solcast_slots)

    def test_source_finer_than_an_hourly_slot_stays_hourly(self) -> None:
        """At 60-minute slots the populator's slot mean is the hour's value."""
        inp = _input_for(60, _HALF_HOURLY)

        assert all(sc.slot_in_day is None for sc in inp.solcast_slots)
        assert inp.solcast_slots[8].pv_estimate == pytest.approx(0.6)


class TestPlannerSlotPv:
    """The PV energy the planner puts on each slot."""

    def test_half_hourly_source_at_quarter_hour_slots(self) -> None:
        """0.4 kW then 0.8 kW is 0.1, 0.1, 0.2, 0.2 kWh, not four times 0.15."""
        pv = _planned_pv(_input_for(15, _HALF_HOURLY), _H8)

        assert pv == pytest.approx([0.1, 0.1, 0.2, 0.2])

    def test_half_hourly_source_at_half_hour_slots(self) -> None:
        pv = _planned_pv(_input_for(30, _HALF_HOURLY), _H8)

        assert pv == pytest.approx([0.2, 0.4])

    def test_quarter_hourly_source_at_quarter_hour_slots(self) -> None:
        pv = _planned_pv(_input_for(15, _QUARTER_HOURLY), _H8)

        assert pv == pytest.approx([0.05, 0.1, 0.2, 0.25])

    def test_both_attributes_plan_at_the_finer_cadence(self) -> None:
        """With hourly and half-hourly on one sensor the half-hours win."""
        both = {**_HOURLY, **_HALF_HOURLY}

        pv = _planned_pv(_input_for(15, both), _H8)

        assert pv == pytest.approx([0.1, 0.1, 0.2, 0.2])

    @pytest.mark.parametrize("interval_minutes", [15, 30, 60])
    @pytest.mark.parametrize(
        "source",
        [_HOURLY, _HALF_HOURLY, _QUARTER_HOURLY],
        ids=["hourly", "half_hourly", "quarter_hourly"],
    )
    def test_slot_energy_sums_to_the_hours_energy(
        self, source: dict[str, Any], interval_minutes: int
    ) -> None:
        """The alignment conserves each hour's energy at every cadence."""
        inp = _input_for(interval_minutes, source)

        assert sum(_planned_pv(inp, _H8)) == pytest.approx(0.6)
        assert sum(_planned_pv(inp, _H8 + timedelta(hours=1))) == pytest.approx(1.2)

    def test_hourly_source_still_splits_the_hour_evenly(self) -> None:
        pv = _planned_pv(_input_for(15, _HOURLY), _H8)

        assert pv == pytest.approx([0.15, 0.15, 0.15, 0.15])

    def test_hourly_fallback_covers_slots_without_their_own_entry(self) -> None:
        """An hour-granular entry still fans out next to per-slot entries."""
        inp = _input_for(15, _HALF_HOURLY)
        hour_9 = [sc for sc in inp.solcast_slots if sc.hour == 9]
        mixed = [sc for sc in inp.solcast_slots if sc.hour != 9]
        mixed.append(SolcastSlot(hour=9, pv_estimate=1.2))
        assert len(hour_9) == 4

        out = run_planner(dataclasses.replace(inp, solcast_slots=mixed))

        nine = [s.solcast_pv_estimate_kwh for s in out.slots if s.start.hour == 9]
        assert nine == pytest.approx([0.3, 0.3, 0.3, 0.3])
        assert out.data_quality.today_pv_missing_hours == []


class TestMissingSubHourlyPoints:
    """A slot without PV data is reported, not silently planned as zero."""

    def test_missing_slots_are_reported_in_data_quality(self) -> None:
        inp = _input_for(15, _HALF_HOURLY)
        kept = [sc for sc in inp.solcast_slots if sc.hour != 9]

        out = run_planner(dataclasses.replace(inp, solcast_slots=kept))

        assert out.data_quality.today_pv_missing_hours == [9]
        assert "hour_09" in out.missing_inputs
        assert not out.data_quality.is_complete
        assert [
            s.solcast_pv_estimate_kwh for s in out.slots if s.start.hour == 9
        ] == pytest.approx([0.0, 0.0, 0.0, 0.0])

    def test_one_missing_slot_flags_its_hour_and_keeps_the_others(self) -> None:
        inp = _input_for(15, _HALF_HOURLY)
        kept = [sc for sc in inp.solcast_slots if sc.slot_in_day != 37]

        out = run_planner(dataclasses.replace(inp, solcast_slots=kept))

        assert out.data_quality.today_pv_missing_hours == [9]
        assert [
            s.solcast_pv_estimate_kwh for s in out.slots if s.start.hour == 9
        ] == pytest.approx([0.25, 0.0, 0.35, 0.35])

    def test_complete_sub_hourly_input_reports_nothing(self) -> None:
        out = run_planner(_input_for(15, _HALF_HOURLY))

        assert out.data_quality.today_pv_missing_hours == []


def _utc_half_hours(first: datetime, count: int) -> dict[str, Any]:
    """Return *count* half-hourly points from *first*, value 0.1 × (index + 1)."""
    return _source(
        "detailedForecast",
        first.astimezone(UTC),
        30,
        [round(0.1 * (i + 1), 1) for i in range(count)],
    )


class TestDstDays:
    """Each physical slot takes its own half-hour on both DST transition days."""

    @pytest.mark.parametrize(
        ("now", "slot_count"),
        [
            pytest.param(datetime(2026, 3, 29, 0, 5, tzinfo=_TZ), 92, id="spring"),
            pytest.param(datetime(2026, 10, 25, 0, 5, tzinfo=_TZ), 100, id="autumn"),
        ],
    )
    def test_every_slot_gets_the_half_hour_that_contains_it(
        self, now: datetime, slot_count: int
    ) -> None:
        midnight = now.replace(minute=0)
        source = _utc_half_hours(midnight, slot_count // 2)

        inp = _input_for(15, source, now=now)
        out = run_planner(inp)

        assert len(out.slots) == slot_count
        keys = [(sc.day_offset, sc.slot_in_day) for sc in inp.solcast_slots]
        assert len(set(keys)) == slot_count
        for slot in out.slots:
            elapsed = slot.start.astimezone(UTC) - midnight.astimezone(UTC)
            half_hour = int(elapsed.total_seconds() // 1800)
            assert slot.solcast_pv_estimate_kwh == pytest.approx(
                round(0.1 * (half_hour + 1) * 0.25, 3)
            ), slot.start.isoformat()
        assert out.data_quality.today_pv_missing_hours == []

    def test_repeated_hour_keeps_both_occurrences_apart(self) -> None:
        """The fall-back day's two 02:00 hours hold different half-hours."""
        now = datetime(2026, 10, 25, 0, 5, tzinfo=_TZ)
        source = _utc_half_hours(now.replace(minute=0), 50)

        out = run_planner(_input_for(15, source, now=now))

        first = [
            s.solcast_pv_estimate_kwh
            for s in out.slots
            if s.start.astimezone(UTC).hour == 0 and s.start.hour == 2
        ]
        second = [
            s.solcast_pv_estimate_kwh
            for s in out.slots
            if s.start.astimezone(UTC).hour == 1 and s.start.hour == 2
        ]
        assert first == pytest.approx([0.125, 0.125, 0.15, 0.15], abs=1e-3)
        assert second == pytest.approx([0.175, 0.175, 0.2, 0.2], abs=1e-3)


def _hour_slots(hour: datetime, interval_minutes: int) -> list[PlannedSlot]:
    """Return empty planned slots covering *hour*."""
    step = timedelta(minutes=interval_minutes)
    return [
        PlannedSlot(start=hour + step * i, end=hour + step * (i + 1))
        for i in range(60 // interval_minutes)
    ]


class TestSolarCorrectorAndTracking:
    """The corrector stays per hour; forecast tracking stays per slot."""

    def test_hour_factor_applies_to_each_sub_hourly_slot(self) -> None:
        inp = _input_for(15, _HALF_HOURLY)
        tsi = TimeSeriesIndex.from_now(_NOW, interval_minutes=15, horizon_hours=24)
        slots = [PlannedSlot(start=meta.start, end=meta.end) for meta in tsi]
        corrector = SolarForecastCorrector(hour_factors={8: 0.5})

        populate_solcast(slots, inp.solcast_slots, 15, tsi, corrector=corrector)

        eight = [s.solcast_pv_estimate_kwh for s in slots if s.start.hour == 8]
        nine = [s.solcast_pv_estimate_kwh for s in slots if s.start.hour == 9]
        assert eight == pytest.approx([0.05, 0.05, 0.1, 0.1])
        assert nine == pytest.approx([0.25, 0.25, 0.35, 0.35])

    def test_each_slot_registers_its_own_forecast(self) -> None:
        out = run_planner(_input_for(15, _HALF_HOURLY))
        tracker = ForecastTracker(max_slots=96)

        register_forecasts_from_planner(out, tracker, now=_NOW)

        forecasts = [
            record.forecast_pv_kwh
            for record in tracker.records
            if _H8 <= record.start < _H8 + timedelta(hours=1)
        ]
        assert forecasts == pytest.approx([0.1, 0.1, 0.2, 0.2])

    def test_sub_hourly_entries_without_an_index_use_the_hours_mean(self) -> None:
        """The index-free path has no slot keys, so it plans the hour's mean."""
        slots = _hour_slots(_H8, 15)
        entries = [
            SolcastSlot(hour=8, pv_estimate=value, slot_in_day=32 + i)
            for i, value in enumerate([0.4, 0.4, 0.8, 0.8])
        ]

        populate_solcast(slots, entries, 15, None)

        assert [s.solcast_pv_estimate_kwh for s in slots] == pytest.approx(
            [0.15, 0.15, 0.15, 0.15]
        )

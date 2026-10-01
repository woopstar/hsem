"""Issue #1219 — a missing quarter-hour price is estimated from the same quarter.

With a quarter-hourly price source the missing-price estimate (issue #1002)
gave all four quarters of an estimated hour one price: the **last** quarter of
the earlier day's hour.  Tomorrow, which is estimated every day until the
day-ahead prices publish, was planned without the intra-hour shape and biased
towards the ``:45`` price.

The estimate now reads the same wall-clock quarter of the nearest earlier day.
``slot_in_day`` counts real steps since local midnight (issue #1160), so on a
DST day the same index is a different clock time; these tests therefore run on
both transition days, through the real populator, ``build_planner_input`` and
``run_planner``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import homeassistant.util.dt as dt_util

from custom_components.hsem.coordinator_builder import (
    build_planner_input,
    generate_recommendation_intervals,
)
from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    populate_price_and_solcast_from_snapshot,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.milp_optimizer import is_scipy_available

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_TZ = ZoneInfo("Europe/Copenhagen")
_IMPORT = "sensor.import_price"
_EXPORT = "sensor.export_price"
#: An ordinary day, the 25-hour fall-back day and the 23-hour spring-forward day.
_ORDINARY = datetime(2026, 6, 1, tzinfo=_TZ)
_FALL_BACK = datetime(2026, 10, 25, tzinfo=_TZ)
_SPRING_FORWARD = datetime(2026, 3, 29, tzinfo=_TZ)
#: Second pass of the repeated fall-back hour costs this much more.
_SECOND_PASS = 0.5


@pytest.fixture(autouse=True)
def copenhagen_ha_tz() -> Iterator[None]:
    """Run each test with Home Assistant's local timezone set to Copenhagen."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(_TZ)
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


def _clock_price(hour: int, minute: int) -> float:
    """Return a price that identifies its wall-clock quarter: 10:15 is 1.1015."""
    return round(1.0 + hour / 100.0 + minute / 10000.0, 6)


def _day_prices(day_start: datetime, *, offset: float = 0.0) -> dict[str, Any]:
    """Return a quarter-hourly price sensor's attributes for one local day.

    Walks real time, so the fall-back day has 100 points and the
    spring-forward day 92.  The second pass of the repeated hour is dearer.
    """
    end = datetime.combine(day_start.date() + timedelta(days=1), time(0), _TZ)
    moment = day_start.astimezone(UTC)
    points = []
    while moment < end.astimezone(UTC):
        local = moment.astimezone(_TZ)
        price = _clock_price(local.hour, local.minute)
        points.append(
            {
                "start": local.isoformat(),
                "price": round(
                    price + (_SECOND_PASS if local.fold else 0.0) + offset, 6
                ),
            }
        )
        moment += timedelta(minutes=15)
    return {"prices_today": points}


def _tomorrow(day_start: datetime) -> datetime:
    """Return local midnight of the day after *day_start*."""
    return datetime.combine(day_start.date() + timedelta(days=1), time(0), _TZ)


def _plan_with_todays_prices(today: datetime) -> PlannerOutput:
    """Plan at 09:05 on *today* with prices published for that day only."""
    now = today.replace(hour=9, minute=5)
    cfg = SensorConfig()
    cfg.recommendation_interval_minutes = 15
    cfg.recommendation_interval_length = 48
    cfg.electricity_price_update_interval = 15
    cfg.import_electricity_price_sensor = _IMPORT
    cfg.export_electricity_price_sensor = _EXPORT
    attributes = {_IMPORT: _day_prices(today), _EXPORT: _day_prices(today, offset=-0.5)}
    with patch("homeassistant.util.dt.now", return_value=now):
        recs = generate_recommendation_intervals(15, 48)
        coverage = populate_price_and_solcast_from_snapshot(
            recs, StateSnapshot(live=LiveState(), sensor_attributes=attributes), cfg
        )
        planner_input = build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=recs,
            batteries_schedules=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
            forecast_coverage=coverage,
        )
        assert {point.day_offset for point in planner_input.price_points} == {0}
        return run_planner(planner_input)


def _slots_of_tomorrow(output: PlannerOutput, today: datetime) -> list[PlannedSlot]:
    start = _tomorrow(today)
    end = _tomorrow(start)
    return [slot for slot in output.slots if start <= slot.start < end]


class TestOrdinaryDay:
    """Prices for today only: the state every morning before the auction."""

    def test_each_quarter_gets_todays_price_for_the_same_quarter(self) -> None:
        """Before: 1.1045 for 10:00, 10:15, 10:30 and 10:45 alike."""
        output = _plan_with_todays_prices(_ORDINARY)

        tomorrow = _slots_of_tomorrow(output, _ORDINARY)
        assert len(tomorrow) == 96
        for slot in tomorrow:
            expected = _clock_price(slot.start.hour, slot.start.minute)
            assert slot.price.import_price == pytest.approx(expected)
            assert slot.price.export_price == pytest.approx(expected - 0.5)

    def test_the_quarters_of_an_estimated_hour_differ(self) -> None:
        output = _plan_with_todays_prices(_ORDINARY)

        ten = [
            slot.price.import_price
            for slot in _slots_of_tomorrow(output, _ORDINARY)
            if slot.start.hour == 10
        ]
        assert ten == pytest.approx([1.10, 1.1015, 1.103, 1.1045])

    def test_the_gap_is_still_reported(self) -> None:
        output = _plan_with_todays_prices(_ORDINARY)

        quality = output.data_quality
        assert quality.today_price_missing_hours == []
        assert quality.tomorrow_price_missing_hours == list(range(24))
        assert quality.is_complete is False


class TestFallBackDay:
    """25 hours: ``slot_in_day`` runs four ahead of an ordinary day after 03:00."""

    def test_tomorrow_reads_the_same_clock_time_of_the_long_day(self) -> None:
        """Today is the long day; 10:15 is its slot 45, tomorrow's slot 41."""
        output = _plan_with_todays_prices(_FALL_BACK)

        tomorrow = _slots_of_tomorrow(output, _FALL_BACK)
        assert len(tomorrow) == 96
        for slot in tomorrow:
            if slot.start.hour == 2:
                continue
            assert slot.price.import_price == pytest.approx(
                _clock_price(slot.start.hour, slot.start.minute)
            )

    def test_the_repeated_hour_is_estimated_from_both_passes(self) -> None:
        """02:15 happened twice today, at 1.0215 and 1.5215: their mean."""
        output = _plan_with_todays_prices(_FALL_BACK)

        for slot in _slots_of_tomorrow(output, _FALL_BACK):
            if slot.start.hour == 2:
                assert slot.price.import_price == pytest.approx(
                    _clock_price(2, slot.start.minute) + _SECOND_PASS / 2.0
                )

    def test_both_passes_of_tomorrows_repeated_hour_read_todays_hour(self) -> None:
        """Tomorrow is the long day: 100 slots, both 02:15 slots get 1.0215."""
        today = _FALL_BACK - timedelta(days=1)
        output = _plan_with_todays_prices(today)

        tomorrow = _slots_of_tomorrow(output, today)
        assert len(tomorrow) == 100
        assert sum(slot.start.hour == 2 for slot in tomorrow) == 8
        for slot in tomorrow:
            assert slot.price.import_price == pytest.approx(
                _clock_price(slot.start.hour, slot.start.minute)
            )


class TestSpringForwardDay:
    """23 hours: ``slot_in_day`` runs four behind an ordinary day after 03:00."""

    def test_tomorrow_reads_the_same_clock_time_of_the_short_day(self) -> None:
        """Today is the short day; 10:15 is its slot 37, tomorrow's slot 41."""
        output = _plan_with_todays_prices(_SPRING_FORWARD)

        tomorrow = _slots_of_tomorrow(output, _SPRING_FORWARD)
        assert len(tomorrow) == 96
        for slot in tomorrow:
            if slot.start.hour == 2:
                continue
            assert slot.price.import_price == pytest.approx(
                _clock_price(slot.start.hour, slot.start.minute)
            )

    def test_the_hour_the_short_day_skipped_has_nothing_to_read(self) -> None:
        """02:00-03:00 did not exist today: the documented 0.0, reported missing."""
        output = _plan_with_todays_prices(_SPRING_FORWARD)

        skipped = [
            slot
            for slot in _slots_of_tomorrow(output, _SPRING_FORWARD)
            if slot.start.hour == 2
        ]
        assert len(skipped) == 4
        assert all(slot.price.import_price == pytest.approx(0.0) for slot in skipped)
        assert 2 in output.data_quality.tomorrow_price_missing_hours

    def test_tomorrows_short_day_reads_todays_clock_times(self) -> None:
        """Tomorrow is the short day: 92 slots, none at 02:xx."""
        today = _SPRING_FORWARD - timedelta(days=1)
        output = _plan_with_todays_prices(today)

        tomorrow = _slots_of_tomorrow(output, today)
        assert len(tomorrow) == 92
        assert not any(slot.start.hour == 2 for slot in tomorrow)
        for slot in tomorrow:
            assert slot.price.import_price == pytest.approx(
                _clock_price(slot.start.hour, slot.start.minute)
            )

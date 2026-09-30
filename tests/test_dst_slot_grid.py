"""Regression tests for issue #1160: the slot grid on DST days.

The recommendation grid and the planner's ``TimeSeriesIndex`` stepped from
local midnight with aware-datetime ``+ timedelta``, which is wall-clock
arithmetic in ``ZoneInfo`` zones.  The spring-forward day gained four slots
at the non-existent 02:xx times (duplicating 03:xx in UTC), and the
fall-back day lost its repeated hour.  Every assertion here compares
physical (UTC) instants, because slots sharing one ``ZoneInfo`` compare by
wall clock and hide the bug.

Timezone under test: ``Europe/Copenhagen``
  - spring forward: 2026-03-29 02:00 → 03:00 (UTC+1 → UTC+2), 23 h day
  - fall back:      2026-10-25 03:00 → 02:00 (UTC+2 → UTC+1), 25 h day
"""

from __future__ import annotations

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
from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    _populate_from_attributes,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.planner.slot_population import populate_prices
from custom_components.hsem.utils.datetime_utils import (
    future_slot_indices,
    physical_slot_grid,
    slot_position,
)

_TZ = ZoneInfo("Europe/Copenhagen")
_SPRING = datetime(2026, 3, 29, 12, 0, tzinfo=_TZ)
_AUTUMN = datetime(2026, 10, 25, 12, 0, tzinfo=_TZ)
_ORDINARY = datetime(2026, 6, 1, 12, 0, tzinfo=_TZ)


@pytest.fixture
def copenhagen_ha_tz() -> Iterator[None]:
    """Run the test with Home Assistant's local timezone set to Copenhagen."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(_TZ)
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


def _utc_starts(bounds: list[tuple[datetime, datetime]]) -> list[datetime]:
    return [start.astimezone(UTC) for start, _ in bounds]


class TestPhysicalSlotGrid:
    """``physical_slot_grid`` steps in real time and covers the local day."""

    @pytest.mark.parametrize(
        ("anchor", "interval", "expected"),
        [
            pytest.param(_SPRING, 15, 92, id="spring_15"),
            pytest.param(_AUTUMN, 15, 100, id="autumn_15"),
            pytest.param(_ORDINARY, 15, 96, id="ordinary_15"),
            pytest.param(_AUTUMN, 60, 25, id="autumn_60"),
            pytest.param(_SPRING, 30, 46, id="spring_30"),
        ],
    )
    def test_slot_count(self, anchor: datetime, interval: int, expected: int) -> None:
        assert len(physical_slot_grid(anchor, interval, 24)) == expected

    @pytest.mark.parametrize("anchor", [_SPRING, _AUTUMN], ids=["spring", "autumn"])
    def test_every_slot_is_one_interval_of_real_time(self, anchor: datetime) -> None:
        bounds = physical_slot_grid(anchor, 15, 24)
        for start, end in bounds:
            assert end - start == timedelta(minutes=15)
            assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(minutes=15)
        starts = _utc_starts(bounds)
        assert len(set(starts)) == len(starts)
        assert starts == sorted(starts)

    def test_boundaries_sort_in_physical_order(self) -> None:
        """Sorting the boundaries directly keeps the fall-back hour in order."""
        starts = [start for start, _ in physical_slot_grid(_AUTUMN, 15, 24)]
        assert sorted(starts) == starts

    def test_local_labels_on_fall_back_day(self) -> None:
        bounds = physical_slot_grid(_AUTUMN, 60, 24)
        labels = [start.isoformat() for start, _ in bounds[1:5]]
        assert labels == [
            "2026-10-25T01:00:00+02:00",
            "2026-10-25T02:00:00+02:00",
            "2026-10-25T02:00:00+01:00",
            "2026-10-25T03:00:00+01:00",
        ]

    def test_horizon_ends_at_local_midnight(self) -> None:
        bounds = physical_slot_grid(_SPRING, 15, 48)
        assert bounds[0][0] == datetime(2026, 3, 29, 0, 0, tzinfo=_TZ)
        assert bounds[-1][1] == datetime(2026, 3, 31, 0, 0, tzinfo=_TZ)
        assert len(bounds) == 92 + 96

    def test_utc_anchor_has_no_dst(self) -> None:
        anchor = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
        assert len(physical_slot_grid(anchor, 15, 24)) == 96


class TestSlotPosition:
    """``slot_position`` counts real steps since the slot's local midnight."""

    def test_matches_wall_clock_index_on_ordinary_day(self) -> None:
        midnight = _ORDINARY.replace(hour=0)
        for start, _ in physical_slot_grid(_ORDINARY, 15, 48):
            day, index = slot_position(start, midnight, 15)
            assert index == (start.hour * 60 + start.minute) // 15
            assert day == (start.date() - midnight.date()).days

    @pytest.mark.parametrize(
        ("anchor", "per_day"),
        [
            pytest.param(_SPRING, 92, id="spring"),
            pytest.param(_AUTUMN, 100, id="autumn"),
        ],
    )
    def test_unique_and_dense_on_dst_day(self, anchor: datetime, per_day: int) -> None:
        midnight = anchor.replace(hour=0)
        positions = [
            slot_position(start, midnight, 15)
            for start, _ in physical_slot_grid(anchor, 15, 24)
        ]
        assert positions == [(0, i) for i in range(per_day)]

    def test_accepts_a_zoneinfo_fold(self) -> None:
        midnight = _AUTUMN.replace(hour=0)
        first = datetime(2026, 10, 25, 2, 0, tzinfo=_TZ, fold=0)
        second = datetime(2026, 10, 25, 2, 0, tzinfo=_TZ, fold=1)
        assert slot_position(first, midnight, 60) == (0, 2)
        assert slot_position(second, midnight, 60) == (0, 3)


class TestFutureSlotIndicesInRepeatedHour:
    """The MILP slot filter compares physical instants (issue #1160)."""

    @pytest.mark.parametrize(
        ("fold", "first_future"),
        [
            # 02:30 UTC+2: the live slot is the first 02:00 (index 2), whose
            # end reads 02:00 on the wall clock but is still in the future.
            pytest.param(0, 2, id="first_occurrence"),
            # 02:30 UTC+1: slots 0-2 have ended; the live slot is index 3.
            pytest.param(1, 3, id="second_occurrence"),
        ],
    )
    def test_repeated_hour(self, fold: int, first_future: int) -> None:
        ends = [end for _, end in physical_slot_grid(_AUTUMN, 60, 24)]
        now = datetime(2026, 10, 25, 2, 30, tzinfo=_TZ, fold=fold)
        assert future_slot_indices(ends, now)[0] == first_future


class TestRecommendationGrid:
    """The coordinator's recommendation grid matches the planner's 1:1."""

    @pytest.mark.parametrize(
        ("anchor", "expected"),
        [
            pytest.param(_SPRING, 92, id="spring"),
            pytest.param(_AUTUMN, 100, id="autumn"),
        ],
    )
    def test_matches_time_series_index(self, anchor: datetime, expected: int) -> None:
        with patch("homeassistant.util.dt.now", return_value=anchor):
            recs = generate_recommendation_intervals(15, 24)
        tsi = TimeSeriesIndex.from_now(anchor, interval_minutes=15, horizon_hours=24)
        assert len(recs) == expected
        assert [r.start.astimezone(UTC) for r in recs] == [
            m.start.astimezone(UTC) for m in tsi
        ]
        assert [r.end.astimezone(UTC) for r in recs] == [
            m.end.astimezone(UTC) for m in tsi
        ]


def _autumn_hourly_recs() -> list[HourlyRecommendation]:
    with patch("homeassistant.util.dt.now", return_value=_AUTUMN):
        return generate_recommendation_intervals(60, 24)


def _autumn_price_attributes() -> dict[str, Any]:
    """A Nord Pool-style price array for the 25 h fall-back day.

    Each physical hour gets its own price (0.10, 0.11, …), with the offset
    written into the timestamp the way price integrations publish it.
    """
    first = datetime(2026, 10, 25, 0, 0, tzinfo=_TZ).astimezone(UTC)
    return {
        "prices": [
            {
                "start": (first + timedelta(hours=i)).astimezone(_TZ).isoformat(),
                "price": round(0.10 + i * 0.01, 2),
            }
            for i in range(25)
        ]
    }


class TestFallBackPrices:
    """Both hour-2 prices on the fall-back day reach their own slot."""

    @pytest.mark.usefixtures("copenhagen_ha_tz")
    def test_attribute_matching_keeps_both_hour_two_prices(self) -> None:
        recs = _autumn_hourly_recs()

        matched = _populate_from_attributes(
            _autumn_price_attributes(), recs, "import_price", "pv50", 60
        )

        assert matched == 25
        assert [r.import_price for r in recs] == pytest.approx(
            [round(0.10 + i * 0.01, 2) for i in range(25)]
        )

    @pytest.mark.usefixtures("copenhagen_ha_tz")
    def test_both_hour_two_prices_reach_the_planner(self) -> None:
        recs = _autumn_hourly_recs()
        _populate_from_attributes(
            _autumn_price_attributes(), recs, "import_price", "pv50", 60
        )
        cfg = SensorConfig()
        cfg.recommendation_interval_minutes = 60
        cfg.recommendation_interval_length = 24

        with patch("homeassistant.util.dt.now", return_value=_AUTUMN):
            inp = build_planner_input(
                cfg=cfg,
                live=LiveState(),
                hourly_recommendations=recs,
                batteries_schedules=[],
                previous_winner_name=None,
                previous_winner_score=0.0,
            )

        keys = [(pp.day_offset, pp.slot_in_day) for pp in inp.price_points]
        assert keys == [(0, i) for i in range(25)]

        tsi = TimeSeriesIndex.from_now(_AUTUMN, interval_minutes=60, horizon_hours=24)
        slots = [PlannedSlot(start=m.start, end=m.end) for m in tsi]
        populate_prices(slots, inp.price_points, tsi=tsi)

        hour_two = [s for s in slots if s.start.hour == 2]
        assert [s.price.import_price for s in hour_two] == pytest.approx([0.12, 0.13])
        assert not tsi.missing_price_slots

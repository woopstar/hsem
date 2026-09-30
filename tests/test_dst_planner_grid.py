"""Regression tests for issue #1169: the planner's own slot grid on DST days.

``build_planner_input`` serialises the coordinator's ``now`` as ``now_iso``,
and ``run_planner`` parsed it back with ``datetime.fromisoformat``, which
yields a fixed UTC offset with no DST rules.  ``TimeSeriesIndex.from_now``
then built the planner grid in that fixed-offset frame: 96 × 15-min slots
on both DST days, shifted by an hour before the transition, and out of step
with the recommendation grid and the ``(day_offset, slot_in_day)`` price
keys.  The #1160 tests passed a ``ZoneInfo`` ``now`` straight into
``TimeSeriesIndex`` and could not see it, so these tests go through the
production path (``build_planner_input`` → ``run_planner``) and assert in
UTC.

Timezone under test: ``Europe/Copenhagen``
  - spring forward: 2026-03-29 02:00 → 03:00 (UTC+1 → UTC+2), 92 slots
  - fall back:      2026-10-25 03:00 → 02:00 (UTC+2 → UTC+1), 100 slots
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import homeassistant.util.dt as dt_util

from custom_components.hsem.coordinator_builder import (
    build_planner_input,
    generate_recommendation_intervals,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.engine_population import _parse_now
from custom_components.hsem.utils.diagnostics import _planner_input_to_dict
from tests.planner.fixtures import make_winter_day_input

_TZ = ZoneInfo("Europe/Copenhagen")

_NOWS = [
    pytest.param(datetime(2026, 10, 25, 0, 30, tzinfo=_TZ), 100, id="autumn_0030"),
    pytest.param(datetime(2026, 10, 25, 2, 30, tzinfo=_TZ), 100, id="autumn_0230_f0"),
    pytest.param(
        datetime(2026, 10, 25, 2, 30, tzinfo=_TZ, fold=1), 100, id="autumn_0230_f1"
    ),
    pytest.param(datetime(2026, 10, 25, 12, 0, tzinfo=_TZ), 100, id="autumn_1200"),
    pytest.param(datetime(2026, 3, 29, 0, 30, tzinfo=_TZ), 92, id="spring_0030"),
    pytest.param(datetime(2026, 3, 29, 12, 0, tzinfo=_TZ), 92, id="spring_1200"),
    pytest.param(datetime(2026, 6, 1, 12, 0, tzinfo=_TZ), 96, id="ordinary_1200"),
]


@pytest.fixture
def copenhagen_ha_tz() -> Iterator[None]:
    """Run the test with Home Assistant's local timezone set to Copenhagen."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(_TZ)
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


def _priced_recs(now: datetime) -> list[HourlyRecommendation]:
    """Return the 15-min recommendation grid with a distinct price per slot."""
    with patch("homeassistant.util.dt.now", return_value=now):
        recs = generate_recommendation_intervals(15, 24)
    for i, rec in enumerate(recs):
        rec.import_price = round(1.0 + i / 100.0, 2)
        rec.export_price = 0.05
    return recs


def _planner_input(now: datetime, recs: list[HourlyRecommendation]) -> PlannerInput:
    """Build the planner input exactly as the coordinator does."""
    cfg = SensorConfig()
    cfg.recommendation_interval_minutes = 15
    cfg.recommendation_interval_length = 24
    with patch("homeassistant.util.dt.now", return_value=now):
        return build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=recs,
            previous_winner_name=None,
            previous_winner_score=0.0,
        )


@pytest.mark.usefixtures("copenhagen_ha_tz")
class TestPlannerGridMatchesRecommendations:
    """``run_planner`` plans on the same physical grid as the coordinator."""

    @pytest.mark.parametrize(("now", "expected"), _NOWS)
    def test_slot_boundaries_match_in_utc(self, now: datetime, expected: int) -> None:
        recs = _priced_recs(now)
        inp = _planner_input(now, recs)

        out = run_planner(inp)

        assert inp.time_zone == "Europe/Copenhagen"
        assert len(recs) == expected
        assert [s.start.astimezone(UTC) for s in out.slots] == [
            r.start.astimezone(UTC) for r in recs
        ]
        assert [s.end.astimezone(UTC) for s in out.slots] == [
            r.end.astimezone(UTC) for r in recs
        ]

    @pytest.mark.parametrize(("now", "expected"), _NOWS)
    def test_each_slot_gets_its_own_price(self, now: datetime, expected: int) -> None:
        recs = _priced_recs(now)

        out = run_planner(_planner_input(now, recs))

        assert len(out.slots) == expected
        assert [s.price.import_price for s in out.slots] == pytest.approx(
            [r.import_price for r in recs]
        )

    def test_live_slot_on_second_occurrence(self) -> None:
        """At 02:30 fold=1 the live slot is the second 02:30, i.e. 01:30Z."""
        now = datetime(2026, 10, 25, 2, 30, tzinfo=_TZ, fold=1)

        out = run_planner(_planner_input(now, _priced_recs(now)))

        live = [
            s
            for s in out.slots
            if s.start.astimezone(UTC) <= now.astimezone(UTC) < s.end.astimezone(UTC)
        ]
        assert [s.start.astimezone(UTC) for s in live] == [
            datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
        ]
        assert out.current_recommendation == live[0].recommendation


@pytest.mark.usefixtures("copenhagen_ha_tz")
def test_without_time_zone_the_grid_stays_fixed_offset() -> None:
    """``time_zone=None`` (old dumps) keeps the pre-#1169 fixed-offset grid."""
    now = datetime(2026, 10, 25, 12, 0, tzinfo=_TZ)
    inp = dataclasses.replace(_planner_input(now, _priced_recs(now)), time_zone=None)

    out = run_planner(inp)

    assert len(out.slots) == 96
    assert out.slots[0].start.astimezone(UTC) == datetime(
        2026, 10, 24, 23, 0, tzinfo=UTC
    )


class TestParseNow:
    """``_parse_now`` re-expresses the ISO instant in the named zone."""

    def test_zone_keeps_the_instant_and_sets_fold(self) -> None:
        now = _parse_now("2026-10-25T02:30:00+01:00", "Europe/Copenhagen")

        assert now.tzinfo == _TZ
        assert now.fold == 1
        assert now.astimezone(UTC) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)

    def test_first_occurrence_has_fold_zero(self) -> None:
        now = _parse_now("2026-10-25T02:30:00+02:00", "Europe/Copenhagen")

        assert now.fold == 0
        assert now.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)

    @pytest.mark.parametrize("zone", [None, ""])
    def test_no_zone_returns_the_fixed_offset(self, zone: str | None) -> None:
        now = _parse_now("2026-10-25T12:00:00+01:00", zone)

        assert now.tzinfo == timezone(timedelta(hours=1))

    @pytest.mark.parametrize("zone", ["Mars/Olympus_Mons", "../etc/passwd"])
    def test_unknown_zone_falls_back_to_the_fixed_offset(self, zone: str) -> None:
        now = _parse_now("2026-10-25T12:00:00+01:00", zone)

        assert now.tzinfo == timezone(timedelta(hours=1))
        assert now.astimezone(UTC) == datetime(2026, 10, 25, 11, 0, tzinfo=UTC)

    def test_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _parse_now("2026-10-25T12:00:00", "Europe/Copenhagen")


@pytest.mark.usefixtures("copenhagen_ha_tz")
def test_time_zone_survives_the_diagnostics_dump() -> None:
    """Diagnostics dumps carry ``time_zone`` so a replay plans the same grid."""
    now = datetime(2026, 10, 25, 12, 0, tzinfo=_TZ)
    dumped = _planner_input_to_dict(_planner_input(now, _priced_recs(now)))

    assert dumped["time_zone"] == "Europe/Copenhagen"
    assert dumped["now_iso"] == "2026-10-25T12:00:00+01:00"


@pytest.mark.usefixtures("copenhagen_ha_tz")
def test_fixed_offset_now_leaves_time_zone_unset() -> None:
    """A ``now`` without an IANA zone (tests, UTC hosts) sets no time_zone."""
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))

    inp = _planner_input(now, _priced_recs(now))

    assert inp.time_zone is None


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(
            datetime(2026, 10, 25, 2, 30, tzinfo=_TZ, fold=1), 100, id="autumn"
        ),
        pytest.param(datetime(2026, 3, 29, 1, 30, tzinfo=_TZ), 92, id="spring"),
    ],
)
@pytest.mark.parametrize("interval", [15, 60])
def test_realistic_dst_plan_passes_the_consistency_gate(
    now: datetime, expected: int, interval: int
) -> None:
    """A full plan on a DST day keeps energy balance and SoC bounds."""
    inp = dataclasses.replace(
        make_winter_day_input(),
        now_iso=now.isoformat(),
        time_zone="Europe/Copenhagen",
        interval_minutes=interval,
        battery_soc_pct=40.0,
    )

    out = run_planner(inp)

    assert len(out.slots) == expected * 15 // interval
    assert not [w for w in out.warnings if "self-consistency" in w]
    usable_max = inp.battery_rated_capacity_kwh
    for slot in out.slots:
        assert -1e-6 <= slot.estimated_battery_capacity_kwh <= usable_max + 1e-6

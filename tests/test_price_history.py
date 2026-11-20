"""Issue #1434 — a persisted price history fills unpublished and missing prices.

HSEM only knew the prices its price sensors exposed right now.  A slot without
a price was estimated from the nearest earlier day in the same payload, and a
price sensor outage left nothing to estimate from: the whole horizon was
planned at ``0.0`` while degraded mode stayed ``ok``.

The price history records the real price of every slot that has started and
keeps the last published prices.  The planner reads its weekday and
time-of-day profile before the nearest-earlier-day estimate, and an outage is
planned from the held and stored prices and reported through degraded mode.

The end-to-end tests run through the real populator,
``update_price_history``, ``build_planner_input`` and ``run_planner``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

import homeassistant.util.dt as dt_util

from custom_components.hsem.coordinator_builder import (
    build_planner_input,
    generate_recommendation_intervals,
)
from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.coordinator_price_history import (
    async_apply_price_history,
    init_price_history_tracker,
    update_price_history,
)
from custom_components.hsem.custom_sensors.degraded_mode_sensor import (
    HSEMDegradedModeSensor,
)
from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    populate_price_and_solcast_from_snapshot,
)
from custom_components.hsem.models.forecast_coverage import ForecastCoverage
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.price_history_tracker import (
    PRICE_HISTORY_RETENTION_DAYS,
    PriceHistoryTracker,
)
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.price_profile import PriceProfile
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.planner.slot_population import (
    build_slots,
    build_time_series_index,
)
from custom_components.hsem.planner.slot_price_population import populate_prices
from custom_components.hsem.utils.degraded_mode import (
    DegradedMode,
    classify_degraded_mode,
    hardware_writes_allowed,
)
from custom_components.hsem.utils.diagnostics import _planner_input_to_dict
from tests.backtest.replay import planner_input_from_dict
from tests.test_dynamic_floor_reference_plan import (
    _CYCLE,
    _LOAD,
    _NOW as _FIXTURE_NOW,
    _coordinator,
    _plan,
    _populate_consumption,
    _snapshot,
)

_TZ = ZoneInfo("Europe/Copenhagen")
_IMPORT = "sensor.import_price"
_EXPORT = "sensor.export_price"
#: Local midnights.  1 June 2026 is a Monday.
_LAST_MONDAY = datetime(2026, 5, 25, tzinfo=_TZ)
_LAST_TUESDAY = datetime(2026, 5, 26, tzinfo=_TZ)
_MONDAY = datetime(2026, 6, 1, tzinfo=_TZ)
_TUESDAY = datetime(2026, 6, 2, tzinfo=_TZ)
#: The 25-hour fall-back day and the 23-hour spring-forward day (Sundays).
_FALL_BACK = datetime(2026, 10, 25, tzinfo=_TZ)
_SPRING_FORWARD = datetime(2026, 3, 29, tzinfo=_TZ)
#: The second pass of the repeated fall-back hour costs this much more.
_SECOND_PASS = 0.5
#: Export is this much below import in every published price.
_EXPORT_GAP = 0.5
_MON, _TUE, _SUN = 0, 1, 6


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


def _day_points(
    day_start: datetime, *, step: int = 15, offset: float = 0.0
) -> list[dict[str, Any]]:
    """Return one local day of price points, walking real time.

    The fall-back day has 25 hours of points and the spring-forward day 23.
    The second pass of the repeated hour is dearer.
    """
    end = datetime.combine(day_start.date() + timedelta(days=1), time(0), _TZ)
    moment = day_start.astimezone(UTC)
    points = []
    while moment < end.astimezone(UTC):
        local = moment.astimezone(_TZ)
        price = _clock_price(local.hour, local.minute) + offset
        points.append(
            {
                "start": local.isoformat(),
                "price": round(price + (_SECOND_PASS if local.fold else 0.0), 6),
            }
        )
        moment += timedelta(minutes=step)
    return points


def _attributes(
    *days: datetime, step: int = 15, offset: float = 0.0
) -> dict[str, dict[str, Any]]:
    """Return import and export sensor attributes publishing *days*."""
    imports: list[dict[str, Any]] = []
    exports: list[dict[str, Any]] = []
    for day in days:
        imports += _day_points(day, step=step, offset=offset)
        exports += _day_points(day, step=step, offset=offset - _EXPORT_GAP)
    return {_IMPORT: {"prices_today": imports}, _EXPORT: {"prices_today": exports}}


def _cfg(interval: int = 15, source_step: int = 15) -> SensorConfig:
    cfg = SensorConfig()
    cfg.recommendation_interval_minutes = interval
    cfg.recommendation_interval_length = 48
    cfg.electricity_price_update_interval = source_step
    cfg.import_electricity_price_sensor = _IMPORT
    cfg.export_electricity_price_sensor = _EXPORT
    return cfg


def _cycle(
    tracker: PriceHistoryTracker,
    now: datetime,
    attributes: dict[str, dict[str, Any]],
    *,
    interval: int = 15,
    source_step: int = 15,
) -> tuple[PlannerInput, str | None]:
    """Run one cycle's price steps and return the planner input and outage."""
    cfg = _cfg(interval, source_step)
    with patch("homeassistant.util.dt.now", return_value=now):
        recs = generate_recommendation_intervals(interval, 48)
        coverage = populate_price_and_solcast_from_snapshot(
            recs, StateSnapshot(live=LiveState(), sensor_attributes=attributes), cfg
        )
        coverage, outage = update_price_history(tracker, recs, coverage, now, interval)
        planner_input = build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=recs,
            previous_winner_name=None,
            previous_winner_score=0.0,
            forecast_coverage=coverage,
            price_profile=tracker.profile(now.tzinfo),
        )
    return planner_input, outage


def _record_day(
    tracker: PriceHistoryTracker,
    day: datetime,
    *,
    interval: int = 15,
    source_step: int = 15,
    offset: float = 0.0,
) -> None:
    """Record the whole of *day* with a cycle at 23:59 that evening."""
    _cycle(
        tracker,
        day.replace(hour=23, minute=59),
        _attributes(day, step=source_step, offset=offset),
        interval=interval,
        source_step=source_step,
    )


def _slots_of(output: PlannerOutput, day: datetime) -> list[PlannedSlot]:
    end = datetime.combine(day.date() + timedelta(days=1), time(0), _TZ)
    return [slot for slot in output.slots if day <= slot.start < end]


def _populated(
    points: list[PricePoint],
    profile: PriceProfile | None,
    *,
    interval: int = 15,
    hours: int = 48,
) -> tuple[list[PlannedSlot], set[Any]]:
    """Run ``populate_prices`` on a Monday grid; return slots and missing keys."""
    now = _MONDAY.replace(hour=9, minute=5)
    inp = PlannerInput(
        now_iso=now.isoformat(),
        time_zone="Europe/Copenhagen",
        interval_minutes=interval,
        interval_length_hours=hours,
    )
    tsi = build_time_series_index(inp, now)
    slots = build_slots(inp, now)
    populate_prices(slots, points, tsi, profile)
    return slots, set(tsi.missing_price_slots)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class TestPriceProfile:
    """Mean real prices by local weekday, hour and quarter."""

    def test_an_empty_profile_knows_nothing(self) -> None:
        profile = PriceProfile.from_samples([], _TZ)

        assert profile.lookup(_MON, 10, 0, 15) is None

    def test_a_quarter_is_the_mean_of_its_weeks(self) -> None:
        profile = PriceProfile.from_samples(
            [
                (_LAST_MONDAY.replace(hour=10, minute=15), 15, 1.0, 0.4),
                (_MONDAY.replace(hour=10, minute=15), 15, 2.0, 0.8),
            ],
            _TZ,
        )

        assert profile.lookup(_MON, 10, 15, 15) == pytest.approx((1.5, 0.6))

    def test_a_single_sample_is_kept_exactly(self) -> None:
        profile = PriceProfile.from_samples(
            [(_MONDAY.replace(hour=10), 15, 1.23456, 0.1)], _TZ
        )

        assert profile.lookup(_MON, 10, 0, 15) == (1.23456, 0.1)

    def test_other_weekdays_and_hours_are_unknown(self) -> None:
        profile = PriceProfile.from_samples(
            [(_MONDAY.replace(hour=10), 60, 1.0, 0.5)], _TZ
        )

        assert profile.lookup(_TUE, 10, 0, 15) is None
        assert profile.lookup(_MON, 11, 0, 15) is None

    def test_an_hourly_sample_prices_all_four_quarters(self) -> None:
        profile = PriceProfile.from_samples(
            [(_MONDAY.replace(hour=10), 60, 1.0, 0.5)], _TZ
        )

        for minute in (0, 15, 30, 45):
            assert profile.lookup(_MON, 10, minute, 15) == pytest.approx((1.0, 0.5))

    def test_a_wider_slot_reads_the_mean_of_its_quarters(self) -> None:
        ten = _MONDAY.replace(hour=10)
        profile = PriceProfile.from_samples(
            [
                (ten + timedelta(minutes=minute), 15, price, price - 0.5)
                for minute, price in ((0, 1.0), (15, 2.0), (30, 3.0), (45, 4.0))
            ],
            _TZ,
        )

        assert profile.lookup(_MON, 10, 0, 60) == pytest.approx((2.5, 2.0))
        assert profile.lookup(_MON, 10, 30, 30) == pytest.approx((3.5, 3.0))

    def test_an_unknown_quarter_falls_back_to_the_hour(self) -> None:
        profile = PriceProfile.from_samples(
            [
                (_MONDAY.replace(hour=10, minute=0), 15, 1.0, 0.5),
                (_MONDAY.replace(hour=10, minute=15), 15, 2.0, 1.0),
            ],
            _TZ,
        )

        assert profile.lookup(_MON, 10, 45, 15) == pytest.approx((1.5, 0.75))

    def test_switching_from_hourly_to_quarter_hourly_prices(self) -> None:
        """Both kinds of sample land on the same quarter keys."""
        profile = PriceProfile.from_samples(
            [
                (_LAST_MONDAY.replace(hour=10), 60, 1.0, 0.5),
                (_MONDAY.replace(hour=10, minute=0), 15, 2.0, 1.5),
                (_MONDAY.replace(hour=10, minute=15), 15, 3.0, 2.5),
            ],
            _TZ,
        )

        assert profile.lookup(_MON, 10, 0, 15) == pytest.approx((1.5, 1.0))
        assert profile.lookup(_MON, 10, 15, 15) == pytest.approx((2.0, 1.5))
        # Only the hourly week knows :30 and :45.
        assert profile.lookup(_MON, 10, 30, 15) == pytest.approx((1.0, 0.5))
        assert profile.lookup(_MON, 10, 0, 60) == pytest.approx((1.375, 0.875))

    def test_a_zero_width_sample_counts_as_one_quarter(self) -> None:
        profile = PriceProfile.from_samples(
            [(_MONDAY.replace(hour=10), 0, 1.0, 0.5)], _TZ
        )

        assert profile.lookup(_MON, 10, 0, 15) == pytest.approx((1.0, 0.5))
        assert set(profile.quarters) == {(_MON, 10, 0)}

    def test_the_weekday_is_read_in_the_local_zone(self) -> None:
        """Monday 00:30 in Copenhagen is still Sunday in UTC."""
        start = _MONDAY.replace(minute=30).astimezone(UTC)
        profile = PriceProfile.from_samples([(start, 15, 1.0, 0.5)], _TZ)

        assert profile.lookup(_MON, 0, 30, 15) == pytest.approx((1.0, 0.5))
        assert profile.lookup(_SUN, 22, 30, 15) is None


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class TestPriceHistoryTracker:
    """The rolling store and the held prices."""

    def test_the_latest_value_of_a_slot_wins(self) -> None:
        tracker = PriceHistoryTracker()
        start = _MONDAY.replace(hour=10)

        tracker.record(start, 15, 1.0, 0.5)
        tracker.record(start, 15, 2.0, 1.0)

        assert tracker.sample_count == 1
        assert tracker.profile(_TZ).lookup(_MON, 10, 0, 15) == pytest.approx((2.0, 1.0))

    def test_slots_are_keyed_by_instant(self) -> None:
        """Both passes of the fall-back hour are separate slots."""
        tracker = PriceHistoryTracker()
        first = _FALL_BACK.replace(hour=2, minute=15)

        tracker.record(first, 15, 1.0, 0.5)
        tracker.record(first.replace(fold=1), 15, 2.0, 1.0)

        assert tracker.sample_count == 2

    def test_old_prices_are_pruned(self) -> None:
        tracker = PriceHistoryTracker()
        now = _MONDAY.replace(hour=12)
        tracker.record(now - timedelta(days=PRICE_HISTORY_RETENTION_DAYS + 1), 15, 1, 1)
        tracker.record(now - timedelta(days=PRICE_HISTORY_RETENTION_DAYS - 1), 15, 2, 2)

        tracker.prune(now)

        assert tracker.sample_count == 1

    def test_the_profile_is_cached_until_the_history_changes(self) -> None:
        tracker = PriceHistoryTracker()
        tracker.record(_MONDAY.replace(hour=10), 15, 1.0, 0.5)

        first = tracker.profile(_TZ)
        assert tracker.profile(_TZ) is first
        assert tracker.profile(UTC) is not first

        tracker.record(_MONDAY.replace(hour=11), 15, 1.0, 0.5)
        assert tracker.profile(_TZ).lookup(_MON, 11, 0, 15) is not None

    def test_pruning_rebuilds_the_profile(self) -> None:
        tracker = PriceHistoryTracker(retention_days=1)
        tracker.record(_LAST_MONDAY.replace(hour=10), 15, 1.0, 0.5)
        assert tracker.profile(_TZ).lookup(_MON, 10, 0, 15) is not None

        tracker.prune(_MONDAY)

        assert tracker.profile(_TZ).lookup(_MON, 10, 0, 15) is None

    def test_held_prices_are_replaced_each_cycle(self) -> None:
        tracker = PriceHistoryTracker()
        ten = _MONDAY.replace(hour=10)
        eleven = _MONDAY.replace(hour=11)

        tracker.hold({ten: (1.0, 0.5)})
        tracker.hold({eleven: (2.0, 1.0)})

        assert tracker.held_price(ten) is None
        assert tracker.held_price(eleven.astimezone(UTC)) == (2.0, 1.0)

    @pytest.mark.asyncio
    async def test_state_survives_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / ".storage" / "hsem_price_history.json"
        tracker = PriceHistoryTracker(history_file=str(path))
        tracker.record(_MONDAY.replace(hour=10), 15, 1.25, 0.75)
        tracker.hold({_TUESDAY.replace(hour=8): (3.0, 2.5)})

        assert await tracker.save_history() is True

        restored = PriceHistoryTracker()
        hass = MagicMock()
        hass.config.config_dir = str(tmp_path)
        await init_price_history_tracker(restored, hass)

        assert restored.history_file == str(path)
        assert restored.sample_count == 1
        assert restored.profile(_TZ).lookup(_MON, 10, 0, 15) == pytest.approx(
            (1.25, 0.75)
        )
        assert restored.held_price(_TUESDAY.replace(hour=8)) == (3.0, 2.5)

    @pytest.mark.asyncio
    async def test_a_missing_file_leaves_the_tracker_empty(
        self, tmp_path: Path
    ) -> None:
        tracker = PriceHistoryTracker()
        hass = MagicMock()
        hass.config.config_dir = str(tmp_path)

        await init_price_history_tracker(tracker, hass)

        assert tracker.sample_count == 0
        assert tracker.history_file.endswith("hsem_price_history.json")

    @pytest.mark.asyncio
    async def test_nothing_is_written_without_a_file_or_a_change(
        self, tmp_path: Path
    ) -> None:
        assert await PriceHistoryTracker().save_history() is False

        path = tmp_path / "history.json"
        tracker = PriceHistoryTracker(history_file=str(path))
        assert await tracker.save_history() is True
        assert not path.exists()

        tracker.record(_MONDAY, 15, 1.0, 0.5)
        assert await tracker.save_history() is True
        written = path.stat().st_mtime_ns

        # The same price again is not a change.
        tracker.record(_MONDAY, 15, 1.0, 0.5)
        assert await tracker.save_history() is True
        assert path.stat().st_mtime_ns == written

    @pytest.mark.asyncio
    async def test_a_failed_write_is_retried(self, tmp_path: Path) -> None:
        path = tmp_path / "history.json"
        tracker = PriceHistoryTracker(history_file=str(path))
        tracker.record(_MONDAY, 15, 1.0, 0.5)

        with patch(
            "custom_components.hsem.models.price_history_tracker."
            "write_json_history_file",
            return_value=False,
        ):
            assert await tracker.save_history() is False

        assert await tracker.save_history() is True
        assert path.exists()

    @pytest.mark.asyncio
    async def test_a_revised_held_price_does_not_rewrite_the_file(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "history.json"
        tracker = PriceHistoryTracker(history_file=str(path))
        ten = _MONDAY.replace(hour=10)
        tracker.hold({ten: (1.0, 0.5)})
        await tracker.save_history()
        written = path.stat().st_mtime_ns

        tracker.hold({ten: (1.1, 0.6)})
        await tracker.save_history()
        assert path.stat().st_mtime_ns == written
        assert tracker.held_price(ten) == (1.1, 0.6)

        tracker.hold({ten: (1.1, 0.6), _MONDAY.replace(hour=11): (2.0, 1.0)})
        await tracker.save_history()
        assert json.loads(path.read_text())["held"].keys() == {
            ten.astimezone(UTC).isoformat(),
            _MONDAY.replace(hour=11).astimezone(UTC).isoformat(),
        }

    def test_invalid_persisted_entries_are_skipped(self) -> None:
        good = _MONDAY.replace(hour=10).astimezone(UTC).isoformat()
        tracker = PriceHistoryTracker.from_dict(
            {
                "samples": {
                    good: [15, 1.0, 0.5],
                    "not a date": [15, 1.0, 0.5],
                    "2026-06-01T11:00:00": [15, 1.0, 0.5],  # naive
                    "2026-06-01T12:00:00+00:00": [15, 1.0],
                    "2026-06-01T13:00:00+00:00": [15, "nan", 0.5],
                    "2026-06-01T14:00:00+00:00": [True, 1.0, 0.5],
                    "2026-06-01T15:00:00+00:00": [15.0, 1.0, 0.5],
                    "2026-06-01T16:00:00+00:00": [0, 1.0, 0.5],
                    "2026-06-01T17:00:00+00:00": [120, 1.0, 0.5],
                    "2026-06-01T18:00:00+00:00": "1.0",
                },
                "held": {
                    good: [2.0, 1.0],
                    "not a date": [2.0, 1.0],
                    "2026-06-01T12:00:00+00:00": [2.0],
                    "2026-06-01T13:00:00+00:00": [None, 1.0],
                    "2026-06-01T14:00:00+00:00": {"import": 1.0},
                },
            }
        )

        assert tracker.sample_count == 1
        assert tracker.held_price(_MONDAY.replace(hour=10)) == (2.0, 1.0)
        assert len(tracker.as_dict()["held"]) == 1

    def test_a_file_of_another_shape_restores_nothing(self) -> None:
        tracker = PriceHistoryTracker.from_dict({"samples": [], "held": "x"})

        assert tracker.sample_count == 0
        assert tracker.as_dict() == {"version": 1, "samples": {}, "held": {}}


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


class TestRecording:
    """Only real prices of slots that have started are stored."""

    def test_only_started_slots_are_recorded(self) -> None:
        tracker = PriceHistoryTracker()

        _cycle(tracker, _MONDAY.replace(hour=9, minute=5), _attributes(_MONDAY))

        # 00:00 to 09:00 inclusive: 37 quarter-hours.  The published
        # afternoon is real, but a forecast sensor's future values are not.
        assert tracker.sample_count == 37
        profile = tracker.profile(_TZ)
        assert profile.lookup(_MON, 9, 0, 15) == pytest.approx(
            (_clock_price(9, 0), _clock_price(9, 0) - _EXPORT_GAP)
        )
        assert (_MON, 9, 15) not in profile.quarters

    def test_an_estimate_is_never_recorded(self) -> None:
        """A stale source covers no slot: Tuesday is estimated, never stored."""
        tracker = PriceHistoryTracker()
        _record_day(tracker, _MONDAY)

        # Tuesday morning and the sensors still publish Monday only.
        planner_input, outage = _cycle(
            tracker, _TUESDAY.replace(hour=9, minute=5), _attributes(_MONDAY)
        )

        assert outage is not None
        assert planner_input.price_points == []
        assert tracker.sample_count == 96
        assert not any(key[0] == _TUE for key in tracker.profile(_TZ).quarters)

    def test_a_slot_with_only_an_import_price_is_not_recorded(self) -> None:
        tracker = PriceHistoryTracker()
        attributes = _attributes(_MONDAY)
        attributes[_EXPORT] = {"prices_today": _day_points(_MONDAY)[:8]}

        _cycle(tracker, _MONDAY.replace(hour=9, minute=5), attributes)

        assert tracker.sample_count == 8

    def test_no_slots_no_outage(self) -> None:
        tracker = PriceHistoryTracker()
        coverage = ForecastCoverage()

        result = update_price_history(tracker, [], coverage, _MONDAY, 15)

        assert result == (coverage, None)
        assert tracker.sample_count == 0

    def test_the_fall_back_day_keeps_both_passes(self) -> None:
        tracker = PriceHistoryTracker()

        _record_day(tracker, _FALL_BACK)

        assert tracker.sample_count == 100
        # Both passes of 02:15 share the key and are averaged.
        expected = _clock_price(2, 15) + _SECOND_PASS / 2
        assert tracker.profile(_TZ).lookup(_SUN, 2, 15, 15) == pytest.approx(
            (expected, expected - _EXPORT_GAP)
        )
        assert tracker.profile(_TZ).lookup(_SUN, 3, 15, 15) == pytest.approx(
            (_clock_price(3, 15), _clock_price(3, 15) - _EXPORT_GAP)
        )

    def test_the_spring_forward_day_has_no_skipped_hour(self) -> None:
        tracker = PriceHistoryTracker()

        _record_day(tracker, _SPRING_FORWARD)

        assert tracker.sample_count == 92
        profile = tracker.profile(_TZ)
        assert profile.lookup(_SUN, 2, 0, 15) is None
        assert profile.lookup(_SUN, 3, 0, 15) == pytest.approx(
            (_clock_price(3, 0), _clock_price(3, 0) - _EXPORT_GAP)
        )
        assert len(profile.quarters) == 92


# ---------------------------------------------------------------------------
# Planner: published price, then the profile, then the earlier day
# ---------------------------------------------------------------------------


def _point(day: int, hour: int, minute: int, price: float) -> PricePoint:
    return PricePoint(
        hour=hour,
        import_price=price,
        export_price=price - _EXPORT_GAP,
        day_offset=day,
        slot_in_day=hour * 4 + minute // 15,
    )


_TODAY_POINTS = [
    _point(0, hour, minute, 5.0) for hour in range(24) for minute in (0, 15, 30, 45)
]


class TestPopulatePricesOrder:
    """``populate_prices`` on the production (``slot_in_day``) path."""

    def test_a_published_price_beats_the_profile(self) -> None:
        profile = PriceProfile.from_samples(
            [(_MONDAY.replace(hour=10), 60, 9.0, 8.0)], _TZ
        )

        slots, missing = _populated(_TODAY_POINTS, profile)

        ten = next(s for s in slots if s.start == _MONDAY.replace(hour=10))
        assert ten.price.import_price == pytest.approx(5.0)
        assert not any(key.day_offset == 0 for key in missing)

    def test_the_profile_beats_the_earlier_day(self) -> None:
        profile = PriceProfile.from_samples(
            [(_LAST_TUESDAY.replace(hour=10), 60, 9.0, 8.0)], _TZ
        )

        slots, missing = _populated(_TODAY_POINTS, profile)

        by_start = {slot.start: slot for slot in slots}
        ten = by_start[_TUESDAY.replace(hour=10, minute=30)]
        assert ten.price.import_price == pytest.approx(9.0)
        assert ten.price.export_price == pytest.approx(8.0)
        # An hour the profile lacks still reads today's price for that hour.
        eleven = by_start[_TUESDAY.replace(hour=11)]
        assert eleven.price.import_price == pytest.approx(5.0)
        assert eleven.price.export_price == pytest.approx(5.0 - _EXPORT_GAP)

    def test_a_profile_slot_is_still_reported_missing(self) -> None:
        profile = PriceProfile.from_samples(
            [(_LAST_TUESDAY.replace(hour=10), 60, 9.0, 8.0)], _TZ
        )

        _slots, missing = _populated(_TODAY_POINTS, profile)
        _slots, missing_without = _populated(_TODAY_POINTS, None)

        assert missing == missing_without
        assert len(missing) == 96
        assert {key.day_offset for key in missing} == {1}

    def test_an_empty_profile_changes_nothing(self) -> None:
        with_empty, _ = _populated(_TODAY_POINTS, PriceProfile())
        without, _ = _populated(_TODAY_POINTS, None)

        assert [slot.price for slot in with_empty] == [slot.price for slot in without]

    def test_no_price_at_all_falls_back_to_zero(self) -> None:
        profile = PriceProfile.from_samples(
            [(_MONDAY.replace(hour=10), 60, 9.0, 8.0)], _TZ
        )

        slots, _missing = _populated(_TODAY_POINTS[:4], profile, hours=24)

        by_start = {slot.start: slot for slot in slots}
        assert by_start[_MONDAY.replace(hour=10)].price.import_price == pytest.approx(
            9.0
        )
        assert by_start[_MONDAY.replace(hour=11)].price.import_price == pytest.approx(
            0.0
        )


class TestPopulatePricesHourlyPath:
    """Hand-built hourly points and an input with no price point at all."""

    def test_no_price_points_are_filled_from_the_profile(self) -> None:
        """A price outage with nothing held: the builder emits no point."""
        profile = PriceProfile.from_samples(
            [
                (_LAST_MONDAY.replace(hour=10), 60, 9.0, 8.0),
                (_LAST_TUESDAY.replace(hour=10), 60, 7.0, 6.0),
            ],
            _TZ,
        )

        slots, missing = _populated([], profile)

        by_start = {slot.start: slot for slot in slots}
        monday = by_start[_MONDAY.replace(hour=10, minute=15)]
        assert monday.price.import_price == pytest.approx(9.0)
        assert monday.price.export_price == pytest.approx(8.0)
        assert by_start[_TUESDAY.replace(hour=10)].price.import_price == pytest.approx(
            7.0
        )
        assert by_start[_MONDAY.replace(hour=11)].price.import_price == pytest.approx(
            0.0
        )
        assert len(missing) == len(slots)

    def test_no_price_points_and_no_profile_plan_at_zero(self) -> None:
        slots, missing = _populated([], None)

        assert all(abs(slot.price.import_price) < 1e-9 for slot in slots)
        assert all(abs(slot.price.export_price) < 1e-9 for slot in slots)
        assert len(missing) == len(slots)

    def test_hourly_points_read_the_profile_before_the_earlier_day(self) -> None:
        points = [
            PricePoint(hour=hour, import_price=5.0, export_price=4.5, day_offset=0)
            for hour in range(24)
        ] + [PricePoint(hour=0, import_price=6.0, export_price=5.5, day_offset=1)]
        profile = PriceProfile.from_samples(
            [(_LAST_TUESDAY.replace(hour=10), 60, 9.0, 8.0)], _TZ
        )

        slots, missing = _populated(points, profile, interval=60)

        by_start = {slot.start: slot for slot in slots}
        assert by_start[_TUESDAY].price.import_price == pytest.approx(6.0)
        ten = by_start[_TUESDAY.replace(hour=10)]
        assert ten.price.import_price == pytest.approx(9.0)
        assert ten.price.export_price == pytest.approx(8.0)
        eleven = by_start[_TUESDAY.replace(hour=11)]
        assert eleven.price.import_price == pytest.approx(5.0)
        assert eleven.price.export_price == pytest.approx(4.5)
        assert len(missing) == 23


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

_needs_scipy = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)


@_needs_scipy
class TestUnpublishedTomorrow:
    """Monday 09:05, Tuesday's prices not yet published."""

    def test_tomorrow_reads_last_weeks_same_weekday(self) -> None:
        tracker = PriceHistoryTracker()
        _record_day(tracker, _LAST_TUESDAY, offset=3.0)

        planner_input, outage = _cycle(
            tracker, _MONDAY.replace(hour=9, minute=5), _attributes(_MONDAY)
        )
        output = run_planner(planner_input)

        assert outage is None
        tomorrow = _slots_of(output, _TUESDAY)
        assert len(tomorrow) == 96
        for slot in tomorrow:
            expected = _clock_price(slot.start.hour, slot.start.minute) + 3.0
            assert slot.price.import_price == pytest.approx(expected)
            assert slot.price.export_price == pytest.approx(expected - _EXPORT_GAP)
        for slot in _slots_of(output, _MONDAY):
            assert slot.price.import_price == pytest.approx(
                _clock_price(slot.start.hour, slot.start.minute)
            )

    def test_the_gap_is_still_reported(self) -> None:
        tracker = PriceHistoryTracker()
        _record_day(tracker, _LAST_TUESDAY, offset=3.0)

        planner_input, _outage = _cycle(
            tracker, _MONDAY.replace(hour=9, minute=5), _attributes(_MONDAY)
        )
        quality = run_planner(planner_input).data_quality

        assert quality.today_price_missing_hours == []
        assert quality.tomorrow_price_missing_hours == list(range(24))
        assert quality.is_complete is False

    def test_an_empty_store_plans_exactly_as_before(self) -> None:
        now = _MONDAY.replace(hour=9, minute=5)
        with_store, _ = _cycle(PriceHistoryTracker(), now, _attributes(_MONDAY))
        cfg = _cfg()
        with patch("homeassistant.util.dt.now", return_value=now):
            recs = generate_recommendation_intervals(15, 48)
            coverage = populate_price_and_solcast_from_snapshot(
                recs,
                StateSnapshot(live=LiveState(), sensor_attributes=_attributes(_MONDAY)),
                cfg,
            )
            without_store = build_planner_input(
                cfg=cfg,
                live=LiveState(),
                hourly_recommendations=recs,
                previous_winner_name=None,
                previous_winner_score=0.0,
                forecast_coverage=coverage,
            )

        assert with_store.price_points == without_store.price_points
        # The store holds this morning only: nothing for Tuesday.
        assert with_store.price_profile is not None
        assert {key[0] for key in with_store.price_profile.quarters} == {_MON}
        assert without_store.price_profile is None
        before = run_planner(without_store)
        after = run_planner(with_store)
        assert [slot.price for slot in after.slots] == [
            slot.price for slot in before.slots
        ]
        assert [slot.recommendation for slot in after.slots] == [
            slot.recommendation for slot in before.slots
        ]
        assert after.plan_cost == before.plan_cost

    def test_hourly_history_prices_quarter_hourly_slots(self) -> None:
        """The price source and slots were hourly last week, 15-minute now."""
        tracker = PriceHistoryTracker()
        _record_day(tracker, _LAST_TUESDAY, interval=60, source_step=60, offset=3.0)
        assert tracker.sample_count == 24

        planner_input, _ = _cycle(
            tracker, _MONDAY.replace(hour=9, minute=5), _attributes(_MONDAY)
        )
        output = run_planner(planner_input)

        for slot in _slots_of(output, _TUESDAY):
            assert slot.price.import_price == pytest.approx(
                _clock_price(slot.start.hour, 0) + 3.0
            )

    def test_quarter_hourly_history_prices_hourly_slots(self) -> None:
        """The reverse switch: each hour reads the mean of its quarters."""
        tracker = PriceHistoryTracker()
        _record_day(tracker, _LAST_TUESDAY, offset=3.0)

        planner_input, _ = _cycle(
            tracker,
            _MONDAY.replace(hour=9, minute=5),
            _attributes(_MONDAY, step=60),
            interval=60,
            source_step=60,
        )
        output = run_planner(planner_input)

        tomorrow = _slots_of(output, _TUESDAY)
        assert len(tomorrow) == 24
        for slot in tomorrow:
            quarters = [_clock_price(slot.start.hour, m) for m in (0, 15, 30, 45)]
            assert slot.price.import_price == pytest.approx(sum(quarters) / 4 + 3.0)

    def test_the_week_after_the_fall_back_day(self) -> None:
        """Sunday 1 November is estimated from the 25-hour Sunday before."""
        tracker = PriceHistoryTracker()
        _record_day(tracker, _FALL_BACK)
        saturday = datetime(2026, 10, 31, tzinfo=_TZ)

        planner_input, _ = _cycle(
            tracker, saturday.replace(hour=9, minute=5), _attributes(saturday)
        )
        output = run_planner(planner_input)

        sunday = _slots_of(output, datetime(2026, 11, 1, tzinfo=_TZ))
        assert len(sunday) == 96
        for slot in sunday:
            expected = _clock_price(slot.start.hour, slot.start.minute)
            if slot.start.hour == 2:
                expected += _SECOND_PASS / 2
            assert slot.price.import_price == pytest.approx(expected)

    def test_the_week_after_the_spring_forward_day(self) -> None:
        """02:00 was skipped last Sunday, so it reads Saturday's price."""
        tracker = PriceHistoryTracker()
        _record_day(tracker, _SPRING_FORWARD, offset=3.0)
        saturday = datetime(2026, 4, 4, tzinfo=_TZ)

        planner_input, _ = _cycle(
            tracker, saturday.replace(hour=9, minute=5), _attributes(saturday)
        )
        output = run_planner(planner_input)

        for slot in _slots_of(output, datetime(2026, 4, 5, tzinfo=_TZ)):
            expected = _clock_price(slot.start.hour, slot.start.minute)
            if slot.start.hour != 2:
                expected += 3.0
            assert slot.price.import_price == pytest.approx(expected)


@_needs_scipy
class TestPriceOutage:
    """The price sensors stop delivering at 09:10 on Monday."""

    @staticmethod
    def _outage(tracker: PriceHistoryTracker) -> tuple[PlannerInput, str | None]:
        _cycle(tracker, _MONDAY.replace(hour=9, minute=5), _attributes(_MONDAY))
        return _cycle(tracker, _MONDAY.replace(hour=9, minute=10), {})

    def test_published_prices_are_held_and_the_rest_comes_from_the_store(
        self,
    ) -> None:
        tracker = PriceHistoryTracker()
        _record_day(tracker, _LAST_TUESDAY, offset=3.0)

        planner_input, outage = self._outage(tracker)
        output = run_planner(planner_input)

        assert outage is not None
        for slot in _slots_of(output, _MONDAY):
            expected = _clock_price(slot.start.hour, slot.start.minute)
            assert slot.price.import_price == pytest.approx(expected)
            assert slot.price.export_price == pytest.approx(expected - _EXPORT_GAP)
        for slot in _slots_of(output, _TUESDAY):
            expected = _clock_price(slot.start.hour, slot.start.minute) + 3.0
            assert slot.price.import_price == pytest.approx(expected)
        quality = output.data_quality
        assert quality.today_price_missing_hours == []
        assert quality.tomorrow_price_missing_hours == list(range(24))

    def test_the_reason_names_the_cause(self) -> None:
        tracker = PriceHistoryTracker()

        _planner_input, outage = self._outage(tracker)

        assert outage == (
            "Price data unavailable (import and export price): planning 96 of "
            "192 slots from held published prices and the rest from the price "
            "history (37 recorded slots)"
        )

    def test_one_missing_side_is_named(self) -> None:
        tracker = PriceHistoryTracker()
        _cycle(tracker, _MONDAY.replace(hour=9, minute=5), _attributes(_MONDAY))
        attributes = _attributes(_MONDAY)
        del attributes[_EXPORT]

        _planner_input, outage = _cycle(
            tracker, _MONDAY.replace(hour=9, minute=10), attributes
        )

        assert outage is not None
        assert outage.startswith("Price data unavailable (export price): planning 96")

    def test_two_sources_that_never_overlap_are_an_outage(self) -> None:
        tracker = PriceHistoryTracker()
        attributes = _attributes(_MONDAY)
        attributes[_EXPORT] = {"prices_today": _day_points(_TUESDAY)}

        _planner_input, outage = _cycle(
            tracker, _MONDAY.replace(hour=9, minute=5), attributes
        )

        assert outage is not None
        assert outage.startswith("Price data unavailable (import and export price)")

    def test_held_prices_are_not_recorded_as_history(self) -> None:
        tracker = PriceHistoryTracker()
        _cycle(tracker, _MONDAY.replace(hour=9, minute=5), _attributes(_MONDAY))
        recorded = tracker.sample_count

        _cycle(tracker, _MONDAY.replace(hour=15), {})

        assert tracker.sample_count == recorded

    def test_the_outage_continues_into_the_next_day(self) -> None:
        """Monday's held prices are over; Tuesday comes from the store."""
        tracker = PriceHistoryTracker()
        _record_day(tracker, _LAST_TUESDAY, offset=3.0)
        _cycle(tracker, _MONDAY.replace(hour=9, minute=5), _attributes(_MONDAY))

        planner_input, outage = _cycle(tracker, _TUESDAY.replace(hour=9, minute=5), {})
        output = run_planner(planner_input)

        assert outage is not None
        assert "planning 0 of 192 slots" in outage
        assert planner_input.price_points == []
        for slot in _slots_of(output, _TUESDAY):
            expected = _clock_price(slot.start.hour, slot.start.minute) + 3.0
            assert slot.price.import_price == pytest.approx(expected)
        assert output.data_quality.today_price_missing_hours == list(range(24))

    def test_an_empty_store_plans_exactly_as_before(self) -> None:
        """Nothing held, nothing stored: every price is 0.0, as today."""
        now = _MONDAY.replace(hour=9, minute=10)
        tracker = PriceHistoryTracker()
        cfg = _cfg()
        with patch("homeassistant.util.dt.now", return_value=now):
            recs = generate_recommendation_intervals(15, 48)
            coverage = populate_price_and_solcast_from_snapshot(
                recs, StateSnapshot(live=LiveState(), sensor_attributes={}), cfg
            )
            after_coverage, outage = update_price_history(
                tracker, recs, coverage, now, 15
            )
            planner_input = build_planner_input(
                cfg=cfg,
                live=LiveState(),
                hourly_recommendations=recs,
                previous_winner_name=None,
                previous_winner_score=0.0,
                forecast_coverage=after_coverage,
                price_profile=tracker.profile(now.tzinfo),
            )
        output = run_planner(planner_input)

        assert after_coverage is coverage
        assert outage is not None
        assert planner_input.price_points == []
        assert all(abs(slot.price.import_price) < 1e-9 for slot in output.slots)
        assert all(abs(slot.price.export_price) < 1e-9 for slot in output.slots)
        assert output.data_quality.today_price_missing_hours == list(range(24))


# ---------------------------------------------------------------------------
# Degraded mode
# ---------------------------------------------------------------------------


class TestDegradedReason:
    """An outage degrades the cycle without blocking the planner or writes."""

    def test_a_reason_alone_is_degraded(self) -> None:
        assert classify_degraded_mode(False, [], ["price outage"]) is (
            DegradedMode.Degraded
        )
        assert classify_degraded_mode(False, [], []) is DegradedMode.OK
        assert classify_degraded_mode(False, []) is DegradedMode.OK

    def test_a_critical_entity_still_wins(self) -> None:
        mode = classify_degraded_mode(
            True, ["Missing entity: batteries_state_of_capacity"], ["price outage"]
        )

        assert mode is DegradedMode.Error

    def test_live_state_reclassifies_and_keeps_the_planner_running(self) -> None:
        live = LiveState()
        assert live.degraded_mode is DegradedMode.OK

        live.add_degraded_reason("price outage")

        assert live.degraded_mode is DegradedMode.Degraded
        assert live.missing_entities is False
        assert live.degraded_reasons == ["price outage"]
        assert hardware_writes_allowed(live.degraded_mode) is True


class TestCoordinatorCycle:
    """The real coordinator cycle records, holds, reports and persists."""

    @staticmethod
    async def _collect(coordinator: Any, coverage_for: Any) -> None:
        """Run the collect phase with a populator that covers *coverage_for*."""

        def _populate(recs: list[Any], *_args: Any) -> ForecastCoverage:
            starts = frozenset(
                rec.start.astimezone(UTC) for rec in recs if coverage_for(rec)
            )
            for rec in recs:
                if coverage_for(rec):
                    rec.import_price = 2.0
                    rec.export_price = 1.0
            return ForecastCoverage(import_price=starts, export_price=starts)

        collected: tuple[Any, None, list[Any]] = (_snapshot(), None, [])
        with (
            patch(f"{_CYCLE}.hsem_now", return_value=_FIXTURE_NOW),
            patch(
                "custom_components.hsem.coordinator_builder.hsem_now",
                return_value=_FIXTURE_NOW,
            ),
            patch(
                f"{_CYCLE}.async_collect_all_states",
                AsyncMock(return_value=collected),
            ),
            patch(
                f"{_LOAD}.populate_avg_house_consumption_from_snapshot",
                side_effect=_populate_consumption,
            ),
            patch(
                f"{_CYCLE}.populate_price_and_solcast_from_snapshot",
                side_effect=_populate,
            ),
        ):
            await coordinator._async_collect_and_populate(_FIXTURE_NOW)

    @pytest.mark.asyncio
    async def test_an_outage_reaches_the_degraded_mode_sensor(
        self, tmp_path: Path
    ) -> None:
        coordinator, _build, _solved = _coordinator(
            tmp_path, reference=_plan(grid_charge=False), floor_enabled=False
        )
        history_file = tmp_path / "hsem_price_history.json"
        coordinator._price_history_tracker.history_file = str(history_file)

        await self._collect(coordinator, lambda rec: True)

        live = coordinator._live
        assert live is not None
        assert live.degraded_mode is DegradedMode.OK
        assert coordinator._price_history_tracker.sample_count > 0
        assert history_file.exists()

        await self._collect(coordinator, lambda rec: False)

        live = coordinator._live
        assert live is not None
        assert live.degraded_mode is DegradedMode.Degraded
        assert live.missing_entities is False
        recs = coordinator._hourly_recommendations
        assert all(rec.import_price == pytest.approx(2.0) for rec in recs)
        coverage = coordinator._forecast_coverage
        assert coverage is not None
        assert all(coverage.has_price(rec.start) for rec in recs)

        sensor = HSEMDegradedModeSensor(MagicMock(), coordinator)
        coordinator.data = CoordinatorData(cfg=coordinator._cfg, live=live)
        assert sensor.state == "degraded"
        attributes = sensor.extra_state_attributes
        assert attributes["hardware_writes_blocked"] is False
        assert attributes["missing_entities"] == []
        assert len(attributes["degraded_reasons"]) == 1
        assert attributes["degraded_reasons"][0].startswith(
            "Price data unavailable (import and export price): planning 192 of 192"
        )

    @pytest.mark.asyncio
    async def test_the_sensor_lists_no_reason_before_the_first_cycle(
        self, tmp_path: Path
    ) -> None:
        coordinator, _build, _solved = _coordinator(
            tmp_path, reference=_plan(grid_charge=False), floor_enabled=False
        )
        coordinator.data = None  # type: ignore[assignment]  # before the first cycle

        sensor = HSEMDegradedModeSensor(MagicMock(), coordinator)

        assert sensor.extra_state_attributes["degraded_reasons"] == []

    @pytest.mark.asyncio
    async def test_a_coordinator_without_a_tracker_keeps_the_coverage(self) -> None:
        coverage = ForecastCoverage()
        live = LiveState()

        result = await async_apply_price_history(
            MagicMock(spec=[]), coverage, _MONDAY, live, 15
        )

        assert result is coverage
        assert live.degraded_reasons == []


class TestDiagnosticsRoundTrip:
    """The profile survives the diagnostics dump and the backtest replay."""

    def test_the_profile_round_trips_through_json(self) -> None:
        profile = PriceProfile.from_samples(
            [
                (_MONDAY.replace(hour=10), 60, 1.0, 0.5),
                (_TUESDAY.replace(hour=11, minute=15), 15, 2.0, 1.5),
            ],
            _TZ,
        )

        rows = json.loads(json.dumps(profile.as_dict()))

        assert rows["hours"] == [[_MON, 10, 1.0, 0.5], [_TUE, 11, 2.0, 1.5]]
        assert PriceProfile.from_dict(rows) == profile
        assert PriceProfile.from_dict({}) == PriceProfile()

    def test_the_dump_and_the_replay_keep_the_profile(self) -> None:
        profile = PriceProfile.from_samples(
            [(_MONDAY.replace(hour=10), 60, 1.0, 0.5)], _TZ
        )
        with_profile = PlannerInput(now_iso=_MONDAY.isoformat(), price_profile=profile)
        without = PlannerInput(now_iso=_MONDAY.isoformat())

        dumped = json.loads(json.dumps(_planner_input_to_dict(with_profile)))
        replayed, report = planner_input_from_dict({"planner_input": dumped})

        assert replayed.price_profile == profile
        assert report.is_faithful
        assert _planner_input_to_dict(without)["price_profile"] is None

    @pytest.mark.asyncio
    async def test_a_cycle_without_coverage_skips_the_step(self) -> None:
        coordinator = MagicMock()
        coordinator._price_history_tracker = PriceHistoryTracker()
        live = LiveState()

        result = await async_apply_price_history(coordinator, None, _MONDAY, live, 15)

        assert result is None
        assert live.degraded_reasons == []

"""Issue #1434 — a persisted price history fills unpublished and missing prices.

HSEM only knew the prices its price sensors exposed right now.  A slot without
a price was estimated from the nearest earlier day in the same payload, and a
price sensor outage left nothing to estimate from: the whole horizon was
planned at ``0.0`` while degraded mode stayed ``ok``.

The price history records the real price of every slot that has started and
keeps the last published prices.  The planner reads its weekday and
time-of-day profile, shifted to the level of the published prices, before the
nearest-earlier-day estimate.  An outage is planned from the held and stored
prices, reported through degraded mode and stopped after a time limit.

The end-to-end tests run through the real populator,
``update_price_history``, ``build_planner_input`` and ``run_planner``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
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
from custom_components.hsem.coordinator_performance import build_performance_sample
from custom_components.hsem.coordinator_price_history import (
    PRICE_OUTAGE_EXPIRED_LABEL,
    PriceHistoryResult,
    async_apply_price_history,
    init_price_history_tracker,
    price_source,
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
    PRICE_OUTAGE_MAX_HOURS,
    PriceHistoryTracker,
)
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.price_profile import (
    PROFILE_MIN_DAYS,
    PriceProfile,
)
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
from custom_components.hsem.utils.recommendations import Recommendations
from tests.backtest.conftest import corpus_paths
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
_IMPORT_FORECAST = "sensor.import_price_forecast"
_CARNOT_OFFSET = 5.0
#: Local midnights.  1 June 2026 is a Monday.
_LAST_MONDAY = datetime(2026, 5, 25, tzinfo=_TZ)
_LAST_TUESDAY = datetime(2026, 5, 26, tzinfo=_TZ)
_MONDAY = datetime(2026, 6, 1, tzinfo=_TZ)
_TUESDAY = datetime(2026, 6, 2, tzinfo=_TZ)
_WEDNESDAY = datetime(2026, 6, 3, tzinfo=_TZ)
#: The 25-hour fall-back day and the 23-hour spring-forward day (Sundays).
_FALL_BACK = datetime(2026, 10, 25, tzinfo=_TZ)
_SPRING_FORWARD = datetime(2026, 3, 29, tzinfo=_TZ)
#: The second pass of the repeated fall-back hour costs this much more.
_SECOND_PASS = 0.5
#: Export is this much below import in every published price.
_EXPORT_GAP = 0.5
#: The recorded weeks were this much dearer than the prices published now.
_OLD_LEVEL = 3.0
#: Tuesdays have an evening peak that Mondays lack: the weekday's shape.
_TUESDAY_PEAK = {18: 0.2}
_MON, _TUE, _WED, _SUN = 0, 1, 2, 6
_NOW = _MONDAY.replace(hour=9, minute=5)

_needs_scipy = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
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


def _clock_price(hour: int, minute: int) -> float:
    """Return a price that identifies its wall-clock quarter: 10:15 is 1.1015."""
    return round(1.0 + hour / 100.0 + minute / 10000.0, 6)


def _day_points(
    day_start: datetime,
    *,
    step: int = 15,
    offset: float = 0.0,
    peak: dict[int, float] | None = None,
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
        price += (peak or {}).get(local.hour, 0.0)
        points.append(
            {
                "start": local.isoformat(),
                "price": round(price + (_SECOND_PASS if local.fold else 0.0), 6),
            }
        )
        moment += timedelta(minutes=step)
    return points


def _attributes(
    *days: datetime,
    step: int = 15,
    offset: float = 0.0,
    peak: dict[int, float] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return import and export sensor attributes publishing *days*."""
    imports: list[dict[str, Any]] = []
    exports: list[dict[str, Any]] = []
    for day in days:
        imports += _day_points(day, step=step, offset=offset, peak=peak)
        exports += _day_points(day, step=step, offset=offset - _EXPORT_GAP, peak=peak)
    return {_IMPORT: {"prices_today": imports}, _EXPORT: {"prices_today": exports}}


def _cfg(interval: int = 15, source_step: int = 15) -> SensorConfig:
    cfg = SensorConfig()
    cfg.recommendation_interval_minutes = interval
    cfg.recommendation_interval_length = 48
    cfg.electricity_price_update_interval = source_step
    cfg.import_electricity_price_sensor = _IMPORT
    cfg.export_electricity_price_sensor = _EXPORT
    cfg.import_electricity_price_forecast_sensor = _IMPORT_FORECAST
    return cfg


def _cycle(
    tracker: PriceHistoryTracker | None,
    now: datetime,
    attributes: dict[str, dict[str, Any]],
    *,
    interval: int = 15,
    source_step: int = 15,
) -> tuple[PlannerInput, PriceHistoryResult]:
    """Run one cycle's price steps; ``tracker=None`` is a setup without one."""
    cfg = _cfg(interval, source_step)
    with patch("homeassistant.util.dt.now", return_value=now):
        recs = generate_recommendation_intervals(interval, 48)
        coverage = populate_price_and_solcast_from_snapshot(
            recs, StateSnapshot(live=LiveState(), sensor_attributes=attributes), cfg
        )
        result = PriceHistoryResult(coverage)
        if tracker is not None:
            result = update_price_history(tracker, recs, coverage, now, interval)
        planner_input = build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=recs,
            previous_winner_name=None,
            previous_winner_score=0.0,
            forecast_coverage=result.coverage,
            price_profile=tracker.profile(now.tzinfo) if tracker else None,
        )
    return planner_input, result


def _record_day(
    tracker: PriceHistoryTracker,
    day: datetime,
    *,
    interval: int = 15,
    source_step: int = 15,
    offset: float = 0.0,
    peak: dict[int, float] | None = None,
) -> None:
    """Record the whole of *day* with a cycle at 23:59 that evening."""
    _cycle(
        tracker,
        day.replace(hour=23, minute=59),
        _attributes(day, step=source_step, offset=offset, peak=peak),
        interval=interval,
        source_step=source_step,
    )


def _record_weeks(tracker: PriceHistoryTracker, last: datetime, **kwargs: Any) -> None:
    """Record the same weekday on the ``PROFILE_MIN_DAYS`` weeks ending at *last*."""
    for weeks in reversed(range(PROFILE_MIN_DAYS)):
        day = (last.replace(tzinfo=None) - timedelta(weeks=weeks)).replace(tzinfo=_TZ)
        _record_day(tracker, day, **kwargs)


def _history(**kwargs: Any) -> PriceHistoryTracker:
    """Six dearer Mondays and six dearer Tuesdays with an evening peak."""
    tracker = PriceHistoryTracker()
    _record_weeks(tracker, _LAST_MONDAY, offset=_OLD_LEVEL, **kwargs)
    _record_weeks(
        tracker, _LAST_TUESDAY, offset=_OLD_LEVEL, peak=_TUESDAY_PEAK, **kwargs
    )
    return tracker


def _slots_of(output: PlannerOutput, day: datetime) -> list[PlannedSlot]:
    end = datetime.combine(day.date() + timedelta(days=1), time(0), _TZ)
    return [slot for slot in output.slots if day <= slot.start < end]


def _populated(
    points: list[PricePoint],
    profile: PriceProfile | None,
    *,
    interval: int = 15,
    hours: int = 48,
) -> tuple[dict[datetime, PlannedSlot], set[Any]]:
    """Run ``populate_prices`` on a Monday grid; return slots and missing keys."""
    inp = PlannerInput(
        now_iso=_NOW.isoformat(),
        time_zone="Europe/Copenhagen",
        interval_minutes=interval,
        interval_length_hours=hours,
    )
    tsi = build_time_series_index(inp, _NOW)
    slots = build_slots(inp, _NOW)
    populate_prices(slots, points, tsi, profile)
    return {slot.start: slot for slot in slots}, set(tsi.missing_price_slots)


def _days(start: datetime, count: int = PROFILE_MIN_DAYS) -> list[datetime]:
    """Return the same wall-clock time on *count* consecutive weeks."""
    return [
        (start.replace(tzinfo=None) - timedelta(weeks=week)).replace(tzinfo=_TZ)
        for week in range(count)
    ]


def _energi_data_service(
    real: list[datetime], carnot: list[datetime]
) -> dict[str, dict[str, Any]]:
    """Return an Energi Data Service import sensor with Carnot enabled.

    ``raw_today`` holds the published prices of *real* and ``forecast`` the
    Carnot predictions of *carnot*, 5.0 above the clock price, hourly.  The
    export sensor publishes both days.
    """

    def entries(days: list[datetime], **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"hour": point["start"], "price": point["price"]}
            for day in days
            for point in _day_points(day, **kwargs)
        ]

    attributes = _attributes(*real, *carnot)
    attributes[_IMPORT] = {
        "raw_today": entries(real),
        "forecast": entries(carnot, step=60, offset=_CARNOT_OFFSET),
    }
    return attributes


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class TestPriceProfile:
    """Median per weekday and quarter, only for buckets with enough days."""

    def test_an_empty_profile_knows_nothing(self) -> None:
        profile = PriceProfile.from_samples([], _TZ)

        assert profile.is_empty
        assert profile.lookup(_MON, 10, 0, 15) is None

    def test_a_bucket_needs_six_days(self) -> None:
        """Fewer days were a worse estimate than the earlier day (issue #1452)."""
        ten = _MONDAY.replace(hour=10)
        five = PriceProfile.from_samples(
            [(day, 15, 9.0, 8.0) for day in _days(ten, 5)], _TZ
        )
        six = PriceProfile.from_samples(
            [(day, 15, 9.0, 8.0) for day in _days(ten, 6)], _TZ
        )

        assert PROFILE_MIN_DAYS == 6
        assert five.is_empty
        assert not six.is_empty
        assert six.lookup(_MON, 10, 0, 15) == pytest.approx((9.0, 8.0))

    def test_a_bucket_is_the_median_of_its_days(self) -> None:
        """One extreme day does not move the bucket."""
        ten = _MONDAY.replace(hour=10)
        prices = [(1.0, 0.5), (1.2, 0.7), (40.0, 0.6), (1.1, 0.5), (1.3, 0.7)]
        profile = PriceProfile.from_samples(
            [(day, 15, *pair) for day, pair in zip(_days(ten, 5), prices, strict=True)],
            _TZ,
            min_days=5,
        )

        assert profile.lookup(_MON, 10, 0, 15) == pytest.approx((1.2, 0.6))

    def test_two_samples_of_one_day_count_as_one_day(self) -> None:
        ten = _MONDAY.replace(hour=10)
        profile = PriceProfile.from_samples(
            [(ten, 15, 1.0, 0.5), (ten, 15, 3.0, 2.5)], _TZ, min_days=1
        )
        too_few = PriceProfile.from_samples(
            [(ten, 15, 1.0, 0.5), (ten, 15, 3.0, 2.5)], _TZ, min_days=2
        )

        assert profile.lookup(_MON, 10, 0, 15) == pytest.approx((2.0, 1.5))
        assert too_few.is_empty

    def test_an_hourly_sample_prices_all_four_quarters(self) -> None:
        profile = PriceProfile.from_samples(
            [(day, 60, 2.0, 1.0) for day in _days(_MONDAY.replace(hour=10))], _TZ
        )

        for minute in (0, 15, 30, 45):
            assert profile.lookup(_MON, 10, minute, 15) == pytest.approx((2.0, 1.0))
        assert profile.lookup(_MON, 11, 0, 15) is None

    def test_a_wider_slot_reads_the_mean_of_its_quarters(self) -> None:
        samples = [
            (day.replace(minute=minute), 15, 1.0 + minute / 15, 0.5)
            for day in _days(_MONDAY.replace(hour=10))
            for minute in (0, 15, 30, 45)
        ]
        profile = PriceProfile.from_samples(samples, _TZ)

        assert profile.lookup(_MON, 10, 0, 60) == pytest.approx((2.5, 0.5))
        assert profile.lookup(_MON, 10, 30, 30) == pytest.approx((3.5, 0.5))

    def test_the_lookup_falls_back_step_by_step(self) -> None:
        profile = PriceProfile(
            quarters={(_MON, 10, 0): (1.0, 0.1)},
            hours={(_MON, 10): (2.0, 0.2), (_MON, 11): (3.0, 0.3)},
            any_day_quarters={(12, 15): (4.0, 0.4)},
            any_day_hours={12: (5.0, 0.5), 13: (6.0, 0.6)},
        )

        assert profile.lookup(_MON, 10, 0, 15) == pytest.approx((1.0, 0.1))
        assert profile.lookup(_MON, 10, 15, 15) == pytest.approx((2.0, 0.2))
        assert profile.lookup(_MON, 11, 0, 15) == pytest.approx((3.0, 0.3))
        assert profile.lookup(_TUE, 12, 15, 15) == pytest.approx((4.0, 0.4))
        assert profile.lookup(_TUE, 12, 30, 15) == pytest.approx((5.0, 0.5))
        assert profile.lookup(_TUE, 13, 0, 60) == pytest.approx((6.0, 0.6))
        assert profile.lookup(_TUE, 14, 0, 15) is None

    def test_a_weekday_without_enough_days_reads_every_weekday(self) -> None:
        ten = _MONDAY.replace(hour=10)
        samples = [
            (ten, 15, 1.0, 0.5),
            (ten + timedelta(days=1), 15, 2.0, 1.5),
            (ten + timedelta(days=2), 15, 9.0, 8.5),
        ]
        profile = PriceProfile.from_samples(samples, _TZ, min_days=3)

        assert profile.quarters == {}
        assert profile.lookup(_SUN, 10, 0, 15) == pytest.approx((2.0, 1.5))

    def test_a_zero_width_sample_counts_as_one_quarter(self) -> None:
        profile = PriceProfile.from_samples(
            [(_MONDAY.replace(hour=10), 0, 2.0, 1.0)], _TZ, min_days=1
        )

        assert list(profile.quarters) == [(_MON, 10, 0)]

    def test_the_weekday_is_read_in_the_local_zone(self) -> None:
        late_sunday_utc = datetime(2026, 5, 31, 22, 30, tzinfo=UTC)
        profile = PriceProfile.from_samples(
            [(late_sunday_utc, 15, 2.0, 1.0)], _TZ, min_days=1
        )

        assert list(profile.quarters) == [(_MON, 0, 30)]

    def test_the_profile_round_trips_through_json(self) -> None:
        profile = PriceProfile.from_samples(
            [
                (_MONDAY.replace(hour=10), 60, 1.0, 0.5),
                (_TUESDAY.replace(hour=11, minute=15), 15, 2.0, 1.5),
            ],
            _TZ,
            min_days=1,
        )

        rows = json.loads(json.dumps(profile.as_dict()))

        assert rows["hours"] == [[_MON, 10, 1.0, 0.5], [_TUE, 11, 2.0, 1.5]]
        assert rows["any_day_hours"] == [[10, 1.0, 0.5], [11, 2.0, 1.5]]
        assert PriceProfile.from_dict(rows) == profile
        assert PriceProfile.from_dict({}) == PriceProfile()


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

_SOURCE = {
    "import_entity": _IMPORT,
    "export_entity": _EXPORT,
    "import_unit": "DKK/kWh",
    "export_unit": "DKK/kWh",
}


def _hass(tmp_path: Path) -> MagicMock:
    hass = MagicMock()
    hass.config.config_dir = str(tmp_path)
    return hass


class TestPriceHistoryTracker:
    """Recording, pruning, holding, the price source and persistence."""

    def test_the_latest_value_of_a_slot_wins(self) -> None:
        tracker = PriceHistoryTracker()
        ten = _MONDAY.replace(hour=10)

        tracker.record(ten, 15, 1.0, 0.5)
        tracker.record(ten.astimezone(UTC), 15, 3.0, 2.5)

        assert tracker.sample_count == 1
        assert tracker._samples[ten.astimezone(UTC)] == (15, 3.0, 2.5)

    def test_old_prices_are_pruned_and_the_profile_rebuilt(self) -> None:
        tracker = PriceHistoryTracker()
        old = _MONDAY - timedelta(days=PRICE_HISTORY_RETENTION_DAYS + 1)
        tracker.record(old, 15, 1.0, 0.5)
        tracker.record(_MONDAY, 15, 2.0, 1.5)
        before = tracker._bucket_profile(_TZ)
        assert tracker._bucket_profile(_TZ) is before
        assert tracker.profile(_TZ) == before

        tracker.prune(_NOW)

        assert tracker.sample_count == 1
        assert tracker._bucket_profile(_TZ) is not before

    def test_held_prices_are_kept_per_side_until_their_day_is_over(self) -> None:
        tracker = PriceHistoryTracker()
        ten = _MONDAY.replace(hour=10).astimezone(UTC)
        tracker.hold("import", {ten: 1.0})
        tracker.hold("export", {ten: 0.5, ten + timedelta(hours=1): 0.6})

        # A later cycle publishes the next hour only: 10:00 is still held.
        tracker.hold("import", {ten + timedelta(hours=1): 1.1})

        assert tracker.held_price("import", _MONDAY.replace(hour=10)) == pytest.approx(
            1.0
        )
        assert tracker.held_price("import", _MONDAY.replace(hour=11)) == pytest.approx(
            1.1
        )
        assert tracker.held_price("export", _MONDAY.replace(hour=11)) == pytest.approx(
            0.6
        )
        assert tracker.held_price("export", _MONDAY.replace(hour=12)) is None

        tracker.prune(_TUESDAY.replace(hour=13))

        assert tracker.held_price("import", _MONDAY.replace(hour=10)) is None
        assert tracker.held_price("import", _MONDAY.replace(hour=11)) == pytest.approx(
            1.1
        )

    def test_the_outage_clock_starts_runs_and_resets(self) -> None:
        tracker = PriceHistoryTracker()

        assert tracker.mark_outage(_NOW, False) == pytest.approx(0.0)
        assert tracker.mark_outage(_NOW, True) == pytest.approx(0.0)
        assert tracker.mark_outage(_NOW + timedelta(hours=5), True) == pytest.approx(
            5.0
        )
        assert tracker.outage_since == _NOW.astimezone(UTC)
        assert tracker.mark_outage(_NOW + timedelta(hours=6), False) == pytest.approx(
            0.0
        )
        assert tracker.outage_since is None

    def test_a_changed_price_entity_drops_the_store(self) -> None:
        """Another sensor is another price scale (review finding 2)."""
        tracker = PriceHistoryTracker()
        assert tracker.set_source(_SOURCE) is False
        tracker.record(_MONDAY, 15, 1.0, 0.5)
        tracker.hold("import", {_MONDAY.astimezone(UTC): 1.0})
        tracker.mark_outage(_NOW, True)
        assert tracker.set_source(_SOURCE) is False

        dropped = tracker.set_source({**_SOURCE, "import_entity": "sensor.other"})

        assert dropped is True
        assert tracker.sample_count == 0
        assert tracker.held_price("import", _MONDAY) is None
        assert tracker.outage_since is None
        assert tracker.source["import_entity"] == "sensor.other"

    def test_a_changed_unit_drops_the_store(self) -> None:
        tracker = PriceHistoryTracker()
        tracker.set_source(_SOURCE)
        tracker.record(_MONDAY, 15, 1.0, 0.5)

        assert tracker.set_source({**_SOURCE, "export_unit": "EUR/kWh"}) is True
        assert tracker.sample_count == 0

    def test_an_unknown_unit_keeps_the_store(self) -> None:
        """An unavailable sensor reports no unit: an outage is not a change."""
        tracker = PriceHistoryTracker()
        tracker.set_source({**_SOURCE, "import_unit": ""})
        tracker.record(_MONDAY, 15, 1.0, 0.5)

        assert tracker.set_source(_SOURCE) is False
        assert tracker.source["import_unit"] == "DKK/kWh"
        assert tracker.set_source(
            {**_SOURCE, "import_unit": "", "export_unit": ""}
        ) is (False)
        assert tracker.source == _SOURCE
        assert tracker.sample_count == 1

    def test_a_first_source_on_an_empty_store_drops_nothing(self) -> None:
        tracker = PriceHistoryTracker()

        assert tracker.set_source({"import_entity": _IMPORT}) is False
        assert tracker.source["export_entity"] == ""

    @pytest.mark.asyncio
    async def test_state_survives_a_restart(self, tmp_path: Path) -> None:
        tracker = PriceHistoryTracker()
        await init_price_history_tracker(tracker, _hass(tmp_path))
        tracker.set_source(_SOURCE)
        tracker.record(_MONDAY.replace(hour=10), 15, 1.0, 0.5)
        tracker.hold("import", {_MONDAY.replace(hour=11).astimezone(UTC): 2.0})
        tracker.hold("export", {_MONDAY.replace(hour=11).astimezone(UTC): 1.5})
        tracker.mark_outage(_NOW, True)
        assert await tracker.save_history() is True

        restored = PriceHistoryTracker()
        await init_price_history_tracker(restored, _hass(tmp_path))

        assert restored.sample_count == 1
        assert restored.source == _SOURCE
        assert restored.outage_since == _NOW.astimezone(UTC)
        assert restored.held_price("import", _MONDAY.replace(hour=11)) == pytest.approx(
            2.0
        )
        assert restored.held_price("export", _MONDAY.replace(hour=11)) == pytest.approx(
            1.5
        )
        assert restored._samples == tracker._samples

    @pytest.mark.asyncio
    async def test_nothing_is_written_without_a_file_or_a_change(
        self, tmp_path: Path
    ) -> None:
        assert await PriceHistoryTracker().save_history() is False

        tracker = PriceHistoryTracker()
        await init_price_history_tracker(tracker, _hass(tmp_path))
        tracker.record(_MONDAY, 15, 1.0, 0.5)
        assert await tracker.save_history() is True
        path = Path(tracker.history_file)
        path.unlink()

        tracker.record(_MONDAY, 15, 1.0, 0.5)
        tracker.hold("import", {})
        tracker.set_source({})
        tracker.prune(_NOW)
        tracker.mark_outage(_NOW, False)
        assert await tracker.save_history() is True
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_a_revised_held_price_does_not_rewrite_the_file(
        self, tmp_path: Path
    ) -> None:
        tracker = PriceHistoryTracker()
        await init_price_history_tracker(tracker, _hass(tmp_path))
        key = _MONDAY.astimezone(UTC)
        tracker.hold("import", {key: 1.0})
        assert await tracker.save_history() is True
        path = Path(tracker.history_file)
        written = path.read_text()

        tracker.hold("import", {key: 1.3})
        assert await tracker.save_history() is True

        assert path.read_text() == written
        assert tracker.held_price("import", _MONDAY) == pytest.approx(1.3)

    @pytest.mark.asyncio
    async def test_a_failed_write_is_retried(self, tmp_path: Path) -> None:
        tracker = PriceHistoryTracker()
        await init_price_history_tracker(tracker, _hass(tmp_path))
        tracker.record(_MONDAY, 15, 1.0, 0.5)
        target = "custom_components.hsem.models.price_history_tracker"
        with patch(f"{target}.write_json_history_file", return_value=False):
            assert await tracker.save_history() is False

        assert await tracker.save_history() is True
        assert Path(tracker.history_file).exists()

    def test_invalid_persisted_entries_are_skipped(self) -> None:
        start = _MONDAY.astimezone(UTC).isoformat()
        other = _TUESDAY.astimezone(UTC).isoformat()
        tracker = PriceHistoryTracker.from_dict(
            {
                "version": 2,
                "source": "not a mapping",
                "outage_since": "not a date",
                "samples": {
                    start: [15, 1.0, 0.5],
                    "not a date": [15, 1.0, 0.5],
                    other: [15, 1.0],
                    "2026-06-03T00:00:00+00:00": [15, "x", 0.5],
                    "2026-06-04T00:00:00+00:00": [True, 1.0, 0.5],
                    "2026-06-05T00:00:00+00:00": [240, 1.0, 0.5],
                },
                "held_import": {start: 1.0, "not a date": 1.0, other: "x"},
                "held_export": [],
            }
        )

        assert tracker.sample_count == 1
        assert tracker.source == {}
        assert tracker.outage_since is None
        assert tracker.held_price("import", _MONDAY) == pytest.approx(1.0)
        assert tracker.held_price("import", _TUESDAY) is None
        assert tracker.held_price("export", _MONDAY) is None

    def test_a_file_of_another_version_restores_nothing(self) -> None:
        """Version 1 had no price source: its prices cannot be trusted."""
        start = _MONDAY.astimezone(UTC).isoformat()
        old = {"version": 1, "samples": {start: [15, 1.0, 0.5]}}

        assert PriceHistoryTracker.from_dict(old).sample_count == 0
        assert PriceHistoryTracker.from_dict({"samples": []}).sample_count == 0
        assert PriceHistoryTracker.from_dict({"version": 2}).sample_count == 0


# ---------------------------------------------------------------------------
# Recording through the real populator
# ---------------------------------------------------------------------------


class TestRecording:
    """Only real prices of slots that have started are stored."""

    def test_only_started_slots_are_recorded(self) -> None:
        tracker = PriceHistoryTracker()

        _cycle(tracker, _NOW, _attributes(_MONDAY))

        # 00:00 to 09:00 inclusive: 37 quarter-hours.
        assert tracker.sample_count == 37
        nine = tracker._samples[_MONDAY.replace(hour=9).astimezone(UTC)]
        assert nine == pytest.approx(
            (15, _clock_price(9, 0), _clock_price(9, 0) - _EXPORT_GAP)
        )
        assert _MONDAY.replace(hour=9, minute=15).astimezone(UTC) not in (
            tracker._samples
        )

    def test_a_forecast_sensor_is_never_recorded(self) -> None:
        """Its predictions overlay the published prices (review finding 6)."""
        tracker = PriceHistoryTracker()
        attributes = _attributes(_MONDAY)
        attributes[_IMPORT_FORECAST] = {
            "prices_today": _day_points(_MONDAY, offset=5.0)
        }

        planner_input, _ = _cycle(tracker, _NOW, attributes)

        # The planner still gets the forecast sensor's value, as before.
        assert planner_input.price_points[0].import_price == pytest.approx(
            _clock_price(0, 0) + 5.0
        )
        assert tracker.sample_count == 37
        for start, (_step, import_price, _export) in tracker._samples.items():
            local = start.astimezone(_TZ)
            assert import_price == pytest.approx(_clock_price(local.hour, local.minute))
        assert tracker.held_price("import", _MONDAY.replace(hour=20)) == pytest.approx(
            _clock_price(20, 0)
        )

    def test_a_forecast_sensor_alone_records_nothing(self) -> None:
        tracker = PriceHistoryTracker()
        attributes = _attributes(_MONDAY)
        attributes[_IMPORT_FORECAST] = attributes.pop(_IMPORT)

        planner_input, result = _cycle(tracker, _NOW, attributes)

        assert tracker.sample_count == 0
        assert tracker.held_price("import", _MONDAY) is None
        assert result.outage_reason is not None
        assert "(import price)" in result.outage_reason
        # In the outage its predictions are estimates: the import side is
        # left to the planner, which reports the slots as missing.
        assert len(planner_input.price_points) == 96
        assert all(point.import_price is None for point in planner_input.price_points)
        assert not result.coverage.import_price

    def test_a_prediction_after_a_real_price_keeps_the_real_price(self) -> None:
        """``predictions`` is read after ``prices`` and overwrites the slot."""
        tracker = PriceHistoryTracker()
        attributes = _attributes(_MONDAY)
        attributes[_IMPORT] = {
            "prices": _day_points(_MONDAY),
            "predictions": _day_points(_MONDAY, offset=5.0),
        }

        planner_input, result = _cycle(tracker, _NOW, attributes)

        assert result.outage_reason is None
        assert planner_input.price_points[0].import_price == pytest.approx(
            _clock_price(0, 0) + 5.0
        )
        assert tracker.sample_count == 37
        for start, (_step, import_price, _export) in tracker._samples.items():
            local = start.astimezone(_TZ)
            assert import_price == pytest.approx(_clock_price(local.hour, local.minute))
        assert tracker.held_price("import", _MONDAY.replace(hour=20)) == pytest.approx(
            _clock_price(20, 0)
        )

    def test_a_stale_source_is_an_outage_and_records_nothing(self) -> None:
        tracker = PriceHistoryTracker()
        _record_day(tracker, _MONDAY)

        planner_input, result = _cycle(
            tracker, _TUESDAY.replace(hour=9, minute=5), _attributes(_MONDAY)
        )

        assert result.outage_reason is not None
        assert planner_input.price_points == []
        assert tracker.sample_count == 96

    def test_a_slot_with_only_an_import_price_is_not_recorded(self) -> None:
        tracker = PriceHistoryTracker()
        attributes = _attributes(_MONDAY)
        attributes[_EXPORT] = {"prices_today": _day_points(_MONDAY)[:8]}

        _cycle(tracker, _NOW, attributes)

        assert tracker.sample_count == 8

    def test_no_slots_no_outage(self) -> None:
        tracker = PriceHistoryTracker()
        coverage = ForecastCoverage()

        result = update_price_history(tracker, [], coverage, _MONDAY, 15)

        assert result == PriceHistoryResult(coverage)
        assert tracker.outage_since is None

    def test_the_fall_back_day_keeps_both_passes(self) -> None:
        tracker = PriceHistoryTracker()

        _record_day(tracker, _FALL_BACK)

        assert tracker.sample_count == 100
        profile = PriceProfile.from_samples(
            ((start, *sample) for start, sample in tracker._samples.items()),
            _TZ,
            min_days=1,
        )
        # Both passes of 02:15 share the key and are averaged.
        expected = _clock_price(2, 15) + _SECOND_PASS / 2
        assert profile.lookup(_SUN, 2, 15, 15) == pytest.approx(
            (expected, expected - _EXPORT_GAP)
        )

    def test_the_spring_forward_day_has_no_skipped_hour(self) -> None:
        tracker = PriceHistoryTracker()

        _record_day(tracker, _SPRING_FORWARD)

        assert tracker.sample_count == 92
        profile = PriceProfile.from_samples(
            ((start, *sample) for start, sample in tracker._samples.items()),
            _TZ,
            min_days=1,
        )
        assert (_SUN, 2, 0) not in profile.quarters
        assert len(profile.quarters) == 92


# ---------------------------------------------------------------------------
# Planner fill order
# ---------------------------------------------------------------------------


def _point(
    day: int,
    hour: int,
    minute: int,
    import_price: float | None = 5.0,
    export_price: float | None = 4.5,
) -> PricePoint:
    return PricePoint(
        hour=hour,
        import_price=import_price,
        export_price=export_price,
        day_offset=day,
        slot_in_day=hour * 4 + minute // 15,
    )


def _today(hours: range = range(24), **prices: float | None) -> list[PricePoint]:
    return [
        _point(0, hour, minute, **prices)
        for hour in hours
        for minute in (0, 15, 30, 45)
    ]


def _week_profile(
    monday: tuple[float, float] | None,
    tuesday: tuple[float, float] | None,
    tuesday_ten: tuple[float, float] | None = None,
    skip_hour: int | None = None,
) -> PriceProfile:
    """Return a flat profile per weekday with an optional Tuesday 10:00 price."""
    hours: dict[tuple[int, int], tuple[float, float]] = {}
    for weekday, pair in ((_MON, monday), (_TUE, tuesday)):
        for hour in range(24):
            if pair is not None and hour != skip_hour:
                hours[(weekday, hour)] = pair
    if tuesday_ten is not None:
        hours[(_TUE, 10)] = tuesday_ten
    return PriceProfile(hours=hours)


class TestFillOrder:
    """``populate_prices`` on the production (``slot_in_day``) path."""

    def test_published_prices_are_never_touched(self) -> None:
        points = _today() + [
            _point(1, hour, minute) for hour in range(24) for minute in (0, 15, 30, 45)
        ]

        slots, missing = _populated(points, _week_profile((9.0, 8.0), (9.0, 8.0)))

        assert all(
            slot.price.import_price == pytest.approx(5.0) for slot in slots.values()
        )
        assert missing == set()

    def test_the_profile_is_shifted_to_the_published_level(self) -> None:
        """The old level (7.0) is not planned; the Tuesday shape is."""
        profile = _week_profile((7.0, 6.0), (7.0, 6.0), tuesday_ten=(9.0, 8.5))

        slots, missing = _populated(_today(), profile)

        ten = slots[_TUESDAY.replace(hour=10, minute=30)]
        assert ten.price.import_price == pytest.approx(5.0 + 2.0)
        assert ten.price.export_price == pytest.approx(4.5 + 2.5)
        eleven = slots[_TUESDAY.replace(hour=11)]
        assert eleven.price.import_price == pytest.approx(5.0)
        assert eleven.price.export_price == pytest.approx(4.5)
        assert {key.day_offset for key in missing} == {1}
        assert len(missing) == 96

    def test_without_an_overlap_the_earlier_day_is_used(self) -> None:
        """The profile knows Tuesday only: there is no level to shift it to."""
        profile = _week_profile(None, (9.0, 8.0))

        slots, _missing = _populated(_today(), profile)

        ten = slots[_TUESDAY.replace(hour=10)]
        assert ten.price.import_price == pytest.approx(5.0)
        assert ten.price.export_price == pytest.approx(4.5)

    def test_a_short_overlap_is_not_trusted(self) -> None:
        """Three published hours are too few to derive the level from."""
        profile = _week_profile((7.0, 6.0), (9.0, 8.0))

        slots, _missing = _populated(_today(range(3)), profile)

        # Tuesday 01:00 has an earlier day; Tuesday 10:00 has none and reads
        # the profile as recorded.
        assert slots[_TUESDAY.replace(hour=1)].price.import_price == pytest.approx(5.0)
        assert slots[_TUESDAY.replace(hour=10)].price.import_price == pytest.approx(9.0)
        assert slots[_MONDAY.replace(hour=10)].price.import_price == pytest.approx(7.0)

    def test_no_price_points_read_the_profile_as_recorded(self) -> None:
        profile = _week_profile((9.0, 8.0), (7.0, 6.0))

        slots, missing = _populated([], profile)

        assert slots[_MONDAY.replace(hour=10, minute=15)].price.import_price == (
            pytest.approx(9.0)
        )
        assert slots[_TUESDAY.replace(hour=10)].price.export_price == pytest.approx(6.0)
        assert len(missing) == len(slots)

    def test_a_profile_gap_is_not_priced_at_zero(self) -> None:
        """An hour the profile lacks reads the level of the rest (finding 7)."""
        profile = _week_profile((9.0, 8.0), (7.0, 6.0), skip_hour=11)

        slots, _missing = _populated([], profile)

        gap = slots[_MONDAY.replace(hour=11)]
        assert gap.price.import_price == pytest.approx(8.0)
        assert gap.price.export_price == pytest.approx(7.0)

    def test_a_gap_next_to_published_prices_reads_their_level(self) -> None:
        profile = _week_profile((7.0, 6.0), (7.0, 6.0), skip_hour=11)

        slots, _missing = _populated(_today(range(11)), profile)

        # Monday 11:00 is unpublished, has no earlier day and no profile.
        gap = slots[_MONDAY.replace(hour=11)]
        assert gap.price.import_price == pytest.approx(5.0)
        assert gap.price.export_price == pytest.approx(4.5)

    def test_a_published_side_is_kept_when_the_other_is_missing(self) -> None:
        """Import is live, export is not (review finding 3)."""
        points = _today(export_price=None)
        profile = _week_profile((7.0, 6.0), (7.0, 6.0))

        slots, missing = _populated(points, profile)
        without, _ = _populated(points, None)

        ten = slots[_MONDAY.replace(hour=10)]
        assert ten.price.import_price == pytest.approx(5.0)
        assert ten.price.export_price == pytest.approx(6.0)
        assert len(missing) == len(slots)
        assert without[_MONDAY.replace(hour=10)].price.import_price == pytest.approx(
            5.0
        )
        assert without[_MONDAY.replace(hour=10)].price.export_price == pytest.approx(
            0.0
        )

    def test_an_empty_or_absent_profile_changes_nothing(self) -> None:
        with_empty, missing_empty = _populated(_today(), PriceProfile())
        without, missing = _populated(_today(), None)

        assert [slot.price for slot in with_empty.values()] == [
            slot.price for slot in without.values()
        ]
        assert missing_empty == missing
        tuesday = without[_TUESDAY.replace(hour=10)]
        assert tuesday.price.import_price == pytest.approx(5.0)

    def test_no_price_points_and_no_profile_plan_at_zero(self) -> None:
        slots, missing = _populated([], None)

        assert all(abs(slot.price.import_price) < 1e-9 for slot in slots.values())
        assert all(abs(slot.price.export_price) < 1e-9 for slot in slots.values())
        assert len(missing) == len(slots)


class TestFillOrderHourlyPath:
    """Hand-built hourly points, without ``slot_in_day``."""

    @staticmethod
    def _hourly(day: int, hours: range = range(24)) -> list[PricePoint]:
        return [
            PricePoint(hour=hour, import_price=5.0, export_price=4.5, day_offset=day)
            for hour in hours
        ]

    def test_hourly_points_read_the_shifted_profile(self) -> None:
        points = self._hourly(0) + self._hourly(1, range(1))
        profile = _week_profile((7.0, 6.0), (7.0, 6.0), tuesday_ten=(9.0, 8.5))

        slots, missing = _populated(points, profile, interval=60)

        assert slots[_TUESDAY].price.import_price == pytest.approx(5.0)
        ten = slots[_TUESDAY.replace(hour=10)]
        assert ten.price.import_price == pytest.approx(7.0)
        assert ten.price.export_price == pytest.approx(7.0)
        assert len(missing) == 23

    def test_hourly_points_without_a_profile_read_the_earlier_day(self) -> None:
        points = self._hourly(0) + self._hourly(1, range(1))

        slots, _missing = _populated(points, None, interval=60)

        assert slots[_TUESDAY.replace(hour=10)].price.import_price == pytest.approx(5.0)

    def test_legacy_hour_only_points_have_no_earlier_day(self) -> None:
        slots, _missing = _populated(self._hourly(0, range(12)), None, interval=60)

        assert slots[_MONDAY.replace(hour=3)].price.import_price == pytest.approx(5.0)
        assert slots[_MONDAY.replace(hour=20)].price.import_price == pytest.approx(0.0)

    def test_an_hourly_point_with_one_side_is_estimated_as_a_pair(self) -> None:
        points = self._hourly(0, range(23)) + [
            PricePoint(hour=23, import_price=9.0, export_price=None)
        ]

        slots, _missing = _populated(points, None, interval=60, hours=24)

        assert slots[_MONDAY.replace(hour=23)].price.import_price == pytest.approx(0.0)

    def test_without_an_index_a_missing_side_is_zero(self) -> None:
        inp = PlannerInput(
            now_iso=_NOW.isoformat(),
            time_zone="Europe/Copenhagen",
            interval_minutes=60,
            interval_length_hours=24,
        )
        slots = build_slots(inp, _NOW)

        populate_prices(
            slots, [PricePoint(hour=10, import_price=9.0, export_price=None)]
        )

        ten = next(slot for slot in slots if slot.start.hour == 10)
        assert ten.price.import_price == pytest.approx(9.0)
        assert ten.price.export_price == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


@_needs_scipy
class TestUnpublishedTomorrow:
    """Monday 09:05, Tuesday's prices not yet published."""

    def test_tomorrow_has_tuesdays_shape_at_todays_level(self) -> None:
        """The recorded weeks were 3.0 dearer; that level is not planned."""
        planner_input, result = _cycle(_history(), _NOW, _attributes(_MONDAY))
        output = run_planner(planner_input)

        assert result.outage_reason is None
        tomorrow = _slots_of(output, _TUESDAY)
        assert len(tomorrow) == 96
        for slot in tomorrow:
            expected = _clock_price(slot.start.hour, slot.start.minute)
            expected += _TUESDAY_PEAK.get(slot.start.hour, 0.0)
            assert slot.price.import_price == pytest.approx(expected)
            assert slot.price.export_price == pytest.approx(expected - _EXPORT_GAP)
        for slot in _slots_of(output, _MONDAY):
            assert slot.price.import_price == pytest.approx(
                _clock_price(slot.start.hour, slot.start.minute)
            )

    def test_the_gap_is_still_reported(self) -> None:
        planner_input, _ = _cycle(_history(), _NOW, _attributes(_MONDAY))
        quality = run_planner(planner_input).data_quality

        assert quality.today_price_missing_hours == []
        assert quality.tomorrow_price_missing_hours == list(range(24))
        assert quality.is_complete is False

    def test_a_week_of_history_is_too_little_to_use(self) -> None:
        """One Tuesday is not a pattern: tomorrow reads today, as before."""
        tracker = PriceHistoryTracker()
        _record_day(tracker, _LAST_TUESDAY, offset=_OLD_LEVEL, peak=_TUESDAY_PEAK)

        planner_input, _ = _cycle(tracker, _NOW, _attributes(_MONDAY))
        output = run_planner(planner_input)

        for slot in _slots_of(output, _TUESDAY):
            assert slot.price.import_price == pytest.approx(
                _clock_price(slot.start.hour, slot.start.minute)
            )

    def test_an_empty_store_plans_exactly_as_before(self) -> None:
        with_store, _ = _cycle(PriceHistoryTracker(), _NOW, _attributes(_MONDAY))
        without_store, _ = _cycle(None, _NOW, _attributes(_MONDAY))

        assert with_store.price_points == without_store.price_points
        assert with_store.price_profile is not None
        assert with_store.price_profile.is_empty
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
        """The price source and slots were hourly in the recorded weeks."""
        tracker = _history(interval=60, source_step=60)
        assert tracker.sample_count == 12 * 24

        planner_input, _ = _cycle(tracker, _NOW, _attributes(_MONDAY))
        output = run_planner(planner_input)

        # The level shift is the median of today's quarters over the hour.
        shift = (_clock_price(0, 15) + _clock_price(0, 30)) / 2 - _clock_price(0, 0)
        for slot in _slots_of(output, _TUESDAY):
            expected = _clock_price(slot.start.hour, 0) + shift
            expected += _TUESDAY_PEAK.get(slot.start.hour, 0.0)
            assert slot.price.import_price == pytest.approx(expected)

    def test_quarter_hourly_history_prices_hourly_slots(self) -> None:
        """The reverse switch: each hour reads the mean of its quarters."""
        planner_input, _ = _cycle(
            _history(), _NOW, _attributes(_MONDAY, step=60), interval=60, source_step=60
        )
        output = run_planner(planner_input)

        tomorrow = _slots_of(output, _TUESDAY)
        assert len(tomorrow) == 24
        for slot in tomorrow:
            expected = _clock_price(slot.start.hour, 0)
            expected += _TUESDAY_PEAK.get(slot.start.hour, 0.0)
            assert slot.price.import_price == pytest.approx(expected)

    def test_the_week_after_the_fall_back_day(self) -> None:
        """The dearer second pass of one Sunday does not move the bucket."""
        tracker = PriceHistoryTracker()
        _record_weeks(tracker, _FALL_BACK, offset=_OLD_LEVEL)
        saturday = datetime(2026, 10, 31, tzinfo=_TZ)

        planner_input, _ = _cycle(
            tracker, saturday.replace(hour=9, minute=5), _attributes(saturday)
        )
        output = run_planner(planner_input)

        sunday = _slots_of(output, datetime(2026, 11, 1, tzinfo=_TZ))
        assert len(sunday) == 96
        for slot in sunday:
            assert slot.price.import_price == pytest.approx(
                _clock_price(slot.start.hour, slot.start.minute)
            )

    def test_the_week_after_the_spring_forward_day(self) -> None:
        """02:00 has five Sundays only, too few, and reads the earlier day."""
        tracker = PriceHistoryTracker()
        _record_weeks(tracker, _SPRING_FORWARD, offset=_OLD_LEVEL)
        saturday = datetime(2026, 4, 4, tzinfo=_TZ)

        planner_input, _ = _cycle(
            tracker, saturday.replace(hour=9, minute=5), _attributes(saturday)
        )
        profile = planner_input.price_profile
        output = run_planner(planner_input)

        assert profile is not None
        assert (_SUN, 2, 0) not in profile.quarters
        assert (_SUN, 3, 0) in profile.quarters
        for slot in _slots_of(output, datetime(2026, 4, 5, tzinfo=_TZ)):
            assert slot.price.import_price == pytest.approx(
                _clock_price(slot.start.hour, slot.start.minute)
            )


@_needs_scipy
class TestPredictionAttributes:
    """Energi Data Service with Carnot: ``forecast`` on the price sensor."""

    _LATER = _MONDAY.replace(hour=9, minute=10)

    def test_carnot_prices_the_plan_and_is_not_recorded_or_held(self) -> None:
        tracker = PriceHistoryTracker()

        planner_input, result = _cycle(
            tracker, _NOW, _energi_data_service([_MONDAY], [_TUESDAY])
        )

        # The plan uses the Carnot prediction for tomorrow, as before.
        assert result.outage_reason is None
        assert len(planner_input.price_points) == 192
        assert planner_input.price_points[96 + 40].import_price == pytest.approx(
            _clock_price(10, 0) + _CARNOT_OFFSET
        )
        assert tracker.held_price("import", _MONDAY.replace(hour=20)) == pytest.approx(
            _clock_price(20, 0)
        )
        assert tracker.held_price("import", _TUESDAY.replace(hour=10)) is None
        assert tracker.sample_count == 37

    def test_carnot_on_a_started_slot_is_not_recorded(self) -> None:
        """Without ``raw_today`` a prediction covers the slots that started."""
        tracker = PriceHistoryTracker()

        planner_input, result = _cycle(
            tracker, _NOW, _energi_data_service([], [_MONDAY, _TUESDAY])
        )

        assert tracker.sample_count == 0
        assert tracker.held_price("import", _MONDAY.replace(hour=20)) is None
        # The sensor is alive, so this is not an outage and it plans as before.
        assert result.outage_reason is None
        assert result.coverage.import_predictions_only is True
        assert result.coverage.export_predictions_only is False
        assert len(planner_input.price_points) == 192

    def test_carnot_is_not_restored_in_an_outage(self) -> None:
        tracker = PriceHistoryTracker()
        _cycle(tracker, _NOW, _energi_data_service([_MONDAY], [_TUESDAY]))

        planner_input, result = _cycle(tracker, self._LATER, {})
        output = run_planner(planner_input)

        assert result.outage_reason is not None
        assert result.coverage.has_import_price(_MONDAY.replace(hour=20))
        assert not result.coverage.has_import_price(_TUESDAY.replace(hour=10))
        assert result.coverage.has_export_price(_TUESDAY.replace(hour=10))
        for slot in _slots_of(output, _MONDAY):
            assert slot.price.import_price == pytest.approx(
                _clock_price(slot.start.hour, slot.start.minute)
            )
        quality = output.data_quality
        assert quality.today_price_missing_hours == []
        assert quality.tomorrow_price_missing_hours == list(range(24))

    def test_a_forecast_sensors_prediction_is_reported_in_an_outage(self) -> None:
        """The price sensor is down and only the forecast sensor has tomorrow."""
        tracker = PriceHistoryTracker()
        attributes = _attributes(_MONDAY, _TUESDAY)
        del attributes[_IMPORT]
        attributes[_IMPORT_FORECAST] = {
            "predictions": _day_points(_TUESDAY, offset=_CARNOT_OFFSET)
        }
        _cycle(tracker, _NOW, _attributes(_MONDAY))

        planner_input, result = _cycle(tracker, self._LATER, attributes)
        output = run_planner(planner_input)

        assert result.outage_reason is not None
        assert planner_input.price_points[40].import_price == pytest.approx(
            _clock_price(10, 0)
        )
        assert planner_input.price_points[96 + 40].import_price is None
        assert output.data_quality.tomorrow_price_missing_hours == list(range(24))


@_needs_scipy
class TestPriceOutage:
    """The price sensors stop delivering at 09:10 on Monday."""

    _LATER = _MONDAY.replace(hour=9, minute=10)

    def _outage(
        self, tracker: PriceHistoryTracker, attributes: dict[str, Any] | None = None
    ) -> tuple[PlannerInput, PriceHistoryResult]:
        _cycle(tracker, _NOW, _attributes(_MONDAY))
        return _cycle(tracker, self._LATER, attributes or {})

    def test_published_prices_are_held_and_the_rest_comes_from_the_store(
        self,
    ) -> None:
        planner_input, result = self._outage(_history())
        output = run_planner(planner_input)

        assert result.outage_reason is not None
        assert result.expired is False
        for slot in _slots_of(output, _MONDAY):
            expected = _clock_price(slot.start.hour, slot.start.minute)
            assert slot.price.import_price == pytest.approx(expected)
            assert slot.price.export_price == pytest.approx(expected - _EXPORT_GAP)
        for slot in _slots_of(output, _TUESDAY):
            expected = _clock_price(slot.start.hour, slot.start.minute)
            expected += _TUESDAY_PEAK.get(slot.start.hour, 0.0)
            assert slot.price.import_price == pytest.approx(expected)
        quality = output.data_quality
        assert quality.today_price_missing_hours == []
        assert quality.tomorrow_price_missing_hours == list(range(24))

    def test_the_reason_names_the_cause(self) -> None:
        _planner_input, result = self._outage(PriceHistoryTracker())

        assert result.outage_reason == (
            "Price data unavailable (import and export price) for 0.0 h: "
            "planning 96 of 192 slots from held published prices and the rest "
            "from the price history (37 recorded slots). The planner stops "
            "after 48 h"
        )

    def test_a_live_import_price_survives_an_export_outage(self) -> None:
        """Held prices must not overwrite a fresh side (review finding 3)."""
        tracker = PriceHistoryTracker()
        fresh = _attributes(_MONDAY, offset=0.25)
        del fresh[_EXPORT]

        planner_input, result = self._outage(tracker, fresh)
        output = run_planner(planner_input)

        assert result.outage_reason is not None
        assert result.outage_reason.startswith(
            "Price data unavailable (export price) for 0.0 h: planning 96 of 192"
        )
        for slot in _slots_of(output, _MONDAY):
            expected = _clock_price(slot.start.hour, slot.start.minute)
            assert slot.price.import_price == pytest.approx(expected + 0.25)
            assert slot.price.export_price == pytest.approx(expected - _EXPORT_GAP)
        # The fresh import prices are held for the next cycle.
        assert tracker.held_price("import", _MONDAY.replace(hour=20)) == pytest.approx(
            _clock_price(20, 0) + 0.25
        )

    def test_a_held_price_beats_a_forecast_sensors_prediction(self) -> None:
        tracker = PriceHistoryTracker()
        attributes = _attributes(_MONDAY)
        del attributes[_IMPORT]
        attributes[_IMPORT_FORECAST] = {
            "prices_today": _day_points(_MONDAY, offset=5.0)
        }

        planner_input, _ = self._outage(tracker, attributes)

        assert planner_input.price_points[40].import_price == pytest.approx(
            _clock_price(10, 0)
        )

    def test_two_sources_that_never_overlap_are_priced_as_before(self) -> None:
        """No slot has a pair, but neither sensor is down: no outage."""
        tracker = PriceHistoryTracker()
        attributes = _attributes(_MONDAY)
        attributes[_EXPORT] = {"prices_today": _day_points(_TUESDAY)}

        planner_input, result = _cycle(tracker, _NOW, attributes)

        assert result.outage_reason is None
        assert result.coverage.single_sided_prices is False
        assert tracker.sample_count == 0
        assert planner_input.price_points == []

    def test_a_live_side_is_kept_where_nothing_is_held(self) -> None:
        """Export is down from the first cycle: import still prices the plan."""
        attributes = _attributes(_MONDAY)
        del attributes[_EXPORT]

        planner_input, result = _cycle(PriceHistoryTracker(), _NOW, attributes)
        output = run_planner(planner_input)

        assert result.outage_reason is not None
        assert "planning 0 of 192 slots" in result.outage_reason
        assert len(planner_input.price_points) == 96
        assert planner_input.price_points[0].export_price is None
        for slot in _slots_of(output, _MONDAY):
            assert slot.price.import_price == pytest.approx(
                _clock_price(slot.start.hour, slot.start.minute)
            )
            assert slot.price.export_price == pytest.approx(0.0)

    def test_held_prices_are_not_recorded_and_pruning_still_runs(self) -> None:
        """An outage must not stop the retention window (review finding 4)."""
        tracker = PriceHistoryTracker()
        old = _MONDAY - timedelta(days=PRICE_HISTORY_RETENTION_DAYS, hours=-12)
        tracker.record(old, 15, 1.0, 0.5)
        _cycle(tracker, _NOW, _attributes(_MONDAY))
        assert tracker.sample_count == 38

        _cycle(tracker, _MONDAY.replace(hour=15), {})

        assert tracker.sample_count == 37

    def test_the_outage_continues_into_the_next_day(self) -> None:
        """Monday's held prices are over; Tuesday comes from the store.

        Tuesday starts within a day of the last published slot, so the
        profile keeps Monday's level shift.  Wednesday is too late for it
        and reads the profile as recorded (issue #1452).
        """
        tracker = _history()
        _cycle(tracker, _NOW, _attributes(_MONDAY))

        planner_input, result = _cycle(tracker, _TUESDAY.replace(hour=9, minute=5), {})
        output = run_planner(planner_input)

        assert result.outage_reason is not None
        assert "for 0.0 h: planning 0 of 192 slots" in result.outage_reason
        assert result.coverage.import_price == frozenset()
        assert planner_input.price_points == []
        profile = planner_input.price_profile
        assert profile is not None
        for level in (profile.import_level, profile.export_level):
            assert level is not None
            assert level[0] == pytest.approx(-_OLD_LEVEL)
            assert level[1] == _WEDNESDAY
        for slot in _slots_of(output, _TUESDAY):
            expected = _clock_price(slot.start.hour, slot.start.minute)
            expected += _TUESDAY_PEAK.get(slot.start.hour, 0.0)
            assert slot.price.import_price == pytest.approx(expected)
            assert slot.price.export_price == pytest.approx(expected - _EXPORT_GAP)
        # Wednesday was never recorded: it reads every weekday, never 0.0.
        assert all(
            slot.price.import_price > _OLD_LEVEL
            for slot in _slots_of(output, _WEDNESDAY)
        )
        assert output.data_quality.today_price_missing_hours == list(range(24))

    def test_the_outage_expires(self) -> None:
        """After 48 h the stored prices no longer drive the plan (finding 4)."""
        tracker = _history()
        _cycle(tracker, _NOW, _attributes(_MONDAY))
        start = self._LATER
        _cycle(tracker, start, {})
        still, _ = (
            _cycle(tracker, start + timedelta(hours=PRICE_OUTAGE_MAX_HOURS), {}),
            0,
        )
        late = start + timedelta(hours=PRICE_OUTAGE_MAX_HOURS, minutes=5)

        planner_input, result = _cycle(tracker, late, {})

        assert still[1].expired is False
        assert result.expired is True
        assert result.outage_reason is None
        assert planner_input.price_points == []

    def test_the_outage_clock_resets_when_prices_return(self) -> None:
        tracker = PriceHistoryTracker()
        self._outage(tracker)
        assert tracker.outage_since is not None

        _planner_input, result = _cycle(
            tracker, _MONDAY.replace(hour=12), _attributes(_MONDAY)
        )

        assert result.outage_reason is None
        assert tracker.outage_since is None

    def test_an_empty_store_plans_exactly_as_before(self) -> None:
        """Nothing held, nothing stored: every price is 0.0, as today."""
        with_store, result = _cycle(PriceHistoryTracker(), self._LATER, {})
        without_store, plain = _cycle(None, self._LATER, {})
        output = run_planner(with_store)
        before = run_planner(without_store)

        assert result.coverage == plain.coverage
        assert result.outage_reason is not None
        assert with_store.price_points == []
        assert all(abs(slot.price.import_price) < 1e-9 for slot in output.slots)
        assert all(abs(slot.price.export_price) < 1e-9 for slot in output.slots)
        assert [slot.recommendation for slot in output.slots] == [
            slot.recommendation for slot in before.slots
        ]
        assert output.data_quality.today_price_missing_hours == list(range(24))


# ---------------------------------------------------------------------------
# Backtest corpus
# ---------------------------------------------------------------------------


@_needs_scipy
@pytest.mark.parametrize("path", corpus_paths(), ids=lambda path: path.stem)
def test_an_old_price_level_does_not_move_a_corpus_plan(path: Path) -> None:
    """Tomorrow unpublished, history 1.0 dearer: same prices as without it.

    Every recorded cycle publishes both days, so the profile is unused on
    the corpus as it is.  Here tomorrow's points are removed and the profile
    is today's own shape at an older, dearer level.  Shifted to today's
    level it must give what the nearest-earlier-day estimate gives.
    """
    recorded, _report = planner_input_from_dict(json.loads(path.read_text()))
    today = [pp for pp in recorded.price_points if pp.day_offset == 0]
    assert recorded.interval_minutes == 15
    assert len(today) == 96
    profile = PriceProfile(
        any_day_quarters={
            (pp.hour, (pp.slot_in_day or 0) * 15 % 60): (
                (pp.import_price or 0.0) + 1.0,
                (pp.export_price or 0.0) + 1.0,
            )
            for pp in today
        }
    )
    without = run_planner(replace(recorded, price_points=today))
    with_profile = run_planner(
        replace(recorded, price_points=today, price_profile=profile)
    )

    assert len(without.data_quality.tomorrow_price_missing_hours) == 24
    for after, before in zip(with_profile.slots, without.slots):
        assert after.price.import_price == pytest.approx(before.price.import_price)
        assert after.price.export_price == pytest.approx(before.price.export_price)
    assert with_profile.plan_cost is not None
    assert without.plan_cost is not None
    assert with_profile.plan_cost.total_cost == pytest.approx(
        without.plan_cost.total_cost, abs=1e-6
    )


# ---------------------------------------------------------------------------
# Degraded mode, performance and the coordinator
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

    def test_an_expired_outage_is_degraded_not_an_error(self) -> None:
        live = LiveState()

        live.add_missing_entity(PRICE_OUTAGE_EXPIRED_LABEL)

        assert live.degraded_mode is DegradedMode.Degraded


class TestInControl:
    """Performance scoring of a cycle (review finding 5)."""

    def test_a_price_outage_cycle_is_in_control(self) -> None:
        """The planner ran and wrote to the hardware: the result is HSEM's."""
        live = LiveState()
        live.add_degraded_reason("price outage")

        sample = build_performance_sample(_NOW, live, SensorConfig())

        assert live.degraded_mode is DegradedMode.Degraded
        assert sample.in_control is True

    def test_a_cycle_with_a_missing_entity_is_not_in_control(self) -> None:
        """The planner was skipped."""
        live = LiveState()
        live.add_missing_entity(PRICE_OUTAGE_EXPIRED_LABEL)

        assert build_performance_sample(_NOW, live, SensorConfig()).in_control is False

    def test_a_healthy_cycle_is_in_control_unless_read_only(self) -> None:
        read_only = SensorConfig()
        read_only.read_only = True

        assert build_performance_sample(_NOW, LiveState(), SensorConfig()).in_control
        assert not build_performance_sample(_NOW, LiveState(), read_only).in_control


def _published(recs: list[Any], covered: Any) -> ForecastCoverage:
    """Write 2.0/1.0 to the covered slots and return their coverage."""
    prices_in: dict[datetime, float] = {}
    prices_out: dict[datetime, float] = {}
    for rec in recs:
        if covered(rec):
            rec.import_price = 2.0
            rec.export_price = 1.0
            prices_in[rec.start.astimezone(UTC)] = 2.0
            prices_out[rec.start.astimezone(UTC)] = 1.0
    return ForecastCoverage(
        import_price=frozenset(prices_in),
        export_price=frozenset(prices_out),
        published_import=prices_in,
        published_export=prices_out,
    )


class TestCoordinatorCycle:
    """The real coordinator cycle records, holds, reports and persists."""

    @staticmethod
    async def _collect(coordinator: Any, covered: Any) -> str | None:
        """Run the collect phase with a populator that covers *covered*."""
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
                side_effect=lambda recs, *_args: _published(recs, covered),
            ),
        ):
            result = await coordinator._async_collect_and_populate(_FIXTURE_NOW)
        state: str | None = result[1]
        return state

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

        state = await self._collect(coordinator, lambda rec: False)

        live = coordinator._live
        assert live is not None
        assert state is None
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
            "Price data unavailable (import and export price) for 0.0 h: "
            "planning 192 of 192"
        )

    @pytest.mark.asyncio
    async def test_an_expired_outage_stops_the_planner(self, tmp_path: Path) -> None:
        coordinator, _build, _solved = _coordinator(
            tmp_path, reference=_plan(grid_charge=False), floor_enabled=False
        )
        tracker = coordinator._price_history_tracker
        tracker.history_file = str(tmp_path / "hsem_price_history.json")
        tracker.outage_since = _FIXTURE_NOW.astimezone(UTC) - timedelta(
            hours=PRICE_OUTAGE_MAX_HOURS + 1
        )

        state = await self._collect(coordinator, lambda rec: False)

        live = coordinator._live
        assert live is not None
        assert state == Recommendations.MissingInputEntities.value
        assert live.missing_entities_list == [PRICE_OUTAGE_EXPIRED_LABEL]
        assert live.degraded_reasons == []
        assert live.degraded_mode is DegradedMode.Degraded

    @pytest.mark.asyncio
    async def test_a_changed_price_sensor_resets_the_history(
        self, tmp_path: Path
    ) -> None:
        """The cycle binds the store to the price source (review finding 2)."""
        coordinator, _build, _solved = _coordinator(
            tmp_path, reference=_plan(grid_charge=False), floor_enabled=False
        )
        tracker = coordinator._price_history_tracker
        tracker.history_file = str(tmp_path / "hsem_price_history.json")
        await self._collect(coordinator, lambda rec: True)
        recorded = tracker.sample_count
        assert recorded > 0

        configured = tracker.source["import_entity"]
        marker = _FIXTURE_NOW - timedelta(days=7)
        tracker.record(marker, 15, 9.0, 8.0)
        tracker.source["import_entity"] = "sensor.the_previous_tariff"

        await self._collect(coordinator, lambda rec: True)

        assert marker.astimezone(UTC) not in tracker._samples
        assert tracker.sample_count == recorded
        assert tracker.source["import_entity"] == configured

    def test_the_source_reads_entities_and_units(self) -> None:
        cfg = _cfg()
        snapshot = StateSnapshot(
            live=LiveState(),
            sensor_attributes={_IMPORT: {"unit_of_measurement": "DKK/kWh"}},
        )

        assert price_source(cfg, snapshot) == {**_SOURCE, "export_unit": ""}
        assert price_source(SensorConfig(), None) == dict.fromkeys(_SOURCE, "")

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
            MagicMock(spec=[]), coverage, _MONDAY, live, _cfg()
        )

        assert result is coverage
        assert live.degraded_reasons == []

    @pytest.mark.asyncio
    async def test_a_cycle_without_coverage_skips_the_step(self) -> None:
        coordinator = MagicMock()
        coordinator._price_history_tracker = PriceHistoryTracker()
        live = LiveState()

        result = await async_apply_price_history(
            coordinator, None, _MONDAY, live, _cfg()
        )

        assert result is None
        assert live.degraded_reasons == []


class TestDiagnosticsRoundTrip:
    """The profile survives the diagnostics dump and the backtest replay."""

    def test_the_dump_and_the_replay_keep_the_profile(self) -> None:
        profile = PriceProfile.from_samples(
            [(_MONDAY.replace(hour=10), 60, 1.0, 0.5)], _TZ, min_days=1
        )
        with_profile = PlannerInput(now_iso=_MONDAY.isoformat(), price_profile=profile)
        without = PlannerInput(now_iso=_MONDAY.isoformat())

        dumped = json.loads(json.dumps(_planner_input_to_dict(with_profile)))
        replayed, report = planner_input_from_dict({"planner_input": dumped})

        assert replayed.price_profile == profile
        assert report.is_faithful
        assert _planner_input_to_dict(without)["price_profile"] is None

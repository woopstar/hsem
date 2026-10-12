"""Tests for the performance tracker's per-slot record (issue #1182).

What actually happened is built up from consecutive coordinator samples.
These tests pin how a sample pair is split over the slots it spans, what
leaves a slot unobserved, that the record survives a restart, and that the
trailing window and the local days are laid out correctly on DST days.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import homeassistant.util.dt as dt_util

from custom_components.hsem.models.performance_record import (
    REASON_BREAKDOWN_UNSOLVED,
    REASON_INSUFFICIENT_COVERAGE,
    REASON_NO_SITE_LIMITS,
    REASON_ORACLE_UNSOLVED,
    REASON_POTENTIAL_BELOW_THRESHOLD,
    REASON_SCIPY_UNAVAILABLE,
    PerformanceSample,
    PerformanceScore,
    PerformanceSlot,
)
from custom_components.hsem.models.performance_tracker import (
    MAX_DAYS,
    REASON_NOT_EVALUATED,
    RETAIN_SLOT_HOURS,
    SCHEMA_VERSION,
    PerformanceTracker,
    sum_scores,
)
from custom_components.hsem.planner.hindsight_scoring import SiteLimits
from tests.backtest.scoring import day_slot_keys

_TZ = ZoneInfo("Europe/Copenhagen")
_T0 = datetime(2026, 6, 10, 12, 0, tzinfo=_TZ)
_GAP = 600.0
_SITE = SiteLimits(
    rated_kwh=10.0,
    min_soc_pct=5.0,
    max_soc_pct=100.0,
    max_charge_kw=5.0,
    max_discharge_kw=5.0,
    charge_efficiency=0.97,
    discharge_efficiency=0.97,
    max_grid_export_kw=None,
)


@pytest.fixture(autouse=True)
def _copenhagen() -> Iterator[None]:
    """Run every test with Home Assistant's zone set to Europe/Copenhagen."""
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(_TZ)
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


def _sample(
    at: datetime,
    *,
    grid_import: float | None = 0.0,
    grid_export: float | None = 0.0,
    soc: float | None = 50.0,
    import_price: float | None = 1.0,
    export_price: float | None = 0.7,
    **extra: object,
) -> PerformanceSample:
    return PerformanceSample(
        at=at,
        grid_import_energy_kwh=grid_import,
        grid_export_energy_kwh=grid_export,
        soc_pct=soc,
        import_price=import_price,
        export_price=export_price,
        **extra,  # type: ignore[arg-type]
    )


def _feed(tracker: PerformanceTracker, *samples: PerformanceSample) -> None:
    for sample in samples:
        tracker.accumulate(sample, slot_minutes=15, max_gap_seconds=_GAP)


def _slot(tracker: PerformanceTracker, at: datetime) -> PerformanceSlot:
    return tracker.slots[at.astimezone(UTC)]


def _scored(
    day: str,
    *,
    realized: float,
    oracle: float,
    baseline: float,
    potential: float,
    pv_as_realized: float | None = None,
) -> PerformanceScore:
    start = datetime.fromisoformat(day).replace(tzinfo=UTC)
    return PerformanceScore(
        start=start,
        end=start + timedelta(days=1),
        slot_count=96,
        coverage=1.0,
        control_coverage=1.0,
        realized_cost=realized,
        oracle_cost=oracle,
        baseline_cost=baseline,
        potential=potential,
        pv_as_realized_cost=pv_as_realized,
        start_kwh=4.0,
        realized_end_kwh=4.0,
        baseline_end_kwh=4.0,
        day=day,
    )


class TestAccumulation:
    def test_the_first_sample_only_sets_the_baseline(self) -> None:
        tracker = PerformanceTracker()

        _feed(tracker, _sample(_T0, grid_import=1234.5))

        assert tracker.slots == {}

    def test_meter_deltas_land_in_the_slot_they_happened_in(self) -> None:
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(_T0, grid_import=100.0, grid_export=20.0),
            _sample(
                _T0 + timedelta(minutes=5), grid_import=100.4, grid_export=20.1, soc=52
            ),
        )

        slot = _slot(tracker, _T0)
        assert slot.grid_import_kwh == pytest.approx(0.4)
        assert slot.grid_export_kwh == pytest.approx(0.1)
        assert slot.coverage_seconds == pytest.approx(300.0)
        assert slot.coverage == pytest.approx(1 / 3)
        assert slot.soc_start_pct == pytest.approx(50.0)
        assert slot.soc_end_pct == pytest.approx(52.0)

    def test_an_interval_across_a_boundary_is_split_by_time(self) -> None:
        """Ten minutes, four before the boundary and six after it."""
        tracker = PerformanceTracker()
        start = _T0 + timedelta(minutes=11)

        _feed(
            tracker,
            _sample(start, grid_import=10.0, soc=40.0),
            _sample(start + timedelta(minutes=10), grid_import=11.0, soc=50.0),
        )

        first = _slot(tracker, _T0)
        second = _slot(tracker, _T0 + timedelta(minutes=15))
        assert first.grid_import_kwh == pytest.approx(0.4)
        assert second.grid_import_kwh == pytest.approx(0.6)
        assert first.coverage_seconds == pytest.approx(240.0)
        assert second.coverage_seconds == pytest.approx(360.0)
        # The SoC at the boundary is read off the line between the samples,
        # and is the same on both sides of it.
        assert first.soc_end_pct == pytest.approx(44.0)
        assert second.soc_start_pct == pytest.approx(44.0)
        assert second.soc_end_pct == pytest.approx(50.0)

    def test_an_interval_is_priced_at_the_price_it_started_with(self) -> None:
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(_T0, import_price=1.0, export_price=0.5),
            _sample(_T0 + timedelta(minutes=5), import_price=4.0, export_price=2.0),
            _sample(_T0 + timedelta(minutes=15), import_price=9.0, export_price=9.0),
        )

        slot = _slot(tracker, _T0)
        # Five minutes at 1.0 and ten at 4.0.
        assert slot.import_price == pytest.approx(3.0)
        assert slot.export_price == pytest.approx(1.5)

    def test_an_interval_across_a_boundary_takes_each_sides_price(self) -> None:
        """The old price does not leak into the slot after a price change."""
        tracker = PerformanceTracker()
        start = _T0 + timedelta(minutes=12)

        _feed(
            tracker,
            _sample(start, import_price=1.0, export_price=0.5),
            _sample(start + timedelta(minutes=5), import_price=4.0, export_price=2.0),
        )

        assert _slot(tracker, _T0).import_price == pytest.approx(1.0)
        after = _slot(tracker, _T0 + timedelta(minutes=15))
        assert after.import_price == pytest.approx(4.0)
        assert after.export_price == pytest.approx(2.0)

    def test_the_later_slot_falls_back_to_the_earlier_price(self) -> None:
        """A price that went missing after the boundary does not cost the slot."""
        tracker = PerformanceTracker()
        start = _T0 + timedelta(minutes=12)

        _feed(
            tracker,
            _sample(start, import_price=1.0, export_price=0.5),
            _sample(start + timedelta(minutes=5), import_price=None, export_price=None),
        )

        after = _slot(tracker, _T0 + timedelta(minutes=15))
        assert after.import_price == pytest.approx(1.0)
        assert after.export_price == pytest.approx(0.5)

    def test_control_is_counted_only_while_hsem_was_in_control(self) -> None:
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(_T0),
            _sample(_T0 + timedelta(minutes=5), in_control=False),
            _sample(_T0 + timedelta(minutes=10), in_control=False),
            _sample(_T0 + timedelta(minutes=15)),
        )

        slot = _slot(tracker, _T0)
        assert slot.coverage_seconds == pytest.approx(900.0)
        # No interval has HSEM in control at both of its ends.
        assert slot.control_seconds == pytest.approx(0.0)

    def test_a_slot_under_control_throughout(self) -> None:
        tracker = PerformanceTracker()

        _feed(tracker, _sample(_T0), _sample(_T0 + timedelta(minutes=5)))

        assert _slot(tracker, _T0).control_seconds == pytest.approx(300.0)


class TestMissingIsNotZero:
    def test_a_gap_longer_than_the_tolerance_is_left_unobserved(self) -> None:
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(_T0, grid_import=10.0),
            _sample(_T0 + timedelta(minutes=30), grid_import=15.0),
        )

        assert tracker.slots == {}

    def test_the_sample_after_a_gap_is_the_new_baseline(self) -> None:
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(_T0, grid_import=10.0),
            _sample(_T0 + timedelta(minutes=30), grid_import=15.0),
            _sample(_T0 + timedelta(minutes=35), grid_import=15.2),
        )

        slot = _slot(tracker, _T0 + timedelta(minutes=30))
        assert slot.grid_import_kwh == pytest.approx(0.2)

    @pytest.mark.parametrize(
        "broken",
        [
            {"grid_import": None},
            {"grid_export": None},
            {"soc": None},
        ],
    )
    def test_a_missing_reading_leaves_the_interval_out(
        self, broken: dict[str, None]
    ) -> None:
        tracker = PerformanceTracker()

        _feed(tracker, _sample(_T0), _sample(_T0 + timedelta(minutes=5), **broken))

        assert tracker.slots == {}

    @pytest.mark.parametrize("price", ["import_price", "export_price"])
    def test_an_interval_without_a_price_is_left_out(self, price: str) -> None:
        """The price that counts is the one in force when the interval began."""
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(_T0, **{price: None}),
            _sample(_T0 + timedelta(minutes=5)),
        )

        assert tracker.slots == {}

    def test_a_meter_that_went_backwards_is_not_a_negative_flow(self) -> None:
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(_T0, grid_import=500.0),
            _sample(_T0 + timedelta(minutes=5), grid_import=0.0),
        )

        assert tracker.slots == {}

    def test_a_meter_jump_is_not_taken_for_energy(self) -> None:
        """6000 kWh in five minutes is a glitch, not an import."""
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(_T0, grid_import=0.0),
            _sample(_T0 + timedelta(minutes=5), grid_import=6000.0),
        )

        assert tracker.slots == {}

    def test_a_coarse_meter_step_between_two_close_cycles_is_energy(self) -> None:
        """A meter counting in whole kWh steps once, seconds after a cycle."""
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(_T0, grid_import=500.0),
            _sample(_T0 + timedelta(seconds=5), grid_import=501.0),
        )

        assert _slot(tracker, _T0).grid_import_kwh == pytest.approx(1.0)

    def test_samples_out_of_order_add_nothing(self) -> None:
        tracker = PerformanceTracker()

        _feed(tracker, _sample(_T0), _sample(_T0 - timedelta(minutes=1)))

        assert tracker.slots == {}


class TestOptionalSeries:
    def test_pv_comes_from_the_meter_when_there_is_one(self) -> None:
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(_T0, pv_energy_kwh=100.0, pv_power_w=9999.0),
            _sample(_T0 + timedelta(minutes=5), pv_energy_kwh=100.3, pv_power_w=9999.0),
        )

        assert _slot(tracker, _T0).pv_kwh == pytest.approx(0.3)

    def test_pv_falls_back_to_the_power_reading(self) -> None:
        """The mean of 2 kW and 4 kW held for five minutes."""
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(_T0, pv_power_w=2000.0),
            _sample(_T0 + timedelta(minutes=5), pv_power_w=4000.0),
        )

        assert _slot(tracker, _T0).pv_kwh == pytest.approx(0.25)

    def test_pv_unobserved_for_part_of_a_slot_is_unknown_for_all_of_it(self) -> None:
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(_T0, pv_power_w=2000.0),
            _sample(_T0 + timedelta(minutes=5)),
            _sample(_T0 + timedelta(minutes=10), pv_power_w=2000.0),
            _sample(_T0 + timedelta(minutes=14), pv_power_w=2000.0),
        )

        slot = _slot(tracker, _T0)
        assert slot.pv_kwh is None
        assert slot.coverage_seconds == pytest.approx(840.0)

    def test_ev_energy_and_connection_are_recorded_when_configured(self) -> None:
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(_T0, ev_power_w=7200.0, ev_connected=True),
            _sample(_T0 + timedelta(minutes=5), ev_power_w=7200.0, ev_connected=True),
        )

        slot = _slot(tracker, _T0)
        assert slot.ev_kwh == pytest.approx(0.6)
        assert slot.ev_connected is True

    def test_no_ev_configured_leaves_both_unknown(self) -> None:
        tracker = PerformanceTracker()

        _feed(tracker, _sample(_T0), _sample(_T0 + timedelta(minutes=5)))

        slot = _slot(tracker, _T0)
        assert slot.ev_kwh is None
        assert slot.ev_connected is None

    def test_an_unplugged_ev_is_recorded_as_such(self) -> None:
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(_T0, ev_power_w=0.0, ev_connected=False),
            _sample(_T0 + timedelta(minutes=5), ev_power_w=0.0, ev_connected=False),
        )

        slot = _slot(tracker, _T0)
        assert slot.ev_kwh == pytest.approx(0.0)
        assert slot.ev_connected is False


class TestRetention:
    def test_slots_older_than_the_retention_are_dropped(self) -> None:
        tracker = PerformanceTracker()
        old = _T0 - timedelta(hours=RETAIN_SLOT_HOURS + 1)

        _feed(tracker, _sample(old), _sample(old + timedelta(minutes=5)))
        assert len(tracker.slots) == 1
        _feed(tracker, _sample(_T0), _sample(_T0 + timedelta(minutes=5)))

        assert list(tracker.slots) == [_T0.astimezone(UTC)]

    def test_a_changed_slot_width_starts_the_slot_afresh(self) -> None:
        tracker = PerformanceTracker()
        _feed(tracker, _sample(_T0), _sample(_T0 + timedelta(minutes=5)))

        tracker.accumulate(
            _sample(_T0 + timedelta(minutes=10)), slot_minutes=60, max_gap_seconds=_GAP
        )

        slot = _slot(tracker, _T0)
        assert slot.minutes == 60
        assert slot.coverage_seconds == pytest.approx(300.0)

    def test_only_the_newest_days_are_kept(self) -> None:
        tracker = PerformanceTracker()
        first = date(2026, 1, 1)
        days = [
            _scored(
                (first + timedelta(days=offset)).isoformat(),
                realized=1.0,
                oracle=0.5,
                baseline=2.0,
                potential=1.5,
            )
            for offset in range(MAX_DAYS + 5)
        ]

        tracker.apply(days[-1], days)

        assert len(tracker.days) == MAX_DAYS
        assert min(tracker.days) == (first + timedelta(days=5)).isoformat()


class TestWindowAndDays:
    def test_the_window_is_the_24_hours_before_the_current_slot(self) -> None:
        tracker = PerformanceTracker()
        now = _T0 + timedelta(minutes=7)

        request = tracker.request(now, slot_minutes=15, zone=_TZ)

        assert len(request.window) == 96
        assert request.window[0] == (_T0 - timedelta(hours=24)).astimezone(UTC)
        assert request.window[-1] == (_T0 - timedelta(minutes=15)).astimezone(UTC)

    @pytest.mark.parametrize(
        "now",
        [
            # Spring forward: 02:00 → 03:00 on 29 March 2026.
            datetime(2026, 3, 29, 12, 0, tzinfo=_TZ),
            # Fall back: 03:00 → 02:00 on 25 October 2026.
            datetime(2026, 10, 25, 12, 0, tzinfo=_TZ),
        ],
    )
    def test_the_window_is_24_physical_hours_on_a_dst_day(self, now: datetime) -> None:
        tracker = PerformanceTracker()

        request = tracker.request(now, slot_minutes=15, zone=_TZ)

        assert len(request.window) == 96
        assert request.window[-1] + timedelta(minutes=15) == now.astimezone(UTC)
        assert request.window[0] == now.astimezone(UTC) - timedelta(hours=24)
        steps = {
            later - earlier
            for earlier, later in zip(request.window, request.window[1:], strict=False)
        }
        assert steps == {timedelta(minutes=15)}

    @pytest.mark.parametrize(
        ("day", "count"),
        [
            (date(2026, 3, 29), 92),
            (date(2026, 10, 25), 100),
            (date(2026, 6, 10), 96),
        ],
    )
    def test_a_local_day_has_the_slots_the_backtest_gives_it(
        self, day: date, count: int
    ) -> None:
        """A DST day has 92 or 100 quarter-hours, here and in the harness."""
        tracker = PerformanceTracker()
        noon = datetime.combine(day, datetime.min.time(), _TZ) + timedelta(hours=12)
        _feed(tracker, _sample(noon), _sample(noon + timedelta(minutes=5)))

        request = tracker.request(
            noon + timedelta(days=1, hours=1), slot_minutes=15, zone=_TZ
        )

        assert [name for name, _ in request.days] == [day.isoformat()]
        keys = request.days[0][1]
        assert len(keys) == count
        assert list(keys) == day_slot_keys(day, _TZ, 15)

    def test_a_day_is_not_asked_for_before_it_has_ended(self) -> None:
        tracker = PerformanceTracker()
        _feed(tracker, _sample(_T0), _sample(_T0 + timedelta(minutes=5)))

        request = tracker.request(
            _T0 + timedelta(hours=11, minutes=59), slot_minutes=15, zone=_TZ
        )

        assert request.days == ()

    def test_a_day_is_asked_for_once_it_has_ended(self) -> None:
        tracker = PerformanceTracker()
        _feed(tracker, _sample(_T0), _sample(_T0 + timedelta(minutes=5)))

        request = tracker.request(_T0 + timedelta(hours=12), slot_minutes=15, zone=_TZ)

        assert [name for name, _ in request.days] == ["2026-06-10"]

    def test_a_day_that_has_a_score_is_not_asked_for_again(self) -> None:
        tracker = PerformanceTracker()
        _feed(tracker, _sample(_T0), _sample(_T0 + timedelta(minutes=5)))
        tracker.days["2026-06-10"] = _scored(
            "2026-06-10", realized=1.0, oracle=0.5, baseline=2.0, potential=1.5
        )

        request = tracker.request(_T0 + timedelta(hours=12), slot_minutes=15, zone=_TZ)

        assert request.days == ()

    def test_the_request_is_detached_from_the_tracker(self) -> None:
        """The executor reads a copy while the tracker keeps recording."""
        tracker = PerformanceTracker(site=_SITE)
        _feed(tracker, _sample(_T0), _sample(_T0 + timedelta(minutes=5)))
        request = tracker.request(_T0 + timedelta(hours=1), slot_minutes=15, zone=_TZ)

        _feed(tracker, _sample(_T0 + timedelta(minutes=10), grid_import=3.0))

        copy = request.slots[_T0.astimezone(UTC)]
        assert copy.grid_import_kwh == pytest.approx(0.0)
        assert _slot(tracker, _T0).grid_import_kwh == pytest.approx(3.0)
        assert request.site == _SITE

    def test_an_evaluation_is_due_until_the_window_is_current(self) -> None:
        tracker = PerformanceTracker()
        now = _T0 + timedelta(minutes=7)
        assert tracker.needs_evaluation(now, 15)

        window = tracker.request(now, slot_minutes=15, zone=_TZ).window
        tracker.apply(
            PerformanceScore(
                start=window[0],
                end=window[-1] + timedelta(minutes=15),
                slot_count=96,
                reason=REASON_INSUFFICIENT_COVERAGE,
            )
        )

        assert not tracker.needs_evaluation(now, 15)
        assert not tracker.needs_evaluation(_T0 + timedelta(minutes=14), 15)
        assert tracker.needs_evaluation(_T0 + timedelta(minutes=15), 15)

    def test_a_day_without_site_limits_is_tried_again(self) -> None:
        tracker = PerformanceTracker()
        waiting = PerformanceScore(
            start=_T0.astimezone(UTC),
            end=_T0.astimezone(UTC) + timedelta(days=1),
            slot_count=96,
            reason=REASON_NO_SITE_LIMITS,
            day="2026-06-10",
        )

        tracker.apply(waiting, [waiting])

        assert tracker.days == {}

    def test_a_day_that_could_not_be_scored_is_kept_as_such(self) -> None:
        tracker = PerformanceTracker()
        gap = PerformanceScore(
            start=_T0.astimezone(UTC),
            end=_T0.astimezone(UTC) + timedelta(days=1),
            slot_count=96,
            reason=REASON_INSUFFICIENT_COVERAGE,
            day="2026-06-10",
        )

        tracker.apply(gap, [gap])

        assert tracker.days["2026-06-10"].reason == REASON_INSUFFICIENT_COVERAGE


class TestPeriods:
    """A period figure is a ratio of sums over the scored days."""

    def _tracker(self) -> PerformanceTracker:
        tracker = PerformanceTracker()
        # A day with a lot to gain, mostly gained, and a day with little to
        # gain, none of it gained.
        tracker.days["2026-06-08"] = _scored(
            "2026-06-08", realized=11.0, oracle=10.0, baseline=20.0, potential=10.0
        )
        tracker.days["2026-06-09"] = _scored(
            "2026-06-09", realized=6.0, oracle=5.0, baseline=6.0, potential=1.0
        )
        return tracker

    def test_capture_is_a_ratio_of_sums_not_a_mean_of_percentages(self) -> None:
        period = self._tracker().period(date(2026, 6, 10), 7)

        # Days: 90 % and 0 %.  Their mean is 45 %; the period's capture is
        # (9 + 0) ÷ (10 + 1).
        assert period["missed_opportunity"] == pytest.approx(2.0)
        assert period["actual_benefit"] == pytest.approx(9.0)
        assert period["potential"] == pytest.approx(11.0)
        assert period["savings_capture"] == pytest.approx(81.8)
        assert period["scored_days"] == 2
        assert period["days"] == 7

    def test_today_is_not_part_of_the_period(self) -> None:
        period = self._tracker().period(date(2026, 6, 9), 7)

        assert period["scored_days"] == 1
        assert period["potential"] == pytest.approx(10.0)

    def test_days_before_the_period_are_left_out(self) -> None:
        period = self._tracker().period(date(2026, 6, 10), 1)

        assert period["scored_days"] == 1
        assert period["potential"] == pytest.approx(1.0)

    def test_an_unscored_day_is_counted_not_summed(self) -> None:
        tracker = self._tracker()
        tracker.days["2026-06-07"] = PerformanceScore(
            start=_T0.astimezone(UTC),
            end=_T0.astimezone(UTC),
            slot_count=96,
            reason=REASON_INSUFFICIENT_COVERAGE,
            realized_cost=999.0,
            day="2026-06-07",
        )

        period = tracker.period(date(2026, 6, 10), 7)

        assert period["scored_days"] == 2
        assert period["potential"] == pytest.approx(11.0)

    def test_a_period_without_a_scored_day_has_no_capture(self) -> None:
        period = PerformanceTracker().period(date(2026, 6, 10), 30)

        assert period["savings_capture"] is None
        assert period["scored_days"] == 0
        assert period["actual_benefit"] == pytest.approx(0.0)

    def test_a_period_with_almost_nothing_to_gain_has_no_capture(self) -> None:
        tracker = PerformanceTracker()
        tracker.days["2026-06-09"] = _scored(
            "2026-06-09", realized=5.0, oracle=4.99, baseline=5.01, potential=0.02
        )

        period = tracker.period(date(2026, 6, 10), 7)

        assert period["savings_capture"] is None
        assert period["missed_opportunity"] == pytest.approx(0.01)

    def test_the_sums_take_any_additive_field(self) -> None:
        """Later KPIs add their own quantities to the same rollup."""
        scores = list(self._tracker().days.values())

        assert sum_scores(scores, ("baseline_cost",)) == {"baseline_cost": 26.0}


class TestBatteryAndSolarPeriods:
    """The split's period figures are ratios of sums too (issue #1270)."""

    def _tracker(self) -> PerformanceTracker:
        tracker = PerformanceTracker()
        # 1.0 missed of 10: 0.8 on the battery, 0.2 on solar.
        tracker.days["2026-06-08"] = _scored(
            "2026-06-08",
            realized=11.0,
            oracle=10.0,
            baseline=20.0,
            potential=10.0,
            pv_as_realized=10.2,
        )
        # 1.0 missed of 1: all of it on solar.
        tracker.days["2026-06-09"] = _scored(
            "2026-06-09",
            realized=6.0,
            oracle=5.0,
            baseline=6.0,
            potential=1.0,
            pv_as_realized=6.0,
        )
        return tracker

    def test_losses_and_shares_over_the_period(self) -> None:
        period = self._tracker().period(date(2026, 6, 10), 7)

        assert period["battery_loss"] == pytest.approx(0.8)
        assert period["solar_loss"] == pytest.approx(1.2)
        assert period["battery_loss"] + period["solar_loss"] == pytest.approx(
            period["missed_opportunity"]
        )
        # 1 − 0.8 ÷ 11 and 1 − 1.2 ÷ 11, not the mean of the daily shares.
        assert period["battery_utilisation"] == pytest.approx(92.7)
        assert period["solar_utilisation"] == pytest.approx(89.1)
        assert period["breakdown_days"] == 2

    def test_days_scored_before_the_split_are_left_out_of_it(self) -> None:
        """Their potential must not dilute the shares of the days that have it."""
        tracker = self._tracker()
        tracker.days["2026-06-07"] = _scored(
            "2026-06-07", realized=30.0, oracle=20.0, baseline=60.0, potential=40.0
        )

        period = tracker.period(date(2026, 6, 10), 7)

        assert period["scored_days"] == 3
        assert period["breakdown_days"] == 2
        assert period["missed_opportunity"] == pytest.approx(12.0)
        assert period["battery_loss"] == pytest.approx(0.8)
        assert period["battery_utilisation"] == pytest.approx(92.7)

    def test_a_period_without_a_split_day_has_no_split(self) -> None:
        tracker = PerformanceTracker()
        tracker.days["2026-06-09"] = _scored(
            "2026-06-09", realized=6.0, oracle=5.0, baseline=6.0, potential=1.0
        )

        period = tracker.period(date(2026, 6, 10), 7)

        assert period["battery_loss"] is None and period["solar_loss"] is None
        assert period["battery_utilisation"] is None
        assert period["solar_utilisation"] is None
        assert period["breakdown_days"] == 0
        assert period["scored_days"] == 1

    def test_the_window_attributes_carry_the_split(self) -> None:
        tracker = PerformanceTracker()
        tracker.window = _scored(
            "2026-06-09",
            realized=3.4,
            oracle=3.0,
            baseline=5.0,
            potential=2.0,
            pv_as_realized=3.1,
        )

        attributes = tracker.as_sensor_attributes(_T0)

        assert attributes["battery_loss"] == pytest.approx(0.3)
        assert attributes["solar_loss"] == pytest.approx(0.1)
        assert attributes["battery_utilisation"] == pytest.approx(85.0)
        assert attributes["solar_utilisation"] == pytest.approx(95.0)
        assert attributes["breakdown_reason"] is None
        # 20 points short of 100 %: 15 on the battery, 5 on solar.
        assert attributes["savings_capture"] == pytest.approx(80.0)

    def test_a_window_without_the_split_says_why(self) -> None:
        tracker = PerformanceTracker()
        tracker.window = _scored(
            "2026-06-09", realized=3.4, oracle=3.0, baseline=5.0, potential=2.0
        )

        attributes = tracker.as_sensor_attributes(_T0)

        assert attributes["savings_capture"] == pytest.approx(80.0)
        assert attributes["reason"] is None
        assert attributes["battery_utilisation"] is None
        assert attributes["breakdown_reason"] == REASON_BREAKDOWN_UNSOLVED

    def test_before_the_first_evaluation_the_split_is_unknown_too(self) -> None:
        attributes = PerformanceTracker().as_sensor_attributes(_T0)

        assert attributes["breakdown_reason"] == REASON_NOT_EVALUATED

    def test_the_split_survives_a_round_trip(self) -> None:
        tracker = self._tracker()

        restored = PerformanceTracker.from_dict(
            json.loads(json.dumps(tracker.as_dict()))
        )

        assert restored.days == tracker.days
        assert restored.days["2026-06-08"].battery_loss == pytest.approx(0.8)

    def test_a_history_file_from_before_the_split_is_still_read(self) -> None:
        data = self._tracker().as_dict()
        for day in data["days"]:
            del day["pv_as_realized_cost"]

        restored = PerformanceTracker.from_dict(data)

        assert len(restored.days) == 2
        day = restored.days["2026-06-08"]
        assert day.regret == pytest.approx(1.0)
        assert day.pv_as_realized_cost is None
        assert day.battery_loss is None


class TestSensorAttributes:
    def test_before_the_first_evaluation(self) -> None:
        attributes = PerformanceTracker().as_sensor_attributes(_T0)

        assert attributes["reason"] == REASON_NOT_EVALUATED
        assert attributes["solver_status"] == "not_run"
        assert attributes["yesterday"] is None
        assert attributes["last_7_days"]["scored_days"] == 0
        assert attributes["last_30_days"]["days"] == 30

    def test_a_scored_window(self) -> None:
        tracker = PerformanceTracker()
        tracker.window = _scored(
            "2026-06-09", realized=3.4, oracle=3.0, baseline=5.0, potential=2.0
        )
        tracker.days["2026-06-09"] = tracker.window

        attributes = tracker.as_sensor_attributes(_T0)

        assert attributes["reason"] is None
        assert attributes["solver_status"] == "optimal"
        assert attributes["actual_benefit"] == pytest.approx(1.6)
        assert attributes["missed_opportunity"] == pytest.approx(0.4)
        assert attributes["savings_capture"] == pytest.approx(80.0)
        assert attributes["realized_cost"] == pytest.approx(3.4)
        assert attributes["baseline_cost"] == pytest.approx(5.0)
        assert attributes["oracle_cost"] == pytest.approx(3.0)
        assert attributes["coverage"] == pytest.approx(1.0)
        assert attributes["control_coverage"] == pytest.approx(1.0)
        assert attributes["start_stored_kwh"] == pytest.approx(4.0)
        assert attributes["realized_end_stored_kwh"] == pytest.approx(4.0)
        assert attributes["baseline_end_stored_kwh"] == pytest.approx(4.0)
        assert attributes["slot_count"] == 96
        # Published in the site's zone, not in UTC.
        assert attributes["window_start"] == "2026-06-09T02:00:00+02:00"
        assert attributes["window_end"] == "2026-06-10T02:00:00+02:00"
        assert attributes["yesterday"]["date"] == "2026-06-09"
        assert attributes["yesterday"]["savings_capture"] == pytest.approx(80.0)

    def test_a_window_with_almost_nothing_to_gain_says_so(self) -> None:
        """The amounts stay valid; only the share is unknown."""
        tracker = PerformanceTracker()
        tracker.window = _scored(
            "2026-06-09", realized=5.0, oracle=4.99, baseline=5.01, potential=0.02
        )

        attributes = tracker.as_sensor_attributes(_T0)

        assert attributes["reason"] == REASON_POTENTIAL_BELOW_THRESHOLD
        assert attributes["savings_capture"] is None
        assert attributes["missed_opportunity"] == pytest.approx(0.01)
        assert attributes["solver_status"] == "optimal"

    @pytest.mark.parametrize(
        ("reason", "status"),
        [
            (REASON_SCIPY_UNAVAILABLE, "unavailable"),
            (REASON_ORACLE_UNSOLVED, "unsolved"),
            (REASON_INSUFFICIENT_COVERAGE, "not_run"),
        ],
    )
    def test_an_unscored_window_names_its_reason(
        self, reason: str, status: str
    ) -> None:
        tracker = PerformanceTracker()
        tracker.window = PerformanceScore(
            start=_T0.astimezone(UTC),
            end=_T0.astimezone(UTC) + timedelta(days=1),
            slot_count=96,
            reason=reason,
            coverage=0.4,
        )

        attributes = tracker.as_sensor_attributes(_T0)

        assert attributes["reason"] == reason
        assert attributes["solver_status"] == status
        assert attributes["actual_benefit"] is None
        assert attributes["missed_opportunity"] is None
        assert attributes["savings_capture"] is None
        assert attributes["coverage"] == pytest.approx(0.4)


class TestPersistence:
    def _recorded(self) -> PerformanceTracker:
        tracker = PerformanceTracker(site=_SITE)
        _feed(
            tracker,
            _sample(_T0, grid_import=10.0, pv_power_w=1000.0, ev_connected=True),
            _sample(
                _T0 + timedelta(minutes=5),
                grid_import=10.3,
                soc=53.0,
                pv_power_w=1000.0,
                ev_connected=True,
            ),
        )
        tracker.apply(
            _scored(
                "2026-06-09", realized=3.4, oracle=3.0, baseline=5.0, potential=2.0
            ),
            [
                _scored(
                    "2026-06-09", realized=3.4, oracle=3.0, baseline=5.0, potential=2.0
                )
            ],
        )
        return tracker

    def test_a_round_trip_through_json_keeps_everything(self) -> None:
        tracker = self._recorded()

        restored = PerformanceTracker.from_dict(
            json.loads(json.dumps(tracker.as_dict()))
        )

        assert restored.slots == tracker.slots
        assert restored.days == tracker.days
        assert restored.window == tracker.window
        assert restored.site == _SITE
        assert restored._last_sample == tracker._last_sample

    def test_a_restart_mid_slot_loses_nothing(self, tmp_path: Path) -> None:
        """The persisted last sample is the baseline the next one continues from."""
        samples = [
            _sample(
                _T0 + timedelta(minutes=5 * i), grid_import=10.0 + 0.1 * i, soc=50 + i
            )
            for i in range(7)
        ]
        uninterrupted = PerformanceTracker()
        _feed(uninterrupted, *samples)

        before = PerformanceTracker(history_file=str(tmp_path / "performance.json"))
        _feed(before, *samples[:3])
        assert asyncio.run(before.save_history()) is True
        after = PerformanceTracker(history_file=str(tmp_path / "performance.json"))
        asyncio.run(after.load_history())
        _feed(after, *samples[3:])

        assert after.slots.keys() == uninterrupted.slots.keys()
        for start, slot in uninterrupted.slots.items():
            restored = after.slots[start]
            assert restored.grid_import_kwh == pytest.approx(slot.grid_import_kwh)
            assert restored.coverage_seconds == pytest.approx(slot.coverage_seconds)
            assert restored.soc_start_pct == pytest.approx(slot.soc_start_pct)
            assert restored.soc_end_pct == pytest.approx(slot.soc_end_pct)

    def test_a_file_of_another_schema_is_not_read(self) -> None:
        data = self._recorded().as_dict()
        data["schema"] = SCHEMA_VERSION + 1

        restored = PerformanceTracker.from_dict(data)

        assert restored.slots == {}
        assert restored.days == {}
        assert restored.window is None

    def test_entries_that_do_not_parse_are_left_out(self) -> None:
        data = self._recorded().as_dict()
        data["slots"].append({"start": "not a time"})
        data["slots"].append("nonsense")
        data["days"].append({"start": None})
        data["window"] = {"start": 5}
        data["site"] = {"rated_kwh": "ten"}
        data["last_sample"] = {"at": "2026-06-10T12:00:00"}

        restored = PerformanceTracker.from_dict(data)

        assert len(restored.slots) == 1
        assert list(restored.days) == ["2026-06-09"]
        assert restored.window is None
        assert restored.site is None
        assert restored._last_sample is None

    def test_site_limits_without_grid_caps_survive(self) -> None:
        tracker = PerformanceTracker(site=_SITE)

        restored = PerformanceTracker.from_dict(tracker.as_dict())

        assert restored.site == _SITE
        assert restored.site is not None
        assert restored.site.max_grid_import_kw is None

    def test_site_limits_with_a_broken_flag_are_dropped(self) -> None:
        data = PerformanceTracker(site=_SITE).as_dict()
        data["site"]["battery_export_enabled"] = "yes"

        assert PerformanceTracker.from_dict(data).site is None

    def test_nothing_is_saved_without_a_history_file(self) -> None:
        tracker = self._recorded()

        assert asyncio.run(tracker.save_history()) is False
        asyncio.run(tracker.load_history())
        assert tracker.window is not None

    def test_a_missing_history_file_leaves_the_tracker_empty(
        self, tmp_path: Path
    ) -> None:
        tracker = PerformanceTracker(history_file=str(tmp_path / "absent.json"))

        asyncio.run(tracker.load_history())

        assert tracker.slots == {}
        assert tracker.history_file.endswith("absent.json")

    def test_saving_is_due_after_new_scores_and_after_a_while(
        self, tmp_path: Path
    ) -> None:
        tracker = PerformanceTracker(history_file=str(tmp_path / "performance.json"))
        assert not tracker.save_due(120.0)

        _feed(tracker, _sample(_T0))
        assert tracker.save_due(120.0)
        assert asyncio.run(tracker.save_history()) is True
        assert not tracker.save_due(120.0)

        _feed(tracker, _sample(_T0 + timedelta(seconds=60)))
        assert not tracker.save_due(120.0)
        _feed(tracker, _sample(_T0 + timedelta(seconds=120)))
        assert tracker.save_due(120.0)

        assert asyncio.run(tracker.save_history()) is True
        tracker.apply(
            _scored("2026-06-09", realized=3.4, oracle=3.0, baseline=5.0, potential=2.0)
        )
        assert tracker.save_due(120.0)

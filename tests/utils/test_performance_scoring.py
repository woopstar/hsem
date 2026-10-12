"""Tests for the scoring behind the performance sensors (issue #1182).

Recorded slots are turned into the realized window the shared hindsight
scoring works on.  These tests pin three things: that a run is scored only
when its data can be stood behind (and says why when it cannot), that the
battery's flows are the ones its SoC implies, and that a day scored here
equals the same day scored by ``scripts/backtest_score.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import homeassistant.util.dt as dt_util

from custom_components.hsem.models.performance_record import (
    REASON_ENERGY_BALANCE_RESIDUAL,
    REASON_INSUFFICIENT_COVERAGE,
    REASON_NO_SITE_LIMITS,
    REASON_ORACLE_UNSOLVED,
    REASON_POTENTIAL_BELOW_THRESHOLD,
    REASON_SCIPY_UNAVAILABLE,
    REASON_STORED_ENERGY_OUTSIDE_LIMITS,
    PerformanceSlot,
)
from custom_components.hsem.planner.hindsight_scoring import (
    RealizedSlot,
    SiteLimits,
    realized_window,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils import performance_scoring
from custom_components.hsem.utils.performance_scoring import (
    PerformanceRequest,
    evaluate_performance,
    realized_slots,
    score_slots,
    unexplained_energy_kwh,
)
from tests.backtest.actuals import Actuals, load_actuals
from tests.backtest.conftest import CORPUS_DIR
from tests.backtest.harvest import newest_cycle_of_site
from tests.backtest.recorded_day import load_recorded_day, recorded_days
from tests.backtest.replay import load_planner_input
from tests.backtest.scoring import day_slot_keys, merge_actuals, score_day

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_TZ = ZoneInfo("Europe/Copenhagen")
_START = datetime(2026, 6, 9, 22, 0, tzinfo=UTC)
_SITE = SiteLimits(
    rated_kwh=10.0,
    min_soc_pct=5.0,
    max_soc_pct=100.0,
    max_charge_kw=5.0,
    max_discharge_kw=5.0,
    charge_efficiency=0.97,
    discharge_efficiency=0.97,
)
#: Cheap night, dear evening.
_PRICES = [0.5] * 6 + [1.2] * 11 + [2.5] * 4 + [1.2] * 3
_LOAD = [0.6] * 17 + [2.1] * 4 + [0.6] * 3


@pytest.fixture(autouse=True)
def _copenhagen() -> Iterator[None]:
    """Run every test with Home Assistant's zone set to Europe/Copenhagen."""
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(_TZ)
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


def _keys(count: int = 24) -> list[datetime]:
    return [_START + timedelta(hours=index) for index in range(count)]


def _idle_day(soc: float = 40.0) -> dict[datetime, PerformanceSlot]:
    """Return 24 hourly slots in which the battery sat at *soc*."""
    return {
        key: PerformanceSlot(
            start=key,
            minutes=60,
            grid_import_kwh=load,
            grid_export_kwh=0.0,
            soc_start_pct=soc,
            soc_end_pct=soc,
            import_price=price,
            export_price=price - 0.3,
            coverage_seconds=3600.0,
            control_seconds=3600.0,
            pv_kwh=0.0,
            ev_kwh=None,
        )
        for key, price, load in zip(_keys(), _PRICES, _LOAD, strict=True)
    }


class TestAScoredRun:
    def test_an_idle_battery_leaves_the_whole_potential_on_the_table(self) -> None:
        score = score_slots(_keys(), _idle_day(), 60, _SITE)

        assert score.reason is None
        assert score.is_scored
        assert score.start == _START
        assert score.end == _START + timedelta(hours=24)
        assert score.slot_count == 24
        assert score.coverage == pytest.approx(1.0)
        assert score.control_coverage == pytest.approx(1.0)
        assert score.realized_cost == pytest.approx(
            sum(p * q for p, q in zip(_PRICES, _LOAD, strict=True))
        )
        assert score.start_kwh == pytest.approx(4.0)
        assert score.realized_end_kwh == pytest.approx(4.0)
        assert score.regret is not None and score.regret > 1.0
        assert score.savings is not None and score.potential is not None
        assert score.savings + score.regret == pytest.approx(score.potential)

    def test_the_oracle_does_not_drain_the_battery_at_the_end(self) -> None:
        """A battery left full is not a missed sale: the oracle must end as full."""
        slots = _idle_day(soc=90.0)

        score = score_slots(_keys(), slots, 60, _SITE)
        window = realized_window(
            realized_slots(list(slots.values()), _SITE), 9.0, _SITE
        )
        drained = window.oracle(_SITE.battery().min_stored_kwh)

        assert score.realized_end_kwh == pytest.approx(9.0)
        assert score.oracle_cost is not None and drained is not None
        # Selling the 8.5 kWh the battery still holds would have been worth a
        # lot, and none of it is counted as missed.
        assert drained.stored_kwh[-1] < 1.0
        assert score.oracle_cost > drained.cost + 5.0

    def test_holding_energy_through_a_dear_evening_is_missed(self) -> None:
        """The #1125 case: a reserve kept while importing at the peak price."""
        score = score_slots(_keys(), _idle_day(soc=90.0), 60, _SITE)

        # Using the battery in the evening and refilling it at the cheaper
        # price afterwards is worth at least this much.
        assert score.regret is not None
        assert score.regret > 3.0

    def test_control_coverage_tells_a_forced_day_from_a_planner_miss(self) -> None:
        slots = _idle_day()
        for key in _keys()[:12]:
            slots[key].control_seconds = 0.0

        score = score_slots(_keys(), slots, 60, _SITE)

        assert score.is_scored
        assert score.coverage == pytest.approx(1.0)
        assert score.control_coverage == pytest.approx(0.5)

    def test_capture_is_unknown_when_nothing_could_be_saved(self) -> None:
        """Flat prices: no control beats self-consumption by anything."""
        slots = _idle_day(soc=5.0)
        for slot in slots.values():
            slot.import_price = 1.0
            slot.export_price = 0.7

        score = score_slots(_keys(), slots, 60, _SITE)

        assert score.is_scored
        assert score.capture is None
        assert score.capture_reason == REASON_POTENTIAL_BELOW_THRESHOLD
        assert score.regret == pytest.approx(0.0, abs=1e-6)

    def test_a_negative_capture_is_reported_as_negative(self) -> None:
        """Buying at the peak to fill the battery is worse than doing nothing."""
        slots = _idle_day(soc=5.0)
        keys = _keys()
        for index in range(17, 21):
            slot = slots[keys[index]]
            slot.grid_import_kwh += 2.0
            slot.soc_start_pct = 5.0 + 19.4 * (index - 17)
            slot.soc_end_pct = 5.0 + 19.4 * (index - 16)
        for key in keys[21:]:
            slots[key].soc_start_pct = slots[key].soc_end_pct = 5.0 + 19.4 * 4

        score = score_slots(keys, slots, 60, _SITE)

        assert score.is_scored
        assert score.capture is not None
        assert score.capture < 0.0

    def test_an_empty_run_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="at least one slot"):
            score_slots([], {}, 60, _SITE)


class TestGuards:
    """No number is published that cannot be stood behind."""

    def test_no_site_limits(self) -> None:
        score = score_slots(_keys(), _idle_day(), 60, None)

        assert score.reason == REASON_NO_SITE_LIMITS
        assert not score.is_scored
        assert score.regret is None and score.savings is None
        assert score.capture is None
        assert score.capture_reason == REASON_NO_SITE_LIMITS
        assert score.coverage == pytest.approx(1.0)

    def test_a_missing_slot(self) -> None:
        slots = _idle_day()
        del slots[_keys()[7]]

        score = score_slots(_keys(), slots, 60, _SITE)

        assert score.reason == REASON_INSUFFICIENT_COVERAGE
        assert score.coverage == pytest.approx(23 / 24)
        assert score.realized_cost is None

    def test_a_slot_observed_for_too_little_of_its_length(self) -> None:
        slots = _idle_day()
        slots[_keys()[3]].coverage_seconds = 0.8 * 3600.0

        score = score_slots(_keys(), slots, 60, _SITE)

        assert score.reason == REASON_INSUFFICIENT_COVERAGE

    def test_a_slot_just_short_of_complete_is_enough(self) -> None:
        slots = _idle_day()
        slots[_keys()[3]].coverage_seconds = 0.95 * 3600.0

        assert score_slots(_keys(), slots, 60, _SITE).is_scored

    def test_a_slot_of_another_width(self) -> None:
        slots = _idle_day()
        slots[_keys()[3]].minutes = 15

        score = score_slots(_keys(), slots, 60, _SITE)

        assert score.reason == REASON_INSUFFICIENT_COVERAGE
        assert score.coverage == pytest.approx(23 / 24)

    def test_a_slot_without_a_soc_reading(self) -> None:
        slots = _idle_day()
        slots[_keys()[0]].soc_start_pct = None

        assert (
            score_slots(_keys(), slots, 60, _SITE).reason
            == REASON_INSUFFICIENT_COVERAGE
        )

    def test_nothing_observed_at_all(self) -> None:
        score = score_slots(_keys(), {}, 60, _SITE)

        assert score.reason == REASON_INSUFFICIENT_COVERAGE
        assert score.coverage == pytest.approx(0.0)
        assert score.control_coverage is None

    def test_scipy_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(performance_scoring, "is_scipy_available", lambda: False)

        score = score_slots(_keys(), _idle_day(), 60, _SITE)

        assert score.reason == REASON_SCIPY_UNAVAILABLE
        assert score.realized_cost is None

    def test_the_solve_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            performance_scoring, "score_window", lambda *_args, **_kwargs: None
        )

        score = score_slots(_keys(), _idle_day(), 60, _SITE)

        assert score.reason == REASON_ORACLE_UNSOLVED
        assert score.realized_cost is None
        # What was measured is still published for diagnosis.
        assert score.start_kwh == pytest.approx(4.0)
        assert score.realized_end_kwh == pytest.approx(4.0)

    def test_the_solve_runs_out_of_time(self) -> None:
        """A result that is not proven optimal is not a result."""
        score = score_slots(_keys(), _idle_day(), 60, _SITE, time_limit_s=0.0)

        assert score.reason == REASON_ORACLE_UNSOLVED

    def test_stored_energy_far_outside_the_configured_capacity(self) -> None:
        """The SoC ran to 100 % on a battery configured to stop at 60 %."""
        slots = _idle_day(soc=100.0)
        capped = replace(_SITE, max_soc_pct=60.0)

        score = score_slots(_keys(), slots, 60, capped)

        assert score.reason == REASON_STORED_ENERGY_OUTSIDE_LIMITS
        assert score.realized_cost is None
        assert any("leave the configured capacity" in note for note in score.notes)

    def test_stored_energy_slightly_outside_is_widened_not_refused(self) -> None:
        """A SoC of 3 % against a 5 % end-of-discharge setting is noise."""
        score = score_slots(_keys(), _idle_day(soc=3.0), 60, _SITE)

        assert score.is_scored

    def test_meters_that_contradict_each_other(self) -> None:
        """Exporting 3 kWh an hour with no PV, an idle battery and no import."""
        slots = _idle_day()
        for key in _keys()[:6]:
            slots[key].grid_import_kwh = 0.0
            slots[key].grid_export_kwh = 3.0

        score = score_slots(_keys(), slots, 60, _SITE)

        assert score.reason == REASON_ENERGY_BALANCE_RESIDUAL
        assert score.residual_kwh == pytest.approx(18.0)
        assert score.realized_cost is None

    def test_the_residual_is_unknown_without_pv(self) -> None:
        """Without PV in every slot the check cannot be made, so it is skipped."""
        slots = _idle_day()
        for key in _keys()[:6]:
            slots[key].grid_import_kwh = 0.0
            slots[key].grid_export_kwh = 3.0
        slots[_keys()[10]].pv_kwh = None

        score = score_slots(_keys(), slots, 60, _SITE)

        assert score.is_scored
        assert score.residual_kwh is None


class TestBatteryFlows:
    def test_each_slots_flow_runs_to_the_next_slots_starting_soc(self) -> None:
        """A short gap inside a slot does not break the chain."""
        slots = [
            PerformanceSlot(
                start=_START, minutes=60, soc_start_pct=40.0, soc_end_pct=49.0
            ),
            PerformanceSlot(
                start=_START + timedelta(hours=1),
                minutes=60,
                soc_start_pct=50.0,
                soc_end_pct=30.0,
            ),
        ]

        realized = realized_slots(slots, _SITE)

        # 40 → 50 % (the next slot's start), then 50 → 30 %.
        assert realized[0].charged_kwh == pytest.approx(1.0 / 0.97)
        assert realized[0].discharged_kwh == pytest.approx(0.0)
        assert realized[1].charged_kwh == pytest.approx(0.0)
        assert realized[1].discharged_kwh == pytest.approx(2.0 * 0.97)

    def test_the_load_is_derived_from_the_meters(self) -> None:
        """Import − export − charged + discharged, with the EV inside it."""
        slot = PerformanceSlot(
            start=_START,
            minutes=60,
            grid_import_kwh=3.0,
            grid_export_kwh=0.5,
            soc_start_pct=50.0,
            soc_end_pct=40.0,
            ev_kwh=2.0,
        )

        (realized,) = realized_slots([slot], _SITE)

        assert realized.net_load_kwh == pytest.approx(3.0 - 0.5 + 0.97)

    def test_the_ev_is_fixed_load_in_every_run(self) -> None:
        """Recording EV energy does not change a score in stage 1."""
        plain = _idle_day()
        with_ev = _idle_day()
        for slot in with_ev.values():
            slot.ev_kwh = 0.3
            slot.ev_connected = True

        assert score_slots(_keys(), with_ev, 60, _SITE) == score_slots(
            _keys(), plain, 60, _SITE
        )

    def test_the_run_ends_with_the_measured_stored_energy(self) -> None:
        slots = _idle_day()
        keys = _keys()
        readings = [40.0 + 2.0 * index for index in range(25)]
        for index, key in enumerate(keys):
            slots[key].soc_start_pct = readings[index]
            slots[key].soc_end_pct = readings[index + 1]
            slots[key].grid_import_kwh += 0.2 / 0.97

        score = score_slots(keys, slots, 60, _SITE)

        assert score.is_scored
        assert score.realized_end_kwh == pytest.approx(8.8)


class TestUnexplainedEnergy:
    def _slot(self, net: float, pv: float | None = 0.0) -> RealizedSlot:
        return RealizedSlot(
            hours=1.0,
            grid_import_kwh=max(net, 0.0),
            grid_export_kwh=max(-net, 0.0),
            charged_kwh=0.0,
            discharged_kwh=0.0,
            import_price=1.0,
            export_price=1.0,
            pv_kwh=pv,
        )

    def test_consistent_meters_leave_nothing_unexplained(self) -> None:
        slots = [self._slot(0.5), self._slot(-1.0, pv=1.4), self._slot(0.2)]

        assert unexplained_energy_kwh(slots) == pytest.approx(0.0)

    def test_noise_that_cancels_in_the_next_slot_stays_small(self) -> None:
        """One SoC step booked a slot early is not a broken meter."""
        slots = [self._slot(0.3), self._slot(-0.1), self._slot(0.3)]

        assert unexplained_energy_kwh(slots) == pytest.approx(0.1)

    def test_a_persistent_negative_load_adds_up(self) -> None:
        slots = [self._slot(0.5)] + [self._slot(-1.0)] * 4

        assert unexplained_energy_kwh(slots) == pytest.approx(4.0)

    def test_unknown_without_pv(self) -> None:
        assert unexplained_energy_kwh([self._slot(0.5), self._slot(0.5, None)]) is None


class TestEvaluate:
    def test_the_window_and_the_days_are_scored_together(self) -> None:
        keys = tuple(_keys())
        request = PerformanceRequest(
            slots=_idle_day(),
            slot_minutes=60,
            window=keys,
            days=(("2026-06-10", keys),),
            site=_SITE,
        )

        evaluation = evaluate_performance(request)

        assert evaluation.window.is_scored
        assert evaluation.window.day is None
        (day,) = evaluation.days
        assert day.day == "2026-06-10"
        assert day.regret == pytest.approx(evaluation.window.regret)


# ---------------------------------------------------------------------------
# The same day, on the device and in the backtest
# ---------------------------------------------------------------------------


def _recorded() -> tuple[date, Actuals, SiteLimits]:
    """Return the committed recorded day, its actuals and the site's limits."""
    day, directory = next(iter(recorded_days().items()))
    _cycles, actuals = load_recorded_day(directory)
    cycle = newest_cycle_of_site(CORPUS_DIR, actuals.site_tag)
    assert cycle is not None
    planner_input, _ = load_planner_input(cycle)
    return day, actuals, SiteLimits.from_planner_input(planner_input)


def _slots_from_actuals(
    actuals: Actuals, keys: list[datetime]
) -> dict[datetime, PerformanceSlot]:
    """Return what the device would have recorded for the slots of *actuals*."""
    step = timedelta(minutes=actuals.slot_minutes)
    return {
        key: PerformanceSlot(
            start=key,
            minutes=actuals.slot_minutes,
            grid_import_kwh=actuals._lookup("grid_import", key) or 0.0,
            grid_export_kwh=actuals._lookup("grid_export", key) or 0.0,
            soc_start_pct=actuals._lookup("battery_soc_pct", key),
            soc_end_pct=actuals._lookup("battery_soc_pct", key + step),
            import_price=actuals._lookup("import_price", key) or 0.0,
            export_price=actuals._lookup("export_price", key) or 0.0,
            coverage_seconds=step.total_seconds(),
            control_seconds=step.total_seconds(),
            pv_kwh=actuals._lookup("pv_produced", key),
            ev_kwh=None,
        )
        for key in keys
    }


class TestTheSameDayAsTheBacktest:
    """The sensors and ``scripts/backtest_score.py`` must not disagree."""

    def test_a_committed_day_scores_the_same_on_both_sides(self) -> None:
        """Same slots, same SoC: ``backtest_score.py --battery-flows soc``."""
        day, actuals, site = _recorded()
        keys = day_slot_keys(day, _TZ, actuals.slot_minutes)

        device = score_slots(
            keys, _slots_from_actuals(actuals, keys), actuals.slot_minutes, site
        )
        backtest = score_day(actuals, day, _TZ, site, flows_from_soc=True)

        assert device.is_scored and backtest.is_scored
        assert device.realized_cost == pytest.approx(backtest.realized_cost)
        assert device.oracle_cost == pytest.approx(backtest.oracle_cost)
        assert device.baseline_cost == pytest.approx(backtest.baseline_cost)
        assert device.potential == pytest.approx(backtest.potential)
        assert device.regret == pytest.approx(backtest.regret)
        assert device.savings == pytest.approx(backtest.savings)
        assert device.capture == pytest.approx(backtest.capture)
        assert device.realized_end_kwh == pytest.approx(backtest.realized_end_kwh)
        assert device.baseline_end_kwh == pytest.approx(backtest.baseline_end_kwh)

    def test_the_oracle_is_a_lower_bound_on_the_committed_day(self) -> None:
        day, actuals, site = _recorded()
        keys = day_slot_keys(day, _TZ, actuals.slot_minutes)

        device = score_slots(
            keys, _slots_from_actuals(actuals, keys), actuals.slot_minutes, site
        )

        assert device.regret is not None and device.regret >= -1e-6
        assert device.potential is not None and device.potential >= -1e-6
        # The meters of a real installation explain themselves.
        assert device.residual_kwh is not None and device.residual_kwh < 0.5

    def test_the_run_ends_where_the_battery_really_was(self) -> None:
        """SoC-derived flows cannot drift away from the measured SoC."""
        day, actuals, site = _recorded()
        keys = day_slot_keys(day, _TZ, actuals.slot_minutes)
        end_soc = actuals._lookup(
            "battery_soc_pct", keys[-1] + timedelta(minutes=actuals.slot_minutes)
        )
        assert end_soc is not None

        device = score_slots(
            keys, _slots_from_actuals(actuals, keys), actuals.slot_minutes, site
        )

        assert device.realized_end_kwh == pytest.approx(site.rated_kwh * end_soc / 100)

    def test_the_two_sources_of_battery_flows_agree_on_real_days(self) -> None:
        """Measured battery energy and SoC-derived flows tell the same story.

        The backtest reads the battery's energy meters by default; the device
        has only the SoC.  On the committed day the two scores differ by a few
        points of capture, not in what they say about the day.
        """
        day, actuals, site = _recorded()

        meters = score_day(actuals, day, _TZ, site)
        soc = score_day(actuals, day, _TZ, site, flows_from_soc=True)

        assert meters.capture is not None and soc.capture is not None
        assert soc.realized_cost == pytest.approx(meters.realized_cost)
        assert soc.capture == pytest.approx(meters.capture, abs=0.10)
        assert soc.regret == pytest.approx(meters.regret, rel=0.25)


class TestBacktestSocMode:
    def test_the_soc_is_needed_at_every_boundary(self) -> None:
        """The committed two days end without the next day's first reading."""
        paths = sorted(
            (Path(__file__).parents[1] / "backtest" / "actuals").glob("*.json")
        )
        actuals = merge_actuals([load_actuals(path) for path in paths])
        _day, _actuals, site = _recorded()

        score = score_day(actuals, date(2026, 9, 15), _TZ, site, flows_from_soc=True)

        assert score.unscorable == "battery_soc_pct missing at 1/97 slot boundaries"

    def test_the_default_still_reads_the_battery_energy_meters(self) -> None:
        day, actuals, site = _recorded()
        stripped = Actuals(
            slot_minutes=actuals.slot_minutes,
            energy_kwh={
                name: values
                for name, values in actuals.energy_kwh.items()
                if not name.startswith("battery_")
            },
            values=actuals.values,
            site_tag=actuals.site_tag,
        )

        assert score_day(stripped, day, _TZ, site).unscorable is not None
        assert score_day(stripped, day, _TZ, site, flows_from_soc=True).is_scored

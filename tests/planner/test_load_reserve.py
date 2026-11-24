"""Pre-peak reserve for load-forecast uncertainty (issue #1361).

The MILP sizes the top-up before an expensive window to the load forecast.
With the reserve on, it may hold more at the end of that window, sized from
how far the forecast has been off over the window's hours on earlier days.
These tests pin what the spec promises:

- off, or without a history, the plan is the plan of ``origin/main``;
- the reserve needs a price asymmetry, enough history and room in the battery;
- the MILP, the SoC simulation and the score agree on what is held;
- the house load in the energy balance never changes, and the baselines
  keep their slots;
- replanning slot by slot does not make the battery change direction more.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES
from custom_components.hsem.coordinator_builder import build_planner_input
from custom_components.hsem.custom_sensors.config_reader import build_sensor_config
from custom_components.hsem.flows.battery_economics import (
    get_battery_economics_step_schema,
    validate_battery_economics_input,
)
from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.load_error_profile import (
    ROBUST_SPREAD_FACTOR,
    LoadErrorProfile,
)
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner.candidate_generator import (
    CANDIDATE_NO_ACTION,
    CANDIDATE_PASSIVE,
)
from custom_components.hsem.planner.cost_function import CostWeights, score_plan
from custom_components.hsem.planner.engine_core import run_planner
from custom_components.hsem.planner.load_reserve import (
    LOAD_RESERVE_MAX_WINDOWS,
    LOAD_RESERVE_MIN_SAMPLES,
    REASON_APPLIED,
    REASON_CAPACITY_BOUND,
    REASON_INSUFFICIENT_HISTORY,
    REASON_NO_HISTORY,
    REASON_NO_PRICE_ASYMMETRY,
    REASON_NO_UNCERTAINTY,
    REASON_SURPLUS_TOO_COSTLY,
    LoadReservePlan,
    LoadReserveSpec,
    delivered_cost_per_kwh,
    load_reserve_penalty,
    reserve_shortfall_kwh,
    reserve_tiers,
    resolve_load_reserve,
    summarize_load_reserve,
    window_after,
)
from custom_components.hsem.planner.milp import _incumbent
from custom_components.hsem.planner.milp._layout import (
    LOAD_RESERVE_SLACK_BLOCK,
    build_milp_column_layout,
)
from custom_components.hsem.planner.milp._load_reserve_rows import (
    LoadReserveRows,
    load_reserve_column_count,
    load_reserve_rows,
    load_reserve_slack_bounds,
)
from custom_components.hsem.planner.milp_optimizer import (
    is_scipy_available,
    solve_milp,
)
from custom_components.hsem.utils.prices import SlotPrice
from tests.backtest.conftest import corpus_paths
from tests.backtest.invariants import check_invariants, format_violations
from tests.backtest.replay import load_planner_input
from tests.planner.fixtures import make_flat_price_input, make_winter_day_input

_TZ = ZoneInfo("Europe/Copenhagen")
_ETA = 0.98
_STEP = 15
#: The synthetic day: a 2.6 valley, 2.7 shoulders and a 3.0-4.0 evening peak.
_DAY = datetime(2026, 11, 3, tzinfo=_TZ)
_NOW = _DAY.replace(hour=11)
_WINDOW_START = _DAY.replace(hour=15, minute=30)
_WINDOW_END = _DAY.replace(hour=21)
_WINDOW_SLOTS = 22
_SIGMA = 1.0

_needs_scipy = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available"
)


def _price(hour: float, next_day: float | None = None) -> float:
    """Return the synthetic day's import price at a clock hour.

    *next_day* is the flat price of the day after, for a slot on that day.
    """
    if next_day is not None:
        return next_day
    if 15.5 <= hour < 21:
        if 17 <= hour < 19.5:
            return 4.0
        return 3.5 if (16.5 <= hour < 17 or 19.5 <= hour < 20) else 3.0
    return 2.6 if 11 <= hour < 14 else 2.7


def _minute(value: datetime) -> int:
    """Return the UTC epoch minute of an aware datetime."""
    return int(value.astimezone(UTC).timestamp() // 60)


def _history(
    sigma: float = _SIGMA,
    *,
    days: int = 14,
    start: datetime = _WINDOW_START,
    slots: int = _WINDOW_SLOTS,
) -> LoadErrorProfile:
    """Return a history whose robust window spread is *sigma*.

    The window sum is ``±sigma / 1.4826`` on alternating days.
    """
    errors: dict[int, float] = {}
    for back in range(1, days + 1):
        sign = 1.0 if back % 2 else -1.0
        for index in range(slots):
            slot = start - timedelta(days=back) + timedelta(minutes=_STEP * index)
            errors[_minute(slot)] = sign * sigma / ROBUST_SPREAD_FACTOR / slots
    return LoadErrorProfile(errors=errors)


def _day_input(
    *,
    now: datetime = _NOW,
    soc_pct: float = 5.0,
    rated_kwh: float = 15.0,
    night_load: float = 1.6,
    next_day_price: float = 2.6,
) -> PlannerInput:
    """Return the synthetic day at 15-minute slots with a flat second day.

    The battery starts empty.  The evening window draws 1.5 kWh per hour and
    the hours after it ``night_load``, which is what a surplus can serve.
    The second day costs ``next_day_price`` in every slot.
    """
    load = [0.4] * 24
    for hour in range(15, 21):
        load[hour] = 1.5
    for hour in range(21, 24):
        load[hour] = night_load
    base = make_winter_day_input(
        now_iso=now.isoformat(),
        battery_soc_pct=soc_pct,
        battery_rated_capacity_kwh=rated_kwh,
        battery_end_of_discharge_soc_pct=5.0,
        interval_minutes=_STEP,
        interval_length_hours=48,
    )
    return replace(
        base,
        time_zone="Europe/Copenhagen",
        battery_charge_efficiency_pct=_ETA * 100.0,
        battery_discharge_efficiency_pct=_ETA * 100.0,
        price_points=[
            PricePoint(
                hour=hour,
                import_price=_price(
                    hour + quarter / 4, next_day_price if offset else None
                ),
                export_price=0.5,
                day_offset=offset,
                slot_in_day=hour * 4 + quarter,
            )
            for offset in (0, 1)
            for hour in range(24)
            for quarter in range(4)
        ],
        solcast_slots=[SolcastSlot(hour=hour, pv_estimate=0.0) for hour in range(24)],
        consumption_averages=[
            HourlyConsumptionAverage(
                hour=hour, avg_1d=kwh, avg_3d=kwh, avg_7d=kwh, avg_14d=kwh
            )
            for hour, kwh in enumerate(load)
        ],
    )


def _with_reserve(inp: PlannerInput, sigma: float = _SIGMA) -> PlannerInput:
    """Return *inp* with the reserve on and a history of spread *sigma*."""
    return replace(inp, load_reserve_enabled=True, load_error_profile=_history(sigma))


def _charged_before(output: PlannerOutput, end: datetime) -> float:
    """Return the energy the plan stores before *end*."""
    return sum(s.batteries_charged_kwh for s in output.slots if s.end <= end)


def _stored_at(output: PlannerOutput, end: datetime) -> float:
    """Return the model energy the plan holds at the end of a slot."""
    return next(s.estimated_battery_capacity_kwh for s in output.slots if s.end == end)


def _slots(prices: list[float], *, load: float = 0.4) -> list[PlannedSlot]:
    """Return 15-minute slots from ``_NOW`` with the given import prices."""
    slots = []
    for index, price in enumerate(prices):
        start = _NOW + timedelta(minutes=_STEP * index)
        slots.append(
            PlannedSlot(
                start=start,
                end=start + timedelta(minutes=_STEP),
                price=SlotPrice(import_price=price, export_price=0.0),
                avg_house_consumption_kwh=load,
            )
        )
    return slots


def _resolve(
    slots: list[PlannedSlot],
    profile: LoadErrorProfile | None,
    *,
    usable_kwh: float = 14.0,
) -> LoadReservePlan | None:
    """Resolve the reserve for *slots* at ``_NOW``."""
    inp = PlannerInput(
        now_iso=_NOW.isoformat(),
        interval_minutes=_STEP,
        battery_charge_efficiency_pct=_ETA * 100.0,
        battery_discharge_efficiency_pct=_ETA * 100.0,
        load_reserve_enabled=True,
        load_error_profile=profile,
    )
    return resolve_load_reserve(
        inp, slots, _NOW, usable_kwh=usable_kwh, cycle_cost_per_kwh=0.02, zone=_TZ
    )


def _peak_prices() -> list[float]:
    """Return four cheap slots, an eight-slot peak and four cheap slots."""
    return [2.0] * 4 + [4.0] * 8 + [2.0] * 4


def _peak_history(sigma: float = 0.8, days: int = 14) -> LoadErrorProfile:
    """Return a history for the peak of :func:`_peak_prices`."""
    return _history(
        sigma, days=days, start=_NOW + timedelta(minutes=_STEP * 4), slots=8
    )


# ---------------------------------------------------------------------------
# Expensive windows
# ---------------------------------------------------------------------------


class TestWindowAfter:
    """The window a charge in one slot would serve."""

    def test_the_window_runs_while_the_price_stays_above_the_delivered_cost(
        self,
    ) -> None:
        """Slots dearer than a delivered kWh from slot 0 form the window."""
        found = window_after([2.0, 2.0, 3.0, 3.5, 3.0, 2.0, 3.0], 0, _ETA, _ETA, 0.02)

        assert found is not None
        first, last, cost = found
        assert (first, last) == (2, 4)
        assert cost == pytest.approx(delivered_cost_per_kwh(2.0, _ETA, _ETA, 0.02))
        assert cost == pytest.approx((2.0 / _ETA + 0.04) / _ETA)

    def test_a_cheaper_slot_before_the_peak_takes_over(self) -> None:
        """The cheaper slot is the place to charge, not this one."""
        assert window_after([2.5, 2.0, 4.0], 0, _ETA, _ETA, 0.0) is None
        assert window_after([2.5, 2.0, 4.0], 1, _ETA, _ETA, 0.0) is not None

    def test_a_shoulder_below_the_delivered_cost_is_not_in_the_window(self) -> None:
        """A price just above the charge price does not pay for the losses."""
        assert window_after([2.0, 2.05, 2.05], 0, _ETA, _ETA, 0.0) is None

    def test_a_slot_without_a_price_ends_the_window(self) -> None:
        """A missing price is neither dear nor a place to charge."""
        nan = float("nan")
        assert window_after([2.0, 4.0, nan, 4.0], 0, _ETA, _ETA, 0.0) == (
            1,
            1,
            pytest.approx(2.0 / _ETA / _ETA),
        )
        assert window_after([nan, 4.0], 0, _ETA, _ETA, 0.0) is None

    def test_the_last_slot_has_no_window(self) -> None:
        """Nothing comes after the last slot."""
        assert window_after([2.0, 4.0], 1, _ETA, _ETA, 0.0) is None


class TestReserveTiers:
    """The reserve cut into priced tiers."""

    def test_four_tiers_of_half_a_sigma_cheapest_first(self) -> None:
        """The top of the reserve is worth least and is given up first."""
        tiers = reserve_tiers(1.0, 10.0, 1.0)

        assert [width for width, _ in tiers] == pytest.approx([0.5] * 4)
        prices = [price for _, price in tiers]
        assert prices == sorted(prices)
        # 1 − Φ(z) at z = 1.75, 1.25, 0.75 and 0.25.
        assert prices == pytest.approx([0.0401, 0.1056, 0.2266, 0.4013], abs=1e-4)

    def test_the_price_scales_with_the_shortfall_cost(self) -> None:
        """Twice the shortfall cost is twice the price of every tier."""
        single = reserve_tiers(1.0, 10.0, 1.0)
        double = reserve_tiers(1.0, 10.0, 2.0)

        assert [p for _, p in double] == pytest.approx([2 * p for _, p in single])

    def test_the_cap_cuts_the_top_of_the_reserve(self) -> None:
        """What the battery cannot hold is not asked for."""
        tiers = reserve_tiers(1.0, 0.7, 1.0)

        assert [width for width, _ in tiers] == pytest.approx([0.2, 0.5])
        assert sum(width for width, _ in tiers) == pytest.approx(0.7)

    def test_no_spread_no_tier(self) -> None:
        """A forecast that was never off needs no reserve."""
        assert reserve_tiers(0.0, 10.0, 1.0) == ()


class TestSlackCost:
    """What a plan pays for holding less than the full reserve."""

    def test_tiers_are_given_up_cheapest_first(self) -> None:
        """The first kWh of slack costs the cheapest tier's price."""
        spec = LoadReserveSpec(REASON_APPLIED, tiers=((0.5, 0.1), (0.5, 0.4)))

        assert spec.max_reserve_kwh == pytest.approx(1.0)
        assert spec.slack_cost(0.0) == pytest.approx(0.0)
        assert spec.slack_cost(-1.0) == pytest.approx(0.0)
        assert spec.slack_cost(0.3) == pytest.approx(0.03)
        assert spec.slack_cost(0.8) == pytest.approx(0.05 + 0.3 * 0.4)

    def test_slack_beyond_the_reserve_costs_the_dearest_tier(self) -> None:
        """As in the LP, whose dearest slack column is unbounded."""
        spec = LoadReserveSpec(REASON_APPLIED, tiers=((0.5, 0.1), (0.5, 0.4)))

        assert spec.slack_cost(1.5) == pytest.approx(0.05 + 0.2 + 0.5 * 0.4)

    def test_no_tier_no_cost(self) -> None:
        """A decision without a reserve prices nothing."""
        assert LoadReserveSpec(REASON_NO_HISTORY).slack_cost(2.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


class TestResolve:
    """When a reserve is asked for, and why not."""

    def test_off_returns_before_touching_its_inputs(self) -> None:
        """``slots=None`` would raise if anything were read from it."""
        inp = PlannerInput(now_iso=_NOW.isoformat(), load_error_profile=_history())

        assert (
            resolve_load_reserve(
                inp,
                None,  # type: ignore[arg-type]  # must not be read
                _NOW,
                usable_kwh=10.0,
                cycle_cost_per_kwh=0.0,
            )
            is None
        )

    @pytest.mark.parametrize("profile", [None, LoadErrorProfile()])
    def test_without_a_history_there_is_no_reserve(
        self, profile: LoadErrorProfile | None
    ) -> None:
        """A fresh install has nothing to size a reserve from."""
        plan = _resolve(_slots(_peak_prices()), profile)

        assert plan is not None
        assert plan.windows == ()
        assert plan.primary.reason == REASON_NO_HISTORY

    def test_flat_prices_have_no_window(self) -> None:
        """No slot is dearer than a charge before it."""
        plan = _resolve(_slots([2.0] * 16), _peak_history())

        assert plan is not None
        assert plan.windows == ()
        assert plan.primary.reason == REASON_NO_PRICE_ASYMMETRY
        assert plan.primary.window_start is None

    def test_a_peak_barely_above_the_delivered_cost_gets_no_reserve(self) -> None:
        """A shortfall must cost at least 5 % of the replacement cost."""
        cost = delivered_cost_per_kwh(2.0, _ETA, _ETA, 0.02)
        prices = [2.0] * 4 + [cost * 1.02] * 8 + [2.0] * 4

        plan = _resolve(_slots(prices), _peak_history())

        assert plan is not None
        assert plan.windows == ()
        assert plan.primary.reason == REASON_NO_PRICE_ASYMMETRY
        assert plan.primary.shortfall_cost == pytest.approx(cost * 0.02)

    def test_too_few_earlier_days_give_no_reserve(self) -> None:
        """Six complete days are one short."""
        plan = _resolve(
            _slots(_peak_prices()), _peak_history(days=LOAD_RESERVE_MIN_SAMPLES - 1)
        )

        assert plan is not None
        assert plan.windows == ()
        assert plan.primary.reason == REASON_INSUFFICIENT_HISTORY
        assert plan.primary.samples == LOAD_RESERVE_MIN_SAMPLES - 1

    def test_a_forecast_that_was_never_off_gives_no_reserve(self) -> None:
        """The reserve vanishes with the spread."""
        plan = _resolve(_slots(_peak_prices()), _peak_history(sigma=0.0))

        assert plan is not None
        assert plan.windows == ()
        assert plan.primary.reason == REASON_NO_UNCERTAINTY
        assert plan.primary.sigma_kwh == pytest.approx(0.0)

    def test_one_evening_far_off_does_not_set_the_spread(self) -> None:
        """+40 kWh on one of 14 evenings: the reserve keeps its size."""
        profile = _peak_history()
        errors = dict(profile.errors)
        day = _NOW + timedelta(minutes=_STEP * 4) - timedelta(days=5)
        errors[_minute(day)] += 40.0

        plan = _resolve(_slots(_peak_prices()), LoadErrorProfile(errors=errors))

        assert plan is not None
        assert plan.primary.reason == REASON_APPLIED
        assert plan.primary.sigma_kwh == pytest.approx(0.8)
        assert plan.primary.max_reserve_kwh == pytest.approx(1.6)

    def test_a_battery_the_window_empties_has_no_room(self) -> None:
        """The window's own demand already fills the battery."""
        plan = _resolve(_slots(_peak_prices()), _peak_history(), usable_kwh=3.2)

        assert plan is not None
        assert plan.windows == ()
        assert plan.primary.reason == REASON_CAPACITY_BOUND

    def test_the_reserve_is_cut_to_the_room_in_the_battery(self) -> None:
        """Capacity minus the window's demand on the battery side."""
        demand = 8 * 0.4 / _ETA
        plan = _resolve(_slots(_peak_prices()), _peak_history(), usable_kwh=demand + 1)

        assert plan is not None
        assert plan.primary.reason == REASON_APPLIED
        assert plan.primary.max_reserve_kwh == pytest.approx(1.0)

    def test_an_applied_reserve_describes_its_window(self) -> None:
        """Window, spread, both costs and the level come out."""
        slots = _slots(_peak_prices())

        plan = _resolve(slots, _peak_history())

        assert plan is not None
        spec = plan.primary
        assert plan.windows == (spec,)
        assert spec.active
        assert spec.window_start == slots[4].start
        assert spec.window_end == slots[11].end
        assert spec.window_slots == 8
        assert spec.samples == 14
        assert spec.sigma_kwh == pytest.approx(0.8)
        cost = delivered_cost_per_kwh(2.0, _ETA, _ETA, 0.02)
        assert spec.replacement_cost == pytest.approx(cost)
        assert spec.shortfall_cost == pytest.approx(4.0 - cost)
        assert spec.max_reserve_kwh == pytest.approx(1.6)
        assert spec.level_kwh == pytest.approx(1.6)
        assert spec.as_dict()["window_end"] == slots[11].end.isoformat()

    def test_the_dynamic_floor_sits_under_the_reserve(self) -> None:
        """The reserve is held on top of what the floor already asks."""
        slots = _slots(_peak_prices())
        slots[11].discharge_reserve_kwh = 0.75

        plan = _resolve(slots, _peak_history())

        assert plan is not None
        assert plan.primary.floor_kwh == pytest.approx(0.75)
        assert plan.primary.level_kwh == pytest.approx(0.75 + 1.6)

    def test_the_shortfall_price_is_the_cheapest_of_the_last_two_hours(self) -> None:
        """A shortfall is bought in the cheapest slot of the window's tail."""
        prices = [2.0] * 2 + [5.0] * 4 + [3.6] * 4 + [3.0] * 2 + [3.4] * 2 + [2.0] * 2
        profile = _history(0.8, start=_NOW + timedelta(minutes=_STEP * 2), slots=12)

        plan = _resolve(_slots(prices), profile)

        assert plan is not None
        cost = delivered_cost_per_kwh(2.0, _ETA, _ETA, 0.02)
        assert plan.primary.window_slots == 12
        assert plan.primary.shortfall_cost == pytest.approx(3.0 - cost)

    def test_a_window_past_the_lookahead_is_left_alone(self) -> None:
        """Only a window that starts within 24 hours gets a reserve."""
        prices = [2.0] * 100 + [4.0] * 8 + [2.0] * 4

        plan = _resolve(_slots(prices), _history(0.8, slots=120, start=_NOW))

        assert plan is not None
        assert plan.windows == ()
        assert plan.primary.reason == REASON_NO_PRICE_ASYMMETRY

    def test_a_window_without_room_is_searched_for_one_inside_it(self) -> None:
        """A long window the battery cannot bridge leaves its own peak."""
        prices = [2.0] * 2 + [2.6] * 20 + [4.0] * 4 + [2.6] * 4 + [2.0] * 2
        profile = _history(0.6, start=_NOW, slots=len(prices))

        plan = _resolve(_slots(prices), profile, usable_kwh=6.0)

        assert plan is not None
        assert len(plan.windows) == 1
        spec = plan.windows[0]
        assert spec.window_slots == 4
        assert spec.replacement_cost == pytest.approx(
            delivered_cost_per_kwh(2.6, _ETA, _ETA, 0.02)
        )

    def test_at_most_three_reserves(self) -> None:
        """Later peaks are picked up by a later run."""
        prices = ([2.0] * 2 + [4.0] * 2) * 5 + [2.0] * 2
        profile = _history(0.4, start=_NOW, slots=len(prices))

        plan = _resolve(_slots(prices), profile)

        assert plan is not None
        assert len(plan.windows) == LOAD_RESERVE_MAX_WINDOWS
        assert [s.window_start for s in plan.windows] == sorted(
            s.window_start for s in plan.windows if s.window_start is not None
        )


class TestScoreAndSummary:
    """The selector's term and the published diagnostics."""

    def _spec(self, slots: list[PlannedSlot]) -> LoadReserveSpec:
        return LoadReserveSpec(
            REASON_APPLIED,
            sigma_kwh=0.5,
            samples=14,
            shortfall_cost=1.0,
            window_start=slots[1].start,
            window_end=slots[2].end,
            window_slots=2,
            tiers=((0.5, 0.1), (0.5, 0.4)),
        )

    def test_a_plan_at_the_level_pays_nothing(self) -> None:
        """Holding the full reserve leaves no slack."""
        slots = _slots([2.0, 4.0, 4.0, 2.0])
        slots[2].estimated_battery_capacity_kwh = 1.0
        spec = self._spec(slots)

        assert reserve_shortfall_kwh(slots, spec) == pytest.approx(0.0)
        assert load_reserve_penalty(slots, LoadReservePlan(spec, (spec,))) == (
            pytest.approx(0.0)
        )

    def test_a_plan_below_the_level_pays_the_tiers_it_gives_up(self) -> None:
        """The score carries the same slack cost as the LP."""
        slots = _slots([2.0, 4.0, 4.0, 2.0])
        slots[2].estimated_battery_capacity_kwh = 0.3
        spec = self._spec(slots)

        assert reserve_shortfall_kwh(slots, spec) == pytest.approx(0.7)
        assert load_reserve_penalty(slots, LoadReservePlan(spec, (spec,))) == (
            pytest.approx(0.5 * 0.1 + 0.2 * 0.4)
        )

    def test_a_shortfall_below_the_slot_resolution_is_ignored(self) -> None:
        """Stored energy is published with three decimals."""
        slots = _slots([2.0, 4.0, 4.0, 2.0])
        slots[2].estimated_battery_capacity_kwh = 0.9995
        spec = self._spec(slots)

        assert reserve_shortfall_kwh(slots, spec) == pytest.approx(0.0)

    def test_no_plan_and_a_missing_slot_pay_nothing(self) -> None:
        """Off, and a window the slot list does not reach."""
        slots = _slots([2.0, 4.0, 4.0, 2.0])
        spec = self._spec(slots)

        assert load_reserve_penalty(slots, None) == pytest.approx(0.0)
        assert reserve_shortfall_kwh(slots[:2], spec) == pytest.approx(0.0)
        assert reserve_shortfall_kwh(
            slots, LoadReserveSpec(REASON_APPLIED, tiers=((0.5, 0.1),))
        ) == pytest.approx(0.0)

    def test_the_score_gains_the_penalty_and_the_cost_does_not(self) -> None:
        """Selector-only: money stays money."""
        slots = _slots([2.0, 4.0, 4.0, 2.0])
        slots[2].estimated_battery_capacity_kwh = 0.3
        spec = self._spec(slots)
        plain = score_plan(slots, CostWeights(time_discount_rate=1.0))
        weights = CostWeights(time_discount_rate=1.0)
        weights.load_reserve = LoadReservePlan(spec, (spec,))

        scored = score_plan(slots, weights)

        assert scored.load_reserve_penalty == pytest.approx(0.13)
        assert scored.total_cost == pytest.approx(plain.total_cost)
        assert scored.score == pytest.approx(plain.score + 0.13)

    def test_the_discounted_score_gains_the_same_penalty(self) -> None:
        """The slack is undiscounted, as in the LP."""
        slots = _slots([2.0, 4.0, 4.0, 2.0])
        slots[2].estimated_battery_capacity_kwh = 0.3
        spec = self._spec(slots)
        plain = score_plan(slots, CostWeights(time_discount_rate=0.99), now=_NOW)
        weights = CostWeights(time_discount_rate=0.99)
        weights.load_reserve = LoadReservePlan(spec, (spec,))

        scored = score_plan(slots, weights, now=_NOW)

        assert scored.score == pytest.approx(plain.score + 0.13)

    def test_the_summary_says_what_the_plan_holds(self) -> None:
        """Reserve, spread, safety factor and slack per window."""
        slots = _slots([2.0, 4.0, 4.0, 2.0])
        slots[2].estimated_battery_capacity_kwh = 0.75
        spec = self._spec(slots)

        summary = summarize_load_reserve(LoadReservePlan(spec, (spec,)), slots)

        assert summary is not None
        assert summary["reason"] == REASON_APPLIED
        assert summary["stock_at_window_end_kwh"] == pytest.approx(0.75)
        assert summary["slack_kwh"] == pytest.approx(0.25)
        assert summary["sigma_kwh"] == pytest.approx(0.5)
        assert summary["safety_factor"] == pytest.approx(1.5)
        assert summary["total_stock_kwh"] == pytest.approx(0.75)
        assert summary["windows"] == [
            {
                k: v
                for k, v in summary.items()
                if k not in {"windows", "total_stock_kwh"}
            }
        ]

    def test_a_reserve_the_plan_does_not_hold_says_why(self) -> None:
        """Carrying the surplus cost more than the reserve is worth."""
        slots = _slots([2.0, 4.0, 4.0, 2.0])
        spec = self._spec(slots)

        summary = summarize_load_reserve(LoadReservePlan(spec, (spec,)), slots)

        assert summary is not None
        assert summary["reason"] == REASON_SURPLUS_TOO_COSTLY
        assert summary["stock_at_window_end_kwh"] == pytest.approx(0.0)
        assert summary["slack_kwh"] == pytest.approx(1.0)

    def test_the_first_held_reserve_leads_the_summary(self) -> None:
        """A window the plan holds nothing for does not hide a later one."""
        slots = _slots([2.0, 4.0, 4.0, 2.0])
        slots[3].estimated_battery_capacity_kwh = 1.0
        empty = self._spec(slots)
        held = replace(empty, window_end=slots[3].end)

        summary = summarize_load_reserve(LoadReservePlan(empty, (empty, held)), slots)

        assert summary is not None
        assert summary["reason"] == REASON_APPLIED
        assert summary["window_end"] == slots[3].end.isoformat()
        assert [w["reason"] for w in summary["windows"]] == [
            REASON_SURPLUS_TOO_COSTLY,
            REASON_APPLIED,
        ]

    def test_no_reserve_reports_the_reason_and_off_reports_nothing(self) -> None:
        """The reason is visible when none is applied."""
        plan = LoadReservePlan(LoadReserveSpec(REASON_INSUFFICIENT_HISTORY, samples=3))

        summary = summarize_load_reserve(plan, [])

        assert summary is not None
        assert summary["reason"] == REASON_INSUFFICIENT_HISTORY
        assert summary["samples"] == 3
        assert summary["stock_at_window_end_kwh"] == pytest.approx(0.0)
        assert summary["safety_factor"] == pytest.approx(0.0)
        assert summary["windows"] == []
        assert summarize_load_reserve(None, []) is None


# ---------------------------------------------------------------------------
# The LP rows
# ---------------------------------------------------------------------------


class TestRows:
    """One soft row per reserve, one slack column per tier."""

    def test_rows_follow_the_applied_reserves(self) -> None:
        """The row sits at the LP index of the window's last slot."""
        slots = _slots([2.0, 4.0, 4.0, 2.0])
        spec = LoadReserveSpec(
            REASON_APPLIED, window_end=slots[2].end, tiers=((0.5, 0.1), (0.5, 0.4))
        )
        beyond = replace(spec, window_end=slots[3].end + timedelta(hours=1))
        plan = LoadReservePlan(spec, (spec, beyond, LoadReserveSpec(REASON_APPLIED)))

        rows = load_reserve_rows(plan, slots, [1, 2, 3])

        assert rows == (LoadReserveRows(1, 1.0, spec.tiers),)
        assert load_reserve_rows(None, slots, [1, 2, 3]) == ()
        assert load_reserve_column_count(rows) == 2

    def test_the_dearest_slack_column_is_unbounded(self) -> None:
        """The row stays feasible whatever the battery holds."""
        rows = (
            LoadReserveRows(1, 1.0, ((0.5, 0.1), (0.5, 0.4))),
            LoadReserveRows(3, 0.4, ((0.4, 0.2),)),
        )

        assert load_reserve_slack_bounds(rows) == [
            (0.0, 0.5),
            (0.0, None),
            (0.0, None),
        ]

    def test_the_layout_declares_one_column_per_tier(self) -> None:
        """Off, the layout is the layout without a reserve."""
        plain = build_milp_column_layout(4, 0, fuse_active=False)
        wide = build_milp_column_layout(4, 0, fuse_active=False, load_reserve=3)

        assert not plain.has(LOAD_RESERVE_SLACK_BLOCK)
        assert wide.width(LOAD_RESERVE_SLACK_BLOCK) == 3
        assert wide.column_count == plain.column_count + 3

    @_needs_scipy
    def test_the_solver_holds_the_tiers_worth_their_carrying_cost(self) -> None:
        """A dear tier is held, a tier worth less than a round trip is not."""
        prices = [2.0] * 4 + [4.0] * 4 + [1.0] * 4
        slots = _slots(prices, load=0.5)
        for slot in slots:
            slot.estimated_net_consumption_kwh = 0.5
        spec = LoadReserveSpec(
            REASON_APPLIED,
            window_end=slots[7].end,
            tiers=((0.5, 0.01), (0.5, 5.0)),
        )
        kwargs: dict[str, Any] = {
            "current_kwh": 0.0,
            "usable_kwh": 10.0,
            "max_charge_per_slot": 1.0,
            "max_discharge_per_slot": 1.0,
            "charge_efficiency_pct": 100.0,
            "discharge_efficiency_pct": 100.0,
            "replacement_price_per_kwh": 0.0,
        }

        plain = solve_milp(slots, _NOW, **kwargs)
        held = solve_milp(
            slots, _NOW, load_reserve=LoadReservePlan(spec, (spec,)), **kwargs
        )

        assert plain is not None
        assert held is not None

        def stored(result: tuple[list[PlannedSlot], dict[str, Any]]) -> float:
            return sum(
                s.batteries_charged_kwh - s.batteries_discharged_kwh
                for s in result[0][:8]
            )

        assert stored(plain) == pytest.approx(0.0, abs=1e-6)
        assert stored(held) == pytest.approx(0.5, abs=1e-6)


# ---------------------------------------------------------------------------
# Through the engine
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def off() -> PlannerOutput:
    """Return the synthetic day planned without the reserve."""
    return run_planner(_day_input())


@pytest.fixture(scope="module")
def on() -> PlannerOutput:
    """Return the synthetic day planned with the reserve."""
    return run_planner(_with_reserve(_day_input()))


@_needs_scipy
class TestSyntheticDay:
    """A 3.0-4.0 peak after a 2.6 valley, with a spread of 1 kWh."""

    def test_without_the_reserve_the_plan_ends_the_window_empty(
        self, off: PlannerOutput
    ) -> None:
        """The top-up is sized to the forecast."""
        assert off.load_reserve is None
        assert off.explanation.load_reserve is None
        assert _stored_at(off, _WINDOW_END) == pytest.approx(0.0, abs=2e-3)

    def test_the_plan_charges_the_reserve_before_the_peak(
        self, off: PlannerOutput, on: PlannerOutput
    ) -> None:
        """``z × σ`` more is stored before the window than without it."""
        reserve = on.load_reserve
        assert reserve is not None
        assert reserve["reason"] == REASON_APPLIED
        assert reserve["window_start"] == _WINDOW_START.isoformat()
        assert reserve["window_end"] == _WINDOW_END.isoformat()
        assert reserve["window_slots"] == _WINDOW_SLOTS
        assert reserve["sigma_kwh"] == pytest.approx(_SIGMA)
        assert reserve["samples"] == 14
        # Two of four tiers are worth more than carrying them to the
        # 2.7 hours after the window: z = 1.0.
        assert reserve["asked_kwh"] == pytest.approx(2.0 * _SIGMA, abs=2e-3)
        assert reserve["safety_factor"] == pytest.approx(1.0, abs=0.02)
        assert reserve["stock_at_window_end_kwh"] == pytest.approx(_SIGMA, abs=0.02)
        assert reserve["slack_kwh"] == pytest.approx(_SIGMA, abs=0.02)
        extra = _charged_before(on, _WINDOW_START) - _charged_before(off, _WINDOW_START)
        assert extra == pytest.approx(reserve["stock_at_window_end_kwh"], abs=0.02)
        assert _stored_at(on, _WINDOW_END) == pytest.approx(
            reserve["stock_at_window_end_kwh"], abs=2e-3
        )

    def test_the_reserve_never_exceeds_the_room_in_the_battery(
        self, on: PlannerOutput
    ) -> None:
        """``usable − Σ base_load / η_d`` bounds the extra charge."""
        reserve = on.load_reserve
        assert reserve is not None
        usable = 15.0 * 0.95
        demand = sum(
            s.avg_house_consumption_kwh
            for s in on.slots
            if _WINDOW_START <= s.start < _WINDOW_END
        )
        assert reserve["asked_kwh"] <= usable - demand / _ETA + 1e-9

    def test_the_house_load_and_the_energy_balance_are_untouched(
        self, off: PlannerOutput, on: PlannerOutput
    ) -> None:
        """The reserve is stock, not phantom load."""
        for plain, held in zip(off.slots, on.slots, strict=True):
            assert held.avg_house_consumption_kwh == pytest.approx(
                plain.avg_house_consumption_kwh
            )
        violations = check_invariants(_with_reserve(_day_input()), on)
        assert not violations, format_violations(violations)

    def test_the_score_carries_the_slack_and_the_cost_does_not(
        self, off: PlannerOutput, on: PlannerOutput
    ) -> None:
        """``winner.score == output.score``, with the selector-only term."""
        assert on.plan_cost is not None and off.plan_cost is not None
        winner = next(c for c in on.candidates if c.name == on.winner_name)
        assert winner._cost is not None
        assert winner._cost.score == pytest.approx(on.plan_cost.score)
        assert winner._cost.total_cost == pytest.approx(on.plan_cost.total_cost)
        assert on.plan_cost.load_reserve_penalty > 0.0
        assert on.plan_cost.total_cost == pytest.approx(
            on.plan_cost.import_cost
            - on.plan_cost.export_revenue
            + on.plan_cost.cycle_cost
        )
        # Holding 1.5 kWh more costs money in the forecast world.
        assert on.plan_cost.total_cost > off.plan_cost.total_cost
        assert off.plan_cost.load_reserve_penalty == pytest.approx(0.0)

    def test_the_baselines_keep_their_slots(
        self, off: PlannerOutput, on: PlannerOutput
    ) -> None:
        """Only the MILP candidate holds a reserve."""
        for name in (CANDIDATE_NO_ACTION, CANDIDATE_PASSIVE):
            plain = next(c for c in off.candidates if c.name == name)
            held = next(c for c in on.candidates if c.name == name)
            assert plain._cost is not None and held._cost is not None
            assert held._cost.total_cost == pytest.approx(plain._cost.total_cost)
            for a, b in zip(plain.slots, held.slots, strict=True):
                assert b.recommendation == a.recommendation
                assert b.batteries_charged_kwh == pytest.approx(a.batteries_charged_kwh)
                assert b.batteries_discharged_kwh == pytest.approx(
                    a.batteries_discharged_kwh
                )
                assert b.grid_import_kwh == pytest.approx(a.grid_import_kwh)

    def test_the_explanation_shows_the_reserve(self, on: PlannerOutput) -> None:
        """The Plan Strategy sensor reads it from here."""
        assert on.explanation.load_reserve == on.load_reserve
        assert on.explanation.as_dict()["load_reserve"] == on.load_reserve

    def test_no_spread_plans_as_without_the_reserve(self, off: PlannerOutput) -> None:
        """σ = 0: the same slots and the same cost."""
        flat = run_planner(_with_reserve(_day_input(), sigma=0.0))

        assert flat.load_reserve is not None
        assert flat.load_reserve["reason"] == REASON_NO_UNCERTAINTY
        _assert_same_plan(flat, off)

    def test_a_surplus_with_no_use_is_not_bought(self, off: PlannerOutput) -> None:
        """No load after the window and a cheap day after it: not worth it."""
        idle = _day_input(night_load=0.0, next_day_price=1.0)
        plain = run_planner(idle)
        held = run_planner(_with_reserve(idle))

        assert held.load_reserve is not None
        assert held.load_reserve["reason"] == REASON_SURPLUS_TOO_COSTLY
        assert held.load_reserve["stock_at_window_end_kwh"] == pytest.approx(
            0.0, abs=2e-3
        )
        assert _charged_before(held, _WINDOW_START) == pytest.approx(
            _charged_before(plain, _WINDOW_START), abs=2e-3
        )

    def test_a_battery_the_window_fills_gets_no_reserve(self) -> None:
        """Capacity-bound: the window's demand is the battery's capacity."""
        small = _day_input(rated_kwh=8.25 / _ETA / 0.95)
        plain = run_planner(small)
        held = run_planner(_with_reserve(small))

        assert held.load_reserve is not None
        assert held.load_reserve["reason"] != REASON_APPLIED or (
            held.load_reserve["window_end"] != _WINDOW_END.isoformat()
        )
        assert _charged_before(held, _WINDOW_START) == pytest.approx(
            _charged_before(plain, _WINDOW_START), abs=2e-3
        )


def _core_price(hour: float) -> float:
    """Return a day whose peak falls from 4.0 to 3.0 before the window ends."""
    if 15.5 <= hour < 21:
        if 17 <= hour < 19.5:
            return 4.0
        if hour < 16.5:
            return 3.3
        return 3.5 if hour < 20 else 3.0
    return 2.6 if 11 <= hour < 14 else 2.7


@_needs_scipy
def test_a_reserve_is_never_met_by_importing_inside_its_own_window() -> None:
    """Charging before the window is maxed out: the reserve is not re-timed.

    At 1.5 kW the valley and the shoulders cannot store more than the window
    needs.  The row could still be met by importing at 3.0 in the window's
    last hour and discharging at 2.7 after it, which costs money and leaves
    the stock through the 4.0 core unchanged.  The slack is valued at the
    cheapest price of the window's tail, so that never pays.
    """
    limited = replace(
        _day_input(soc_pct=5.0 + 1.9 / 15.0 * 100.0),
        battery_max_charge_power_w=1500.0,
    )
    limited = replace(
        limited,
        price_points=[
            PricePoint(
                hour=hour,
                import_price=_core_price(hour + quarter / 4) if not offset else 2.6,
                export_price=0.5,
                day_offset=offset,
                slot_in_day=hour * 4 + quarter,
            )
            for offset in (0, 1)
            for hour in range(24)
            for quarter in range(4)
        ],
    )
    core_start = _DAY.replace(hour=17)

    plain = run_planner(limited)
    held = run_planner(_with_reserve(limited))

    assert plain.plan_cost is not None and held.plan_cost is not None
    assert held.load_reserve is not None
    assert held.load_reserve["windows"], "a reserve was asked for"
    extra_cost = held.plan_cost.total_cost - plain.plan_cost.total_cost
    extra_core_stock = _stored_at(held, core_start) - _stored_at(plain, core_start)
    # The plan may only cost more when the stock at the dear core rose.
    assert extra_cost <= 1e-6 or extra_core_stock > 1e-3
    assert extra_cost == pytest.approx(0.0, abs=1e-6)
    for a, b in zip(plain.slots, held.slots, strict=True):
        if _WINDOW_START <= a.start < _WINDOW_END:
            assert b.grid_import_kwh == pytest.approx(a.grid_import_kwh, abs=1e-6)


@_needs_scipy
def test_the_score_penalty_is_the_slack_cost_of_the_solve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The selector's term equals the LP's slack cost to 2e-3.

    The score reads the stock from the simulated slots, which carry three
    decimals; the LP knows it exactly.
    """
    captured: dict[str, float] = {}
    original = _incumbent.solve_and_validate

    def solve(linprog: Any, **kwargs: Any) -> Any:
        result = original(linprog, **kwargs)
        blocks = kwargs["variable_blocks"]
        if result is not None and LOAD_RESERVE_SLACK_BLOCK in blocks:
            offset, width = blocks[LOAD_RESERVE_SLACK_BLOCK]
            columns = slice(offset, offset + width)
            captured["slack_cost"] = float(
                (kwargs["c_obj"][columns] * result.x[columns]).sum()
            )
        return result

    monkeypatch.setattr(_incumbent, "solve_and_validate", solve)

    output = run_planner(_with_reserve(_day_input()))

    assert output.plan_cost is not None
    assert output.plan_cost.load_reserve_penalty > 0.0
    assert output.plan_cost.load_reserve_penalty == pytest.approx(
        captured["slack_cost"], abs=2e-3
    )


def _assert_same_plan(output: PlannerOutput, reference: PlannerOutput) -> None:
    """Assert both plans have the same cost and the same slots."""
    assert output.plan_cost is not None and reference.plan_cost is not None
    assert output.winner_name == reference.winner_name
    assert output.plan_cost.total_cost == pytest.approx(
        reference.plan_cost.total_cost, abs=1e-6
    )
    assert output.plan_cost.score == pytest.approx(reference.plan_cost.score, abs=1e-6)
    for slot, other in zip(output.slots, reference.slots, strict=True):
        assert slot.recommendation == other.recommendation, slot.start
        assert slot.batteries_charged_kwh == pytest.approx(
            other.batteries_charged_kwh, abs=1e-6
        )
        assert slot.batteries_discharged_kwh == pytest.approx(
            other.batteries_discharged_kwh, abs=1e-6
        )
        assert slot.grid_import_kwh == pytest.approx(other.grid_import_kwh, abs=1e-6)


# ---------------------------------------------------------------------------
# Replanning slot by slot
# ---------------------------------------------------------------------------


def _closed_loop(enabled: bool) -> tuple[list[str], list[dict[str, Any] | None]]:
    """Replan every slot from 11:00 to 21:00 and execute the live slot.

    The load is the forecast, so nothing but the planner can make the
    battery change direction.

    Returns:
        The executed battery direction per slot, and each run's reserve.
    """
    soc = 5.0
    directions: list[str] = []
    reserves: list[dict[str, Any] | None] = []
    now = _NOW
    while now < _WINDOW_END:
        inp = _day_input(now=now, soc_pct=soc)
        output = run_planner(_with_reserve(inp) if enabled else inp)
        live = next(s for s in output.slots if s.start == now)
        if live.batteries_charged_kwh > 1e-3:
            directions.append("charge")
        elif live.batteries_discharged_kwh > 1e-3:
            directions.append("discharge")
        else:
            directions.append("idle")
        reserves.append(output.load_reserve)
        soc = live.estimated_battery_soc_pct
        now += timedelta(minutes=_STEP)
    return directions, reserves


def _reversals(directions: list[str]) -> int:
    """Return how often the battery turns from charge to discharge or back."""
    moves = [d for d in directions if d != "idle"]
    return sum(1 for a, b in zip(moves, moves[1:]) if a != b)


_Loop = tuple[list[str], list[dict[str, Any] | None]]


@pytest.fixture(scope="module")
def loop_off() -> _Loop:
    """Return the slot-by-slot replay without the reserve."""
    return _closed_loop(False)


@pytest.fixture(scope="module")
def loop_on() -> _Loop:
    """Return the slot-by-slot replay with the reserve."""
    return _closed_loop(True)


@_needs_scipy
class TestRollingReplan:
    """Forty replans through the synthetic day."""

    def test_the_reserve_adds_no_charge_discharge_reversal(
        self,
        loop_off: _Loop,
        loop_on: _Loop,
    ) -> None:
        """One turn from charging to discharging, with and without it."""
        assert _reversals(loop_off[0]) == 1
        assert _reversals(loop_on[0]) == _reversals(loop_off[0])

    def test_the_spread_and_the_window_do_not_move_before_the_window(
        self, loop_on: _Loop
    ) -> None:
        """While a valley slot is ahead, every run sizes the same reserve."""
        valley = [r for r in loop_on[1][: 3 * 4] if r is not None]

        assert len(valley) == 12
        assert {r["window_start"] for r in valley} == {_WINDOW_START.isoformat()}
        assert {r["window_end"] for r in valley} == {_WINDOW_END.isoformat()}
        assert {r["sigma_kwh"] for r in valley} == {_SIGMA}
        assert {r["asked_kwh"] for r in valley} == {2.0 * _SIGMA}
        held = [r["stock_at_window_end_kwh"] for r in valley]
        assert max(held) - min(held) < 0.01

    def test_the_reserve_is_still_there_when_the_window_starts(
        self,
        loop_off: _Loop,
        loop_on: _Loop,
    ) -> None:
        """Nothing discharges it between the valley and the peak."""
        before_window = (_WINDOW_START - _NOW) // timedelta(minutes=_STEP)

        assert "discharge" not in loop_on[0][:before_window]
        assert loop_on[0][:before_window].count("charge") > loop_off[0][
            :before_window
        ].count("charge")


# ---------------------------------------------------------------------------
# The off path is the planner of origin/main
# ---------------------------------------------------------------------------

_GOLDEN = json.loads(
    (
        Path(__file__).parent.parent / "fixtures" / "load_reserve_off_golden.json"
    ).read_text()
)


@_needs_scipy
@pytest.mark.parametrize("name", sorted(_GOLDEN["cycles"]))
@pytest.mark.parametrize("enabled_without_history", [False, True])
def test_off_path_equals_the_plan_recorded_from_main(
    name: str, enabled_without_history: bool
) -> None:
    """The cost and every slot match what ``origin/main`` planned.

    The fixture was recorded from ``origin/main`` before this feature, from
    the same corpus cycles.  With the reserve off, and with it on but without
    a load error history (a fresh tracker), the plan must be that plan.
    """
    golden = _GOLDEN["cycles"][name]
    path = next(p for p in corpus_paths() if p.stem == name)
    recorded, _report = load_planner_input(path)
    assert recorded.load_reserve_enabled is False
    assert recorded.load_error_profile is None
    if enabled_without_history:
        recorded = replace(
            recorded, load_reserve_enabled=True, load_error_profile=LoadErrorProfile()
        )

    output = run_planner(recorded)

    if enabled_without_history:
        assert output.load_reserve is not None
        assert output.load_reserve["reason"] == REASON_NO_HISTORY
    else:
        assert output.load_reserve is None
    assert output.plan_cost is not None
    assert output.plan_cost.load_reserve_penalty == pytest.approx(0.0)
    assert output.winner_name == golden["winner_name"]
    assert output.plan_cost.total_cost == pytest.approx(golden["total_cost"], abs=1e-6)
    assert output.plan_cost.score == pytest.approx(golden["score"], abs=1e-6)
    assert output.plan_cost.terminal_soc_value == pytest.approx(
        golden["terminal_soc_value"], abs=1e-6
    )
    assert output.battery_soc_at_end == pytest.approx(
        golden["battery_soc_at_end"], abs=1e-6
    )
    assert len(output.slots) == len(golden["slots"])
    for slot, (label, charged, discharged, grid_import, grid_export) in zip(
        output.slots, golden["slots"]
    ):
        assert slot.recommendation == label, slot.start
        assert slot.batteries_charged_kwh == pytest.approx(charged, abs=1e-6)
        assert slot.batteries_discharged_kwh == pytest.approx(discharged, abs=1e-6)
        assert slot.grid_import_kwh == pytest.approx(grid_import, abs=1e-6)
        assert slot.grid_export_kwh == pytest.approx(grid_export, abs=1e-6)


@_needs_scipy
@pytest.mark.parametrize("path", corpus_paths(), ids=lambda p: p.stem)
def test_a_fresh_history_plans_every_recorded_cycle_as_without_the_reserve(
    path: Path,
) -> None:
    """σ unknown (a fresh tracker): the plan of the option off, slot by slot."""
    recorded, _report = load_planner_input(path)

    off = run_planner(recorded)
    fresh = run_planner(
        replace(
            recorded, load_reserve_enabled=True, load_error_profile=LoadErrorProfile()
        )
    )

    assert fresh.load_reserve is not None
    assert fresh.load_reserve["reason"] == REASON_NO_HISTORY
    _assert_same_plan(fresh, off)


@_needs_scipy
def test_flat_prices_plan_as_without_the_reserve() -> None:
    """σ > 0 but no window dearer than a charge before it: nothing is added."""
    flat = make_flat_price_input(now_iso="2024-01-15T06:00:00+01:00")
    now = datetime.fromisoformat(flat.now_iso)

    off = run_planner(flat)
    on = run_planner(
        replace(flat, load_reserve_enabled=True, load_error_profile=_dense_history(now))
    )

    assert on.load_reserve is not None
    assert on.load_reserve["reason"] == REASON_NO_PRICE_ASYMMETRY
    assert on.load_reserve["windows"] == []
    assert on.plan_cost is not None
    assert on.plan_cost.load_reserve_penalty == pytest.approx(0.0)
    _assert_same_plan(on, off)


def _dense_history(now: datetime, per_slot: float = 0.05) -> LoadErrorProfile:
    """Return 30 days of errors, ``±per_slot`` kWh on alternating days."""
    end = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    errors = {}
    for back in range(1, 30 * 96 + 1):
        slot = end - timedelta(minutes=_STEP * back)
        errors[_minute(slot)] = per_slot if (back // 96) % 2 else -per_slot
    return LoadErrorProfile(errors=errors)


@_needs_scipy
@pytest.mark.parametrize("path", corpus_paths(), ids=lambda p: p.stem)
def test_recorded_cycles_keep_every_invariant_with_the_reserve_on(path: Path) -> None:
    """Energy balance, SoC bounds, cost identity and terminal SoC hold."""
    recorded, _report = load_planner_input(path)
    now = datetime.fromisoformat(recorded.now_iso)
    inp = replace(
        recorded, load_reserve_enabled=True, load_error_profile=_dense_history(now)
    )

    output = run_planner(inp)

    assert output.load_reserve is not None
    violations = check_invariants(inp, output)
    assert not violations, format_violations(violations)
    assert output.plan_cost is not None
    winner = next(c for c in output.candidates if c.name == output.winner_name)
    assert winner._cost is not None
    assert winner._cost.score == pytest.approx(output.plan_cost.score)
    for window in output.load_reserve["windows"]:
        assert 0.0 <= window["stock_at_window_end_kwh"] <= window["asked_kwh"] + 1e-9
        assert window["asked_kwh"] <= 2.0 * window["sigma_kwh"] + 2e-3


# ---------------------------------------------------------------------------
# The option
# ---------------------------------------------------------------------------


class TestOption:
    """``hsem_load_reserve_enabled`` is opt-in and reaches the planner."""

    def test_default_is_off(self) -> None:
        assert DEFAULT_CONFIG_VALUES["hsem_load_reserve_enabled"] is False
        assert PlannerInput(now_iso="").load_reserve_enabled is False
        assert PlannerInput(now_iso="").load_error_profile is None

    @pytest.mark.parametrize(
        ("options", "expected"),
        [
            ({}, False),
            ({"hsem_load_reserve_enabled": False}, False),
            ({"hsem_load_reserve_enabled": True}, True),
        ],
    )
    def test_the_option_and_the_history_reach_the_planner_input(
        self, options: dict[str, Any], expected: bool
    ) -> None:
        entry = MagicMock()
        entry.data = {}
        entry.options = options
        cfg = build_sensor_config(entry)
        profile = _history()

        planner_input = build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
            load_error_profile=profile,
        )

        assert cfg.load_reserve_enabled is expected
        assert planner_input.load_reserve_enabled is expected
        assert planner_input.load_error_profile is profile

    @pytest.mark.asyncio
    async def test_flow_offers_and_requires_it(self) -> None:
        schema = await get_battery_economics_step_schema(None)
        keys = {str(key.schema) for key in schema.schema}

        assert "hsem_load_reserve_enabled" in keys
        errors = await validate_battery_economics_input({})
        assert errors["hsem_load_reserve_enabled"] == "required"

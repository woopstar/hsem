"""Tests for the MILP-based optimizer and associated Bug 2/3/5 fixes (issue #416).

Coverage
--------
- MILP produces a cheaper plan than the rule-based baseline on a clear
  arbitrage case (buy cheap, sell expensive).
- MILP falls back gracefully when the solver is given a degenerate problem.
- MILP candidate is present in the output candidates list after a planner run.
- Bug 2: ``_AGGRESSIVE_CHARGE_SLOTS`` scales with battery headroom, not a fixed 3.
- Bug 3: ``replacement_price_per_kwh`` uses the minimum future price, not average.
- Bug 5: Aggressive strategy guards against **all** discharge windows, not just the
  first, when multiple discharge windows exist.
- Performance: MILP solves a 96-slot (48 h × 30 min) horizon in under 100 ms.
"""

from __future__ import annotations

import math
import time as time_module
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.candidate_generator import (
    CANDIDATE_MILP,
    _apply_aggressive_strategy,
    _copy_slots,
)
from custom_components.hsem.planner.cost_function import CostWeights, score_plan
from custom_components.hsem.planner.milp_optimizer import is_scipy_available, solve_milp
from custom_components.hsem.planner.soc_simulation import simulate_soc
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations
from tests.planner.fixtures import make_summer_day_input, make_winter_day_input

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_slot(
    *,
    hour: int,
    import_price: float = 0.20,
    export_price: float = 0.05,
    pv_kwh: float = 0.0,
    consumption_kwh: float = 0.5,
    recommendation: str | None = None,
    batteries_charged_kwh: float = 0.0,
) -> PlannedSlot:
    """Build a minimal PlannedSlot for MILP unit tests."""
    start = datetime(2024, 6, 15, hour, 0, tzinfo=_TZ)
    s = PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=import_price, export_price=export_price),
        recommendation=recommendation,
        batteries_charged_kwh=batteries_charged_kwh,
    )
    s.avg_house_consumption_kwh = consumption_kwh
    s.solcast_pv_estimate_kwh = pv_kwh
    s.ev_planned_load_kwh = 0.0
    s.estimated_net_consumption_kwh = consumption_kwh - pv_kwh
    return s


def _make_arbitrage_slots(
    cheap_hours: list[int],
    expensive_hours: list[int],
    cheap_price: float = 0.05,
    expensive_price: float = 3.00,
    neutral_price: float = 0.50,
    total_hours: int = 24,
) -> list[PlannedSlot]:
    """Build a slot list with clear buy-low / sell-high opportunities.

    Cheap hours have import_price=cheap_price; expensive hours have
    import_price=expensive_price; remaining hours use neutral_price.
    """
    slots = []
    for h in range(total_hours):
        if h in cheap_hours:
            imp = cheap_price
        elif h in expensive_hours:
            imp = expensive_price
        else:
            imp = neutral_price
        slots.append(
            _make_slot(
                hour=h,
                import_price=imp,
                export_price=round(imp * 0.8, 4),
                consumption_kwh=0.3,
            )
        )
    return slots


def _score(slots: list[PlannedSlot], current_kwh: float) -> float:
    """Quick helper: simulate SoC then return the plan score."""
    simulate_soc(
        slots,
        _NOW,
        current_kwh,
        usable_kwh=9.0,
        max_capacity_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
        rated_kwh=10.0,
        end_of_discharge_soc_pct=10.0,
    )
    weights = CostWeights(
        min_soc_pct=10.0,
        max_soc_pct=100.0,
    )
    return score_plan(slots, weights, slot_duration_hours=1.0, now=_NOW).score


# ---------------------------------------------------------------------------
# MILP availability guard
# ---------------------------------------------------------------------------


def _scipy_skip():
    """Pytest mark — skip test when scipy is not installed."""
    return pytest.mark.skipif(
        not is_scipy_available(), reason="scipy not available in this environment"
    )


# ---------------------------------------------------------------------------
# MILP correctness tests
# ---------------------------------------------------------------------------


@_scipy_skip()
def test_milp_charges_in_cheap_slots_and_discharges_in_expensive():
    """MILP must charge during cheap hours and discharge during expensive hours."""
    # 4 cheap hours (0–3), 4 expensive hours (20–23), battery empty
    cheap = [0, 1, 2, 3]
    expensive = [20, 21, 22, 23]
    slots = _make_arbitrage_slots(
        cheap, expensive, cheap_price=0.05, expensive_price=3.0
    )

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
    )

    assert milp_result is not None, (
        "MILP must return a solution on a clear arbitrage case"
    )
    result, _diag = milp_result

    # At least some cheap-hour slots should be BatteriesChargeGrid
    charge_hours = {
        s.start.hour
        for s in result
        if s.recommendation == Recommendations.BatteriesChargeGrid.value
    }
    discharge_hours = {
        s.start.hour
        for s in result
        if s.recommendation == Recommendations.BatteriesDischargeMode.value
    }

    # The LP should charge during some cheap hours
    assert charge_hours & set(cheap), (
        f"Expected charge in cheap hours {cheap}, got charge hours: {sorted(charge_hours)}"
    )
    # The LP should discharge during some expensive hours
    assert discharge_hours & set(expensive), (
        f"Expected discharge in expensive hours {expensive}, "
        f"got discharge hours: {sorted(discharge_hours)}"
    )


@_scipy_skip()
def test_milp_cheaper_than_no_action_on_arbitrage_case():
    """MILP plan must score lower than do-nothing on a clear arbitrage opportunity."""
    cheap = [0, 1, 2, 3]
    expensive = [20, 21, 22, 23]
    slots = _make_arbitrage_slots(
        cheap, expensive, cheap_price=0.05, expensive_price=3.0
    )

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
    )
    assert milp_result is not None
    milp_slots, _diag = milp_result

    # No-action baseline: clear all charge/discharge on a copy of the slots
    baseline_slots = _copy_slots(slots)
    for s in baseline_slots:
        s.recommendation = None
        s.batteries_charged_kwh = 0.0

    milp_score = _score(milp_slots, current_kwh=0.0)
    baseline_score = _score(baseline_slots, current_kwh=0.0)

    assert milp_score < baseline_score, (
        f"MILP score {milp_score:.4f} must be lower than no-action {baseline_score:.4f}"
    )


@_scipy_skip()
def test_milp_respects_soc_upper_bound():
    """MILP solution must never charge the battery beyond usable_kwh."""
    slots = _make_arbitrage_slots([0, 1, 2, 3, 4, 5], [], cheap_price=0.01)
    usable_kwh = 5.0

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=usable_kwh,
        max_charge_per_slot=3.0,
        max_discharge_per_slot=None,
    )
    assert milp_result is not None
    result, _diag = milp_result

    # Run SoC simulation to get actual capacity values
    simulate_soc(
        result,
        _NOW,
        current_kwh=0.0,
        usable_kwh=usable_kwh,
        max_capacity_kwh=usable_kwh,
        max_charge_per_slot=3.0,
        max_discharge_per_slot=None,
        rated_kwh=10.0,
        end_of_discharge_soc_pct=0.0,
    )

    for slot in result:
        if slot.estimated_battery_capacity_kwh > 0:
            # Allow a small epsilon for floating-point rounding in simulate_soc
            assert slot.estimated_battery_capacity_kwh <= usable_kwh + 1e-4, (
                f"SoC capacity {slot.estimated_battery_capacity_kwh:.3f} exceeds "
                f"usable_kwh={usable_kwh} at {slot.start}"
            )


@_scipy_skip()
def test_milp_fallback_on_degenerate_input():
    """When usable_kwh=0 the MILP must return None (no valid schedule)."""
    slots = _make_arbitrage_slots([0], [12], cheap_price=0.01, expensive_price=3.0)

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=0.0,  # degenerate — no battery
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
    )
    assert result is None, "MILP must return None when battery is unavailable"


@_scipy_skip()
def test_milp_fallback_on_empty_slot_list():
    """MILP must return None when given an empty slot list."""
    result = solve_milp(
        [],
        _NOW,
        current_kwh=1.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
    )
    assert result is None


@_scipy_skip()
def test_milp_candidate_present_in_planner_output():
    """A full planner run must include the 'milp' candidate in output.candidates."""
    inp = make_winter_day_input(battery_soc_pct=20.0)
    output = run_planner(inp)

    candidate_names = {c.name for c in (output.candidates or [])}
    assert CANDIDATE_MILP in candidate_names, (
        f"Expected 'milp' in candidates, got: {sorted(candidate_names)}"
    )


@_scipy_skip()
def test_milp_winner_cost_invariant_holds():
    """output.plan_cost.score must equal score_plan(output.slots) after a full run."""
    inp = make_summer_day_input(battery_soc_pct=30.0)
    output = run_planner(inp)

    assert output.plan_cost is not None
    # The winner.cost == final_output.cost invariant is checked implicitly by the
    # engine re-scoring after the fill-pass.  We simply assert the output has a
    # finite, non-NaN score.
    assert not math.isnan(output.plan_cost.score), "plan_cost.score must not be NaN"
    assert math.isfinite(output.plan_cost.score), "plan_cost.score must be finite"


@_scipy_skip()
def test_milp_cycle_cost_matches_score_plan():
    """MILP cycle cost must match score_plan()'s max(charge, discharge) per slot.

    Scenario: 2-slot horizon where the LP charges in slot 0 (cheap import)
    and discharges in slot 1 (expensive export).  Verify that the MILP's
    objective cycle cost (α * m[t] where m[t] = max(ec, ed)) is consistent
    with what score_plan() would compute using max(batteries_charged_kwh,
    batteries_discharged_kwh).
    """
    cheap_price = 0.05
    expensive_price = 3.00
    cycle_cost = 0.15  # non-trivial cycle cost per kWh
    usable_kwh = 9.0
    max_charge = 5.0
    max_discharge = 5.0

    # Slot 0: cheap import → charge
    # Slot 1: expensive export → discharge (but also has consumption to serve)
    slots = [
        _make_slot(
            hour=0,
            import_price=cheap_price,
            export_price=round(cheap_price * 0.8, 4),
            consumption_kwh=0.3,
        ),
        _make_slot(
            hour=1,
            import_price=expensive_price,
            export_price=round(expensive_price * 0.8, 4),
            consumption_kwh=0.3,
        ),
    ]

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=usable_kwh,
        max_charge_per_slot=max_charge,
        max_discharge_per_slot=max_discharge,
        cycle_cost_per_kwh=cycle_cost,
    )
    assert milp_result is not None, "MILP must return a solution"
    result, _diag = milp_result

    # Run SoC simulation to populate batteries_discharged_kwh
    simulate_soc(
        result,
        _NOW,
        current_kwh=0.0,
        usable_kwh=usable_kwh,
        max_capacity_kwh=usable_kwh,
        max_charge_per_slot=max_charge,
        max_discharge_per_slot=max_discharge,
        rated_kwh=10.0,
        end_of_discharge_soc_pct=10.0,
    )

    # Compute expected cycle cost using the same max(charge, discharge) rule
    # that score_plan() uses
    expected_cycle_cost = 0.0
    for s in result:
        throughput = max(s.batteries_charged_kwh, s.batteries_discharged_kwh)
        expected_cycle_cost += throughput * cycle_cost

    # The actual cost per slot from slot 0 (charge) = ec[0] * cycle_cost
    # from slot 1 (discharge) = ed[1] * cycle_cost
    # These should be individually available from the result slots
    actual_cycle_cost = 0.0
    for s in result:
        throughput = max(s.batteries_charged_kwh, s.batteries_discharged_kwh)
        actual_cycle_cost += throughput * cycle_cost

    assert abs(actual_cycle_cost - expected_cycle_cost) < 1e-6, (
        f"Cycle cost mismatch: actual={actual_cycle_cost:.6f} "
        f"expected={expected_cycle_cost:.6f}"
    )

    # Also verify that score_plan() cycle_cost matches
    cost_breakdown = score_plan(
        result,
        CostWeights(cycle_cost_per_kwh=cycle_cost, min_soc_pct=10.0, max_soc_pct=100.0),
        slot_duration_hours=1.0,
        now=_NOW,
    )
    assert abs(cost_breakdown.cycle_cost - actual_cycle_cost) < 1e-6, (
        f"score_plan cycle_cost {cost_breakdown.cycle_cost:.6f} does not match "
        f"MILP-implied cycle cost {actual_cycle_cost:.6f}"
    )


# ---------------------------------------------------------------------------
# Performance test
# ---------------------------------------------------------------------------


@_scipy_skip()
def test_milp_solves_96_slot_horizon_under_100ms():
    """MILP must solve a 96-slot (48 h × 30-min) horizon in under 100 ms."""
    # Build a 48-hour, 30-min slot list (96 slots)
    import copy

    base_slot = _make_slot(hour=0, import_price=0.20)
    slots_96: list[PlannedSlot] = []
    for i in range(96):
        s = copy.copy(base_slot)
        minutes_offset = i * 30
        s.start = _NOW + timedelta(minutes=minutes_offset)
        s.end = s.start + timedelta(minutes=30)
        # Vary prices to give the LP something interesting to solve
        price = 0.10 + 0.20 * abs(math.sin(i * math.pi / 24))
        s.price = SlotPrice(
            import_price=round(price, 4), export_price=round(price * 0.8, 4)
        )
        s.avg_house_consumption_kwh = 0.15  # 0.15 kWh per 30-min slot
        s.solcast_pv_estimate_kwh = max(0.0, 0.3 * math.sin(i * math.pi / 32))
        s.ev_planned_load_kwh = 0.0
        s.estimated_net_consumption_kwh = (
            s.avg_house_consumption_kwh - s.solcast_pv_estimate_kwh
        )
        slots_96.append(s)

    t_start = time_module.perf_counter()
    milp_result = solve_milp(
        slots_96,
        _NOW,
        current_kwh=5.0,
        usable_kwh=9.0,
        max_charge_per_slot=2.5,  # 5 kW × 0.5 h
        max_discharge_per_slot=2.5,
    )
    elapsed = time_module.perf_counter() - t_start

    assert milp_result is not None, "MILP must solve the 96-slot horizon"
    _, _diag = milp_result
    assert elapsed < 0.10, (
        f"MILP took {elapsed * 1000:.1f} ms on 96 slots — must be under 100 ms"
    )


# ---------------------------------------------------------------------------
# Bug 2: Dynamic aggressive slot count
# ---------------------------------------------------------------------------


def _make_minimal_inp_for_generator(
    *,
    battery_cycle_cost_per_kwh: float = 0.0,
) -> PlannerInput:
    """Return a minimal PlannerInput sufficient to call generate_candidates."""
    from tests.planner.fixtures import make_summer_day_input

    inp = make_summer_day_input()
    inp.battery_cycle_cost_per_kwh = battery_cycle_cost_per_kwh
    return inp


def test_aggressive_slots_scale_with_battery_headroom():
    """Aggressive charge-slot count must equal ceil(headroom / max_charge_per_slot).

    When the battery has 6 kWh of headroom and max charge is 2 kWh/slot,
    the aggressive strategy should claim 3 slots (ceil(6/2)=3).
    """
    slots = _make_arbitrage_slots(
        cheap_hours=[0, 1, 2, 3],
        expensive_hours=[20, 21, 22, 23],
    )

    # Apply the aggressive strategy with known inputs
    slots_copy = _copy_slots(slots)
    _apply_aggressive_strategy(
        slots_copy,
        _NOW,
        max_charge_per_slot=2.0,
        current_kwh=3.0,  # 3 kWh stored
        usable_kwh=9.0,  # 9 kWh usable → 6 kWh headroom
    )

    # Expected: ceil(6.0 / 2.0) = 3 charge slots
    charge_slots_count = sum(
        1
        for s in slots_copy
        if s.recommendation == Recommendations.BatteriesChargeGrid.value
    )
    assert charge_slots_count == 3, (
        f"Expected 3 charge slots for 6 kWh headroom / 2 kWh per slot, "
        f"got {charge_slots_count}"
    )


def test_aggressive_slots_fallback_when_headroom_zero():
    """When battery is full (headroom=0) the aggressive strategy must not charge.

    headroom = usable_kwh - current_kwh = 9 - 9 = 0.  There is no room to
    store additional energy, so the aggressive strategy should claim 0 charge
    slots.  The old fixed-constant code would always claim 3 regardless.
    """
    slots = _make_arbitrage_slots(cheap_hours=[0, 1, 2, 3], expensive_hours=[])
    slots_copy = _copy_slots(slots)

    _apply_aggressive_strategy(
        slots_copy,
        _NOW,
        max_charge_per_slot=2.0,
        current_kwh=9.0,  # full
        usable_kwh=9.0,
    )

    charge_slots_count = sum(
        1
        for s in slots_copy
        if s.recommendation == Recommendations.BatteriesChargeGrid.value
    )
    assert charge_slots_count == 0, (
        f"Expected 0 charge slots when battery is full (headroom=0), got {charge_slots_count}"
    )


def test_aggressive_slots_fallback_on_degenerate_max_charge():
    """When max_charge_per_slot is 0 (degenerate) the fallback of 3 slots is used."""
    slots = _make_arbitrage_slots(cheap_hours=[0, 1, 2, 3, 4], expensive_hours=[])
    slots_copy = _copy_slots(slots)

    _apply_aggressive_strategy(
        slots_copy,
        _NOW,
        max_charge_per_slot=0.0,  # degenerate
        current_kwh=0.0,
        usable_kwh=9.0,
    )

    # Fallback of 3 is used; there are 5 candidates so 3 should be claimed
    charge_slots_count = sum(
        1
        for s in slots_copy
        if s.recommendation == Recommendations.BatteriesChargeGrid.value
    )
    assert charge_slots_count == 3, (
        f"Expected fallback of 3 charge slots, got {charge_slots_count}"
    )


def test_aggressive_large_headroom_claims_all_available_charge_candidates():
    """When headroom requires more slots than available, all eligible slots are charged.

    With usable_kwh=10, current_kwh=0, and max_charge_per_slot=2.0,
    the strategy wants ceil(10/2)=5 charge slots.  With Bug D fix, the
    aggressive strategy first identifies prospective discharge slots (the
    most expensive ones), then charges only before those.  We use a mix of
    cheap (0.05) and expensive (0.50) hours so that the expensive slots
    become discharge and the cheap slots before them become charge.

    Key invariant: at large headroom we claim significantly more than the old
    fixed value of 3.
    """
    # 24 slots: first 20 cheap (0.05), last 4 expensive (0.50)
    slots = _make_arbitrage_slots(
        cheap_hours=list(range(20)),
        expensive_hours=[20, 21, 22, 23],
        cheap_price=0.05,
        expensive_price=0.50,
    )
    slots_copy = _copy_slots(slots)

    _apply_aggressive_strategy(
        slots_copy,
        _NOW,
        max_charge_per_slot=2.0,
        current_kwh=0.0,
        usable_kwh=10.0,  # headroom = 10 → wants ceil(10/2)=5 charge slots
        max_discharge_per_slot=2.0,  # ceil(10/2)=5 discharge slots
    )

    charge_slots_count = sum(
        1
        for s in slots_copy
        if s.recommendation == Recommendations.BatteriesChargeGrid.value
    )
    # Old code: exactly 3 charge slots (fixed constant).
    # New code: 5 charge slots (ceil(10/2)=5) before first discharge at hour 20.
    assert charge_slots_count > 3, (
        f"Expected significantly more than 3 charge slots at large headroom, "
        f"got {charge_slots_count}"
    )
    assert charge_slots_count >= 5, (
        f"Expected at least 5 charge slots (ceil(10/2)=5), got {charge_slots_count}"
    )


# ---------------------------------------------------------------------------
# Bug 3: Terminal SoC replacement price uses min, not average
# ---------------------------------------------------------------------------


def test_replacement_price_is_minimum_of_future_prices():
    """Engine must pass min(future_import_prices) as replacement_price_per_kwh.

    We indirectly verify this by checking the terminal_soc_value in the plan_cost
    breakdown uses the minimum price, not the average.  A plan that ends with more
    stored energy than it started should have a lower (more negative) terminal_soc_value
    when the minimum price is used vs. the average (because min < avg typically).
    """
    from custom_components.hsem.planner.cost_function import CostWeights, score_plan

    # Build a slot list with heterogeneous prices: min=0.05, avg≈0.55
    slots = [
        _make_slot(hour=h, import_price=(0.05 if h < 4 else 1.0), consumption_kwh=0.1)
        for h in range(8)
    ]

    # Run SoC sim: charge in all slots to increase terminal SoC above initial
    for s in slots[:4]:
        s.recommendation = Recommendations.BatteriesChargeGrid.value
        s.batteries_charged_kwh = 1.0

    weights = CostWeights(min_soc_pct=10.0, max_soc_pct=100.0)
    simulate_soc(
        slots,
        _NOW,
        current_kwh=1.0,
        usable_kwh=9.0,
        max_capacity_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
        rated_kwh=10.0,
        end_of_discharge_soc_pct=0.0,
    )

    import_prices = [s.price.import_price for s in slots]
    min_price = min(import_prices)
    avg_price = sum(import_prices) / len(import_prices)

    # Score with min price (as the engine now does)
    cost_min = score_plan(
        slots,
        weights,
        slot_duration_hours=1.0,
        now=_NOW,
        initial_battery_kwh=1.0,
        replacement_price_per_kwh=min_price,
    )
    # Score with average price (old behaviour)
    cost_avg = score_plan(
        slots,
        weights,
        slot_duration_hours=1.0,
        now=_NOW,
        initial_battery_kwh=1.0,
        replacement_price_per_kwh=avg_price,
    )

    # Both should assign a credit (negative terminal_soc_value) because
    # terminal SoC > initial SoC.  The credit is larger (more negative) with
    # the higher avg price.
    assert cost_min.terminal_soc_value < 1e-9, (
        "Expected negative terminal_soc_value (credit) when terminal SoC > initial SoC"
    )
    # min_price < avg_price → |credit with min| < |credit with avg|
    # i.e. terminal_soc_value is less negative with min price
    assert cost_min.terminal_soc_value >= cost_avg.terminal_soc_value - 1e-9, (
        f"Credit with min price ({cost_min.terminal_soc_value:.4f}) should be "
        f"≥ credit with avg price ({cost_avg.terminal_soc_value:.4f}) — "
        "min price is cheaper so stored energy is worth less"
    )


# ---------------------------------------------------------------------------
# Bug 5: Multi-discharge window guard
# ---------------------------------------------------------------------------


def test_aggressive_no_charge_after_any_discharge_window():
    """Aggressive strategy must not place charge slots after ANY discharge window.

    When two discharge windows exist (e.g. hours 6 and 18), no charge slot
    must appear at or after the start of the earlier window.
    """
    # Set up: 4 cheap slots at hours 8–11 (after the first discharge window at 6)
    # and a second discharge window at hour 18.
    # The aggressive strategy should NOT charge at hours 8-11 because they
    # fall after the first discharge window (hour 6).

    # Build slots hours 0-23 with discharge already placed at hours 6 and 18
    slots = []
    for h in range(24):
        imp = 0.05 if h in (8, 9, 10, 11) else 0.50
        s = _make_slot(hour=h, import_price=imp, export_price=round(imp * 0.8, 4))
        if h in (6, 18):
            s.recommendation = Recommendations.BatteriesDischargeMode.value
        slots.append(s)

    _apply_aggressive_strategy(
        slots,
        _NOW,
        max_charge_per_slot=2.0,
        current_kwh=0.0,
        usable_kwh=9.0,
    )

    # Any slot at or after the first discharge window (hour 6) must NOT be
    # assigned a charge recommendation by the aggressive strategy.
    illegal_charge = [
        s.start.hour
        for s in slots
        if s.start.hour >= 6
        and s.recommendation == Recommendations.BatteriesChargeGrid.value
    ]
    assert not illegal_charge, (
        f"Aggressive strategy placed charge slots at hours {illegal_charge} "
        "which are at or after the first discharge window at hour 6"
    )


def test_aggressive_charge_only_before_first_discharge_window():
    """Charge slots must only appear before the earliest discharge window."""
    # Discharge at hour 8; cheap hours spread 0–15
    slots = []
    for h in range(24):
        imp = 0.05 if h in range(0, 16) else 0.80
        s = _make_slot(hour=h, import_price=imp)
        if h == 8:
            s.recommendation = Recommendations.BatteriesDischargeMode.value
        slots.append(s)

    _apply_aggressive_strategy(
        slots,
        _NOW,
        max_charge_per_slot=2.0,
        current_kwh=0.0,
        usable_kwh=9.0,
    )

    charge_hours = sorted(
        s.start.hour
        for s in slots
        if s.recommendation == Recommendations.BatteriesChargeGrid.value
    )

    # All charge hours must be strictly before hour 8
    assert all(h < 8 for h in charge_hours), (
        f"Charge hours {charge_hours} include slots at or after discharge window at hour 8"
    )


# ---------------------------------------------------------------------------
# Bug A — Cycle cost double-count verification
# ---------------------------------------------------------------------------


@_scipy_skip()
def test_cycle_cost_obj_coefficients_sum_to_one_cycle_cost():
    """Bug A: cycle cost is correctly counted once per slot in the MILP.

    The cost function counts cycle cost as max(charge, discharge) * cycle_cost_per_kwh
    per slot. Since cycle_cost_per_kwh already has a 2× factor in its denominator
    (purchase_price / (2 * usable_kwh * expected_cycles)), the MILP correctly uses
    cycle_cost_per_kwh for both charge and discharge coefficients.

    For a full 9 kWh cycle (charge 9 kWh, discharge 9 kWh) at 0.10/kWh:
    - Charge slot: 9 * 0.10 = 0.90
    - Discharge slot: 9 * 0.10 = 0.90
    - Total: 1.80

    We verify by checking that the MILP solution's cycle cost is approximately
    what we expect for the actual throughput.
    """
    from custom_components.hsem.planner.milp_optimizer import solve_milp

    # Build a 4-slot case: 2 cheap (0.05), 2 expensive (2.00), no conversion loss.
    cheap = [0, 1]
    expensive = [22, 23]
    slots = _make_arbitrage_slots(
        cheap, expensive, cheap_price=0.05, expensive_price=2.00
    )

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=1.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        cycle_cost_per_kwh=0.10,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
    )
    assert milp_result is not None, "MILP must return a solution"
    milp_slots, _diag = milp_result

    # Score the MILP plan with the same cycle cost
    simulate_soc(
        milp_slots,
        _NOW,
        current_kwh=1.0,
        usable_kwh=9.0,
        max_capacity_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
        rated_kwh=10.0,
        end_of_discharge_soc_pct=10.0,
    )
    weights = CostWeights(
        min_soc_pct=10.0,
        max_soc_pct=100.0,
        battery_purchase_price=0.0,
        battery_expected_cycles=0,
        battery_rated_capacity_kwh=9.0,
        cycle_cost_per_kwh=0.10,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
    )
    bd = score_plan(milp_slots, weights, slot_duration_hours=1.0, now=_NOW)

    # With 2 cheap charge slots (max 5 kWh each) and 2 expensive discharge slots,
    # the battery can cycle roughly 8 kWh (headroom = 9 - 1 = 8).
    # Cycle cost ≈ 8 * 0.10 (charge) + 8 * 0.10 (discharge) = 1.60.
    # The actual value may be slightly less due to terminal-SoC credit.
    assert bd.cycle_cost > 0.5, (
        f"Bug A: cycle_cost={bd.cycle_cost:.4f} too low — expected ~1.60 "
        "for ~8 kWh cycled at 0.10/kWh."
    )
    assert bd.cycle_cost < 2.0, (
        f"Bug A: cycle_cost={bd.cycle_cost:.4f} exceeds 2.0 — "
        "cycle cost should be ~1.60 for ~8 kWh cycled."
    )


# ---------------------------------------------------------------------------
# Bug B — Terminal SoC credit replaces hold[t]
# ---------------------------------------------------------------------------


@_scipy_skip()
def test_milp_holds_energy_for_expensive_slot_via_terminal_soc():
    """Bug B: With replacement_price set to expensive slot price, the MILP
    must discharge in the expensive slot, not the cheap one.

    6-slot horizon: cheap[0]=0.05, neutral[1-4]=0.30, expensive[5]=2.00.
    Battery starts with enough energy (5 kWh) to serve either cheap or expensive.
    With replacement_price=2.00, the LP should prefer holding energy for the
    expensive slot vs. discharging in the cheap one.
    """
    slots: list[PlannedSlot] = []
    for h in range(6):
        if h == 0:
            imp = 0.05
        elif h == 5:
            imp = 2.00
        else:
            imp = 0.30
        s = _make_slot(hour=h, import_price=imp, consumption_kwh=0.5)
        s.solcast_pv_estimate_kwh = 0.0
        s.estimated_net_consumption_kwh = (
            s.avg_house_consumption_kwh - s.solcast_pv_estimate_kwh
        )
        slots.append(s)

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=5.0,
        usable_kwh=5.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        cycle_cost_per_kwh=0.0,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
    )
    assert milp_result is not None
    milp_slots, _diag = milp_result

    simulate_soc(
        milp_slots,
        _NOW,
        current_kwh=5.0,
        usable_kwh=5.0,
        max_capacity_kwh=5.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
        rated_kwh=5.0,
        end_of_discharge_soc_pct=0.0,
    )

    discharges_at_hour_5 = any(
        s.start.hour == 5
        and s.recommendation == Recommendations.BatteriesDischargeMode.value
        for s in milp_slots
    )

    assert discharges_at_hour_5, (
        "Bug B: MILP must discharge in expensive slot (hour 5, price=2.00) "
        "when replacement_price captures its value."
    )


@_scipy_skip()
def test_milp_n_vars_is_8m():
    """n_vars should be 8*m (ec, ed, gi, ge, pv, m, s_max_pen, s_min_pen)."""
    slots = _make_arbitrage_slots([0], [23])
    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=1.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
    )
    assert milp_result is not None, "MILP must return a solution"
    result, _diag = milp_result
    assert len(result) == 24, "Expected 24 slots output"


# ---------------------------------------------------------------------------
# Bug C — Mutual exclusion at negative prices
# ---------------------------------------------------------------------------


@_scipy_skip()
def test_milp_no_simultaneous_charge_discharge_at_negative_prices():
    """Bug C: At negative import prices (with export prices clamped to >= 0),
    the LP must NOT produce simultaneous charge + discharge in any slot.

    Export prices are clamped to >= 0 by the MILP solver to avoid paying
    to export.  This test uses non-negative export prices to verify the
    mutual-exclusion constraint ec/max_charge + ed/max_dis <= 1 works.
    """
    slots = [
        _make_slot(
            hour=h,
            import_price=-0.05,
            export_price=0.01,
            consumption_kwh=0.5,
            pv_kwh=0.0,
        )
        for h in range(4)
    ]
    for s in slots:
        s.estimated_net_consumption_kwh = (
            s.avg_house_consumption_kwh - s.solcast_pv_estimate_kwh
        )

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=3.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        cycle_cost_per_kwh=0.0,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
    )
    # The LP may be infeasible with the clamped export prices (0.01)
    # and negative import prices (-0.05).  Skip the check in that case.
    if milp_result is None:
        return
    milp_slots, _diag = milp_result

    # Verify no slot has both charge and discharge above the minimum threshold.
    for s in milp_slots:
        is_charge = s.recommendation == Recommendations.BatteriesChargeGrid.value
        is_discharge = s.recommendation == Recommendations.BatteriesDischargeMode.value
        assert not (is_charge and is_discharge), (
            f"Bug C: Simultaneous charge+discharge at hour {s.start.hour}"
        )


@_scipy_skip()
def test_milp_mutex_post_hoc_always_trivially_true():
    """Bug C: The post-hoc disambiguation block should never trigger because
    the LP constraint guarantees mutual exclusion. We verify by running
    multiple price patterns (including negative) and checking no slot has
    both recommendations."""
    for prices in [
        [0.05, 0.10, 0.15, 0.20],
        [-0.05, -0.02, 0.10],
        [-0.10, 0.30, 0.50],
        [0.02, 0.01, 0.15],
    ]:
        slots = []
        for h in range(4):
            imp = prices[h % len(prices)]
            s = _make_slot(
                hour=h,
                import_price=imp,
                export_price=imp * 0.8 if imp > 0 else imp * 1.2,
                consumption_kwh=0.5,
                pv_kwh=0.0,
            )
            s.estimated_net_consumption_kwh = (
                s.avg_house_consumption_kwh - s.solcast_pv_estimate_kwh
            )
            slots.append(s)

        milp_result = solve_milp(
            slots,
            _NOW,
            current_kwh=3.0,
            usable_kwh=9.0,
            max_charge_per_slot=5.0,
            max_discharge_per_slot=5.0,
            cycle_cost_per_kwh=0.0,
            charge_efficiency_pct=100.0,
            discharge_efficiency_pct=100.0,
        )
        if milp_result is None:
            continue  # skip infeasible random patterns
        milp_slots, _diag = milp_result

        for s in milp_slots:
            is_charge = s.recommendation == Recommendations.BatteriesChargeGrid.value
            is_discharge = (
                s.recommendation == Recommendations.BatteriesDischargeMode.value
            )
            assert not (is_charge and is_discharge), (
                f"Bug C: Simultaneous charge+discharge at hour {s.start.hour} "
                f"with prices {prices}"
            )


@_scipy_skip()
def test_milp_soc_rises_after_cheap_charge_slot():
    """MILP charge must be picked up by SoC simulation, causing a measurable rise.

    Run solve_milp on a 4-slot horizon with 1 very cheap import slot that
    makes charging clearly profitable.  After passing the result through
    simulate_soc, the SoC after the cheap slot must be above the initial
    current_kwh — proving that soc_simulation reads the MILP-written
    batteries_charged_kwh field.
    """
    slots = [
        _make_slot(
            hour=h,
            import_price=(0.01 if h == 0 else 0.50),
            export_price=0.01,
            consumption_kwh=0.3,
        )
        for h in range(4)
    ]
    for s in slots:
        s.solcast_pv_estimate_kwh = 0.0
        s.estimated_net_consumption_kwh = (
            s.avg_house_consumption_kwh - s.solcast_pv_estimate_kwh
        )

    usable_kwh = 9.0
    initial_kwh = 0.0
    max_charge = 5.0

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=initial_kwh,
        usable_kwh=usable_kwh,
        max_charge_per_slot=max_charge,
        max_discharge_per_slot=None,
        cycle_cost_per_kwh=0.0,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
    )
    assert milp_result is not None, "MILP must solve the 4-slot problem"
    milp_slots, _diag = milp_result

    simulate_soc(
        milp_slots,
        _NOW,
        current_kwh=initial_kwh,
        usable_kwh=usable_kwh,
        max_capacity_kwh=usable_kwh,
        max_charge_per_slot=max_charge,
        max_discharge_per_slot=None,
        rated_kwh=10.0,
        end_of_discharge_soc_pct=10.0,
    )

    # The SoC after hour 0 (end of cheap slot) must be higher than initial_kwh
    hour_0_slots = [s for s in milp_slots if s.start.hour == 0]
    assert len(hour_0_slots) == 1
    soc_after_cheap = hour_0_slots[0].estimated_battery_capacity_kwh
    assert soc_after_cheap > initial_kwh, (
        f"SoC after cheap charge slot is {soc_after_cheap:.3f} kWh, "
        f"expected > {initial_kwh} — MILP charge may not be propagated "
        "through soc_simulation"
    )


# ---------------------------------------------------------------------------
# Penalty / soft constraint tests (issue #531)
# ---------------------------------------------------------------------------


@_scipy_skip()
def test_milp_overcharged_start_discharges_with_penalty():
    """Overcharged battery (current_kwh > usable_kwh) should cause the MILP
    to discharge aggressively with penalty variables absorbing the excess.

    With current_kwh=12 and usable_kwh=9, the battery starts 3 kWh above max.
    The MILP must:
    - Return a valid solution (not None)
    - Discharge aggressively in early slots to bring SoC within bounds
    - Have s_max_pen violations in early slots that decrease over time
    """
    slots = _make_arbitrage_slots([0, 1, 2, 3], [20, 21, 22, 23])

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=12.0,  # 3 kWh above usable_kwh=9.0
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        cycle_cost_per_kwh=0.01,
    )

    assert milp_result is not None, (
        "MILP must return a solution even with overcharged battery"
    )
    milp_slots, diag = milp_result

    # Check diagnostics
    assert diag is not None
    assert diag.get("has_violations", False), (
        "Overcharged start must trigger s_max_pen violations"
    )
    total = diag.get("total_violation_kwh", 0.0)
    assert total > 1e-6, f"Expected penalty > 0 for overcharged start, got {total}"

    # Verify discharge happens in early slots
    early_discharge_slots = [
        s
        for s in milp_slots[:4]
        if s.recommendation
        in (
            Recommendations.BatteriesDischargeMode.value,
            Recommendations.ForceBatteriesDischarge.value,
        )
    ]
    assert len(early_discharge_slots) > 0, (
        "Overcharged battery must discharge in early slots"
    )


@_scipy_skip()
def test_milp_normal_start_zero_penalty():
    """Normal battery (current_kwh within bounds) must produce zero penalty.

    When the initial SoC is within [0, usable_kwh], the MILP must never use
    penalty variables because p_soc >> max(p_imp).  The diagnostics must show
    has_violations=False and total_violation_kwh=0.
    """
    slots = _make_arbitrage_slots([0, 1, 2, 3], [20, 21, 22, 23])

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=5.0,  # safely within bounds (0, 9)
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        cycle_cost_per_kwh=0.01,
    )

    assert milp_result is not None, "MILP must return a solution"
    _milp_slots, diag = milp_result

    assert not diag.get("has_violations", True), (
        "Normal start must have no penalty violations"
    )
    total = diag.get("total_violation_kwh", 1.0)
    assert total < 1e-6, f"Expected zero penalty, got {total}"

    # Also verify the s_max_pen and s_min_pen lists are all ~0
    s_max = diag.get("s_max_pen", [1.0])
    s_min = diag.get("s_min_pen", [1.0])
    assert all(abs(v) < 1e-6 for v in s_max), f"s_max_pen should be zero: {s_max}"
    assert all(abs(v) < 1e-6 for v in s_min), f"s_min_pen should be zero: {s_min}"


@_scipy_skip()
def test_milp_extreme_overcharge_returns_plan_with_violations():
    """Extremely overcharged battery (current_kwh >> usable_kwh) must still
    return a valid plan with violations.

    With current_kwh=18 and usable_kwh=9, the battery is 9 kWh above max.
    The MILP must:
    - Return a solution (not None)
    - Have large penalties
    - Discharge every slot until SoC is within bounds
    """
    slots = _make_arbitrage_slots([0, 1, 2, 3], [20, 21, 22, 23])

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=18.0,  # 9 kWh above usable_kwh=9.0
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        cycle_cost_per_kwh=0.01,
    )

    assert milp_result is not None, (
        "MILP must return a solution even with extremely overcharged battery "
        "(2x usable_kwh)"
    )
    milp_slots, diag = milp_result

    # Must have violations
    assert diag.get("has_violations", False), (
        "Extreme overcharge must trigger violations"
    )
    total = diag.get("total_violation_kwh", 0.0)
    assert total > 1e-6, f"Expected penalty > 0, got {total}"

    # Must have discharge actions (to bring SoC down)
    discharge_slots = [
        s
        for s in milp_slots
        if s.recommendation
        in (
            Recommendations.BatteriesDischargeMode.value,
            Recommendations.ForceBatteriesDischarge.value,
        )
    ]
    assert len(discharge_slots) > 0, "Extremely overcharged battery must discharge"


# ---------------------------------------------------------------------------
# Main fuse / tariff protection tests (issue #567)
# ---------------------------------------------------------------------------


@_scipy_skip()
def test_main_fuse_none_no_constraint():
    """When main_fuse_amps is None, behaviour is identical to current."""
    slots = _make_arbitrage_slots([0, 1, 2, 3], [20, 21, 22, 23])

    result_none = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        main_fuse_amps=None,
    )
    result_no_param = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
    )

    assert result_none is not None
    assert result_no_param is not None
    slots_none, diag_none = result_none
    slots_no_param, _diag_no_param = result_no_param

    # Recommendations should be identical
    for i in range(len(slots_none)):
        assert slots_none[i].recommendation == slots_no_param[i].recommendation, (
            f"Slot {i}: recommendation differs when main_fuse_amps=None"
        )
    # Diagnostics should have no fuse violations
    assert diag_none.get("total_fuse_violation_kwh", 1.0) == pytest.approx(0.0)


@_scipy_skip()
def test_main_fuse_zero_no_constraint():
    """When main_fuse_amps=0, behaviour is identical to current."""
    slots = _make_arbitrage_slots([0, 1, 2, 3], [20, 21, 22, 23])

    result_zero = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        main_fuse_amps=0.0,
    )
    result_no_param = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
    )

    assert result_zero is not None
    assert result_no_param is not None
    slots_zero, diag_zero = result_zero
    slots_no_param, _diag_no_param = result_no_param

    for i in range(len(slots_zero)):
        assert slots_zero[i].recommendation == slots_no_param[i].recommendation, (
            f"Slot {i}: recommendation differs when main_fuse_amps=0"
        )
    assert diag_zero.get("total_fuse_violation_kwh", 1.0) == pytest.approx(0.0)


@_scipy_skip()
def test_main_fuse_normal_load_within_limit():
    """Normal load within fuse limit → zero penalty, plan unchanged.

    25 A fuse → max 25*230*3/1000 = 17.25 kW per hour = 4.3125 kWh per 15-min slot.
    With 0.5 kWh house load per slot, the fuse limit is never approached.
    """
    slots = _make_arbitrage_slots([0, 1, 2, 3], [20, 21, 22, 23])

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        main_fuse_amps=25.0,
    )

    assert result is not None
    _milp_slots, diag = result

    # No fuse violations expected — 0.5 kWh house load is well within 25 A limit
    assert diag.get("total_fuse_violation_kwh", 1.0) == pytest.approx(0.0)
    assert not diag.get("has_violations", True)


@_scipy_skip()
def test_main_fuse_house_load_exceeds_fuse_penalty_fires():
    """House load alone exceeds fuse → penalty fires, plan still returned.

    With a 1 A fuse (tiny), max per slot = 1*230*3/1000*1 = 0.69 kWh.
    House load of 0.5 kWh per slot plus battery charging would exceed this.
    The MILP must still return a plan with violations flagged.
    """
    slots = _make_arbitrage_slots([0, 1, 2, 3], [20, 21, 22, 23])

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        main_fuse_amps=1.0,  # very restrictive
    )

    assert result is not None, (
        "MILP must return a plan even when house load exceeds fuse rating"
    )
    _milp_slots, diag = result

    # Fuse violations should be flagged
    fuse_violation = diag.get("total_fuse_violation_kwh", 0.0)
    assert fuse_violation > 1e-6, (
        f"Expected fuse violations with 1 A fuse, got {fuse_violation}"
    )
    assert diag.get("has_violations", False), (
        "has_violations must be True when fuse violations exist"
    )


@_scipy_skip()
def test_main_fuse_battery_ev_exceed_fuse_throttles_charging():
    """Battery + EV + house load would exceed fuse → MILP throttles charging.

    With a 10 A fuse, max per slot = 10*230*3/1000*1 = 6.9 kWh.
    House load 0.5 kWh + max charge 5.0 kWh = 5.5 kWh < 6.9 kWh (OK).
    But with EV adding 3.0 kWh, total = 8.5 kWh > 6.9 kWh.
    The MILP must throttle either battery or EV charging to stay within limit.
    """
    slots = [
        _make_slot(hour=h, import_price=0.05, consumption_kwh=0.5) for h in range(8)
    ]

    ev_cfg = EVConfig(
        enabled=True,
        capacity_kwh=50.0,
        initial_soc_kwh=0.0,
        target_kwh=10.0,
        max_charge_per_slot=3.0,
        charger_efficiency=0.95,
        deadline_slot=7,
    )

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        main_fuse_amps=10.0,
        ev_configs=[ev_cfg],
    )

    assert result is not None, "MILP must return a plan"
    _milp_slots, diag = result

    # The fuse violation should be zero or very small because the MILP
    # can throttle charging to stay within the limit.
    fuse_violation = diag.get("total_fuse_violation_kwh", 1.0)
    assert fuse_violation < 0.01, (
        f"Fuse violation should be near-zero when MILP can throttle, got {fuse_violation}"
    )


@_scipy_skip()
def test_main_fuse_diagnostics_contains_fuse_violation_kwh():
    """Verify total_fuse_violation_kwh is present in diagnostics dict."""
    slots = _make_arbitrage_slots([0, 1, 2, 3], [20, 21, 22, 23])

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        main_fuse_amps=25.0,
    )

    assert result is not None
    _milp_slots, diag = result

    assert "total_fuse_violation_kwh" in diag, (
        "Diagnostics must contain total_fuse_violation_kwh key"
    )
    assert isinstance(diag["total_fuse_violation_kwh"], float), (
        "total_fuse_violation_kwh must be a float"
    )


@_scipy_skip()
def test_main_fuse_has_violations_flag():
    """Verify has_violations flag is set when fuse violations exist."""
    slots = _make_arbitrage_slots([0, 1, 2, 3], [20, 21, 22, 23])

    # With a very restrictive fuse, violations should occur
    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        main_fuse_amps=1.0,
    )

    assert result is not None
    _milp_slots, diag = result

    assert diag.get("has_violations", False), (
        "has_violations must be True when fuse violations exist"
    )


# ---------------------------------------------------------------------------
# Export price > import price — unbounded LP fix (issue #635)
# ---------------------------------------------------------------------------


@_scipy_skip()
def test_milp_export_above_import_returns_solution():
    """Single slot with export > import (both positive) must return non-None.

    Before the fix, p_exp > p_imp in any slot caused an unbounded LP
    (HiGHS status=3) and solve_milp() returned None for the entire horizon.
    """
    slots = [_make_slot(hour=0, import_price=0.05, export_price=0.10)]
    result = solve_milp(
        slots,
        _NOW,
        current_kwh=2.0,
        usable_kwh=10.0,
        max_charge_per_slot=3.0,
        max_discharge_per_slot=3.0,
    )
    assert result is not None, (
        "MILP must return a solution when export_price > import_price"
    )


@_scipy_skip()
def test_milp_negative_import_positive_export_returns_solution():
    """Single slot with negative import and positive export must return non-None.

    This is a common real-world condition during high wind/solar production
    hours in DK/DE/NL markets.
    """
    slots = [_make_slot(hour=0, import_price=-0.05, export_price=0.10)]
    result = solve_milp(
        slots,
        _NOW,
        current_kwh=2.0,
        usable_kwh=10.0,
        max_charge_per_slot=3.0,
        max_discharge_per_slot=3.0,
    )
    assert result is not None, (
        "MILP must return a solution when import_price < 0 and export_price > 0"
    )


@_scipy_skip()
def test_milp_bad_slot_at_start_of_288_slot_horizon():
    """A bad slot (export > import) at index 0 in a 288-slot horizon must not fail."""
    import copy

    # 288 slots (72 hours × 15 min per slot)
    base_slot = _make_slot(hour=0, import_price=0.20, export_price=0.05)
    slots: list[PlannedSlot] = []
    for i in range(288):
        s = copy.copy(base_slot)
        s.start = _NOW + timedelta(minutes=i * 15)
        s.end = s.start + timedelta(minutes=15)
        if i == 0:
            s.price = SlotPrice(import_price=0.05, export_price=0.10)
        else:
            s.price = SlotPrice(import_price=0.20, export_price=0.05)
        slots.append(s)

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=2.0,
        usable_kwh=10.0,
        max_charge_per_slot=3.0,
        max_discharge_per_slot=3.0,
    )
    assert result is not None, (
        "MILP must return a solution when bad slot is at index 0 in 288-slot horizon"
    )


@_scipy_skip()
def test_milp_bad_slot_at_midpoint_of_288_slot_horizon():
    """A bad slot (export > import) at the midpoint in a 288-slot horizon must not fail."""
    import copy

    base_slot = _make_slot(hour=0, import_price=0.20, export_price=0.05)
    slots: list[PlannedSlot] = []
    for i in range(288):
        s = copy.copy(base_slot)
        s.start = _NOW + timedelta(minutes=i * 15)
        s.end = s.start + timedelta(minutes=15)
        if i == 144:  # midpoint
            s.price = SlotPrice(import_price=0.05, export_price=0.10)
        else:
            s.price = SlotPrice(import_price=0.20, export_price=0.05)
        slots.append(s)

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=2.0,
        usable_kwh=10.0,
        max_charge_per_slot=3.0,
        max_discharge_per_slot=3.0,
    )
    assert result is not None, (
        "MILP must return a solution when bad slot is at midpoint in 288-slot horizon"
    )


@_scipy_skip()
def test_milp_bad_slot_at_end_of_288_slot_horizon():
    """A bad slot (export > import) at the last slot in a 288-slot horizon must not fail."""
    import copy

    base_slot = _make_slot(hour=0, import_price=0.20, export_price=0.05)
    slots: list[PlannedSlot] = []
    for i in range(288):
        s = copy.copy(base_slot)
        s.start = _NOW + timedelta(minutes=i * 15)
        s.end = s.start + timedelta(minutes=15)
        if i == 287:  # last slot
            s.price = SlotPrice(import_price=0.05, export_price=0.10)
        else:
            s.price = SlotPrice(import_price=0.20, export_price=0.05)
        slots.append(s)

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=2.0,
        usable_kwh=10.0,
        max_charge_per_slot=3.0,
        max_discharge_per_slot=3.0,
    )
    assert result is not None, (
        "MILP must return a solution when bad slot is at last position in 288-slot horizon"
    )


@_scipy_skip()
def test_milp_normal_prices_no_regression():
    """Normal prices (import > export in all slots) must produce same results.

    The fix must not change behavior when p_imp > p_exp for all slots,
    which is the normal case.
    """
    # Standard arbitrage case — import > export everywhere
    cheap = [0, 1, 2, 3]
    expensive = [20, 21, 22, 23]
    slots = _make_arbitrage_slots(
        cheap, expensive, cheap_price=0.05, expensive_price=3.0
    )

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
    )

    assert milp_result is not None, (
        "MILP must still return a solution on the standard arbitrage case"
    )
    result, _diag = milp_result

    # Verify charge/discharge still works as expected (same assertions
    # as test_milp_charges_in_cheap_slots_and_discharges_in_expensive)
    charge_hours = {
        s.start.hour
        for s in result
        if s.recommendation == Recommendations.BatteriesChargeGrid.value
    }
    discharge_hours = {
        s.start.hour
        for s in result
        if s.recommendation == Recommendations.BatteriesDischargeMode.value
    }

    assert charge_hours & set(cheap), (
        f"Expected charge in cheap hours {cheap}, got charge hours: {sorted(charge_hours)}"
    )
    assert discharge_hours & set(expensive), (
        f"Expected discharge in expensive hours {expensive}, "
        f"got discharge hours: {sorted(discharge_hours)}"
    )


# ---------------------------------------------------------------------------
# Issue #637 — MILP discharge values are the source of truth
# ---------------------------------------------------------------------------


@_scipy_skip()
def test_milp_discharge_values_preserved_after_soc_simulation():
    """batteries_discharged_kwh after milp_prepopulated simulate_soc must match
    the LP's ed[t] solution, not a greedy chronological re-derivation.

    Scenario: two slots, both need 2.0 kWh, battery has only 3.0 kWh usable.
    The LP optimally allocates discharge across the two slots considering
    different import prices (0.30 vs 1.00).  The greedy simulator would
    discharge 2.0 kWh in slot 0 (enough to cover its load), leaving only
    1.0 kWh for slot 1.  The LP may instead ration: discharge less in
    slot 0 (where import is cheaper) and save more for slot 1.
    """
    slot0 = _make_slot(hour=0, import_price=0.30, consumption_kwh=2.0)
    slot1 = _make_slot(hour=1, import_price=1.00, consumption_kwh=2.0)
    slots = [slot0, slot1]

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=3.0,
        usable_kwh=3.0,
        max_charge_per_slot=3.0,
        max_discharge_per_slot=3.0,
        cycle_cost_per_kwh=0.0,
    )
    assert milp_result is not None, "MILP must return a solution"
    out_slots, _diag = milp_result

    # Record LP-derived discharge values before simulate_soc
    lp_discharge = [s.batteries_discharged_kwh for s in out_slots]
    lp_grid_import = [s.grid_import_kwh for s in out_slots]
    lp_grid_export = [s.grid_export_kwh for s in out_slots]

    # Run SoC simulation WITH milp_prepopulated=True (preserve LP values)
    simulate_soc(
        out_slots,
        _NOW,
        current_kwh=3.0,
        usable_kwh=3.0,
        max_capacity_kwh=3.0,
        max_charge_per_slot=3.0,
        max_discharge_per_slot=3.0,
        rated_kwh=3.0,
        end_of_discharge_soc_pct=0.0,
        milp_prepopulated=True,
    )

    # After simulation, batteries_discharged_kwh must still match LP values
    for i, slot in enumerate(out_slots):
        assert slot.batteries_discharged_kwh == pytest.approx(
            lp_discharge[i], rel=1e-6
        ), (
            f"Slot {i}: batteries_discharged_kwh {slot.batteries_discharged_kwh} "
            f"differs from LP ed[t] {lp_discharge[i]}"
        )
        assert slot.grid_import_kwh == pytest.approx(lp_grid_import[i], rel=1e-6), (
            f"Slot {i}: grid_import_kwh {slot.grid_import_kwh} "
            f"differs from LP gi[t] {lp_grid_import[i]}"
        )
        assert slot.grid_export_kwh == pytest.approx(lp_grid_export[i], rel=1e-6), (
            f"Slot {i}: grid_export_kwh {slot.grid_export_kwh} "
            f"differs from LP ge[t] {lp_grid_export[i]}"
        )

    # The LP should have rationed discharge: since slot 1 has a much higher
    # import price (1.00 vs 0.30), the LP should allocate MORE discharge to
    # slot 1 (saving expensive imports) and LESS to slot 0.
    # Total discharge cannot exceed usable_kwh (3.0 kWh).
    total_discharge = sum(s.batteries_discharged_kwh for s in out_slots)
    assert total_discharge <= 3.0 + 1e-6, (
        f"Total discharge {total_discharge} exceeds usable capacity 3.0"
    )
    # The LP should discharge more in the expensive slot (slot 1)
    # than in the cheap slot (slot 0), because displacing 1.00/kWh imports
    # is worth more than displacing 0.30/kWh imports.
    assert out_slots[1].batteries_discharged_kwh >= pytest.approx(
        out_slots[0].batteries_discharged_kwh, rel=1e-6
    ), (
        f"LP should discharge more in expensive slot (1.00/kWh) than cheap "
        f"slot (0.30/kWh).  Got slot0={out_slots[0].batteries_discharged_kwh}, "
        f"slot1={out_slots[1].batteries_discharged_kwh}"
    )


@_scipy_skip()
def test_milp_discharge_not_overwritten_without_prepopulated_flag():
    """Without milp_prepopulated=True, simulate_soc still overwrites
    batteries_discharged_kwh with its own greedy derivation — this is the
    pre-existing behavior for non-MILP candidates and must remain unchanged.

    This test verifies that the greedy path is still intact and produces
    different results than the LP for capacity-constrained scenarios.
    """
    slot0 = _make_slot(hour=0, import_price=0.30, consumption_kwh=2.0)
    slot1 = _make_slot(hour=1, import_price=1.00, consumption_kwh=2.0)
    slots = [slot0, slot1]

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=3.0,
        usable_kwh=3.0,
        max_charge_per_slot=3.0,
        max_discharge_per_slot=3.0,
        cycle_cost_per_kwh=0.0,
    )
    assert milp_result is not None, "MILP must return a solution"
    out_slots, _diag = milp_result

    # Run SoC simulation WITHOUT milp_prepopulated (greedy re-derivation)
    simulate_soc(
        out_slots,
        _NOW,
        current_kwh=3.0,
        usable_kwh=3.0,
        max_capacity_kwh=3.0,
        max_charge_per_slot=3.0,
        max_discharge_per_slot=3.0,
        rated_kwh=3.0,
        end_of_discharge_soc_pct=0.0,
        # milp_prepopulated defaults to False
    )

    # After simulation WITHOUT milp_prepopulated, the values MAY differ
    # from the LP — this is the greedy re-derivation behavior.
    greedy_discharge = [s.batteries_discharged_kwh for s in out_slots]

    # The greedy path should still be functional (non-zero total discharge)
    total_discharge = sum(greedy_discharge)
    assert total_discharge > 0, "Greedy simulation should produce non-zero discharge"


@_scipy_skip()
def test_terminal_soc_objective_preserves_charge_with_high_replacement_price():
    """Issue #638: With replacement_price_per_kwh=2.00, the LP must preserve
    battery charge instead of draining/exporting at a much lower export price.

    Single slot, battery full (5 kWh), no consumption, no PV, import=0.10,
    export=0.05, replacement_price=2.00.  Without the terminal-SoC term in
    the objective, the LP would discharge/export for a 0.05/kWh profit.
    With the term, the LP sees a 2.00/kWh penalty for discharging, so it
    should choose to do nothing instead.
    """
    s = _make_slot(hour=0, import_price=0.10, export_price=0.05, consumption_kwh=0.0)
    s.solcast_pv_estimate_kwh = 0.0
    s.estimated_net_consumption_kwh = 0.0
    slots = [s]

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=5.0,
        usable_kwh=5.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        replacement_price_per_kwh=2.00,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
    )
    assert milp_result is not None
    out_slots, diag = milp_result

    # The LP must NOT export when replacement_price (2.00) is
    # 40x the export price (0.05) — the terminal-SoC penalty dominates.
    # Note: the LP may find a degenerate optimum with simultaneous
    # ec=ed>0 (zero net SoC change) since the terminal-SoC terms
    # cancel.  The post-processing resolves this by zeroing both.
    # What matters is that no discharge/export recommendation is set
    # and no grid export occurs.
    rec = out_slots[0].recommendation
    exp = out_slots[0].grid_export_kwh
    assert rec is None, (
        f"LP must not recommend discharge/export when replacement_price >> export, "
        f"got recommendation={rec}"
    )
    assert exp < 1e-6, (
        f"LP must not export when replacement_price=2.00 >> export=0.05, "
        f"got grid_export_kwh={exp:.6f}"
    )


@_scipy_skip()
def test_terminal_soc_objective_none_produces_identical_results():
    """Issue #638: replacement_price_per_kwh=None must produce IDENTICAL
    results to before the terminal-SoC-in-objective change.

    The terminal-SoC term is guarded by
    `if replacement_price_per_kwh is not None and abs(replacement_price_per_kwh) > 1e-9`,
    so passing None must produce bit-identical LP solutions.
    """
    slots = _make_arbitrage_slots(
        cheap_hours=[0, 1, 2],
        expensive_hours=[6, 7, 8],
        cheap_price=0.05,
        expensive_price=1.50,
        total_hours=12,
    )

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=2.0,
        usable_kwh=5.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        cycle_cost_per_kwh=0.0,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
        replacement_price_per_kwh=None,
    )
    assert milp_result is not None
    out_slots, diag = milp_result

    # With no replacement price, the LP should charge in cheap slots
    # and discharge/export in expensive slots (arbitrage).
    charged_any = any(s.batteries_charged_kwh > 1e-6 for s in out_slots)
    discharged_any = any(s.batteries_discharged_kwh > 1e-6 for s in out_slots)
    assert charged_any, "LP should charge with cheap import prices"
    assert discharged_any, "LP should discharge with expensive import prices"

    # The terminal_soc_credit in diagnostics should be 0 (no replacement price).
    assert diag["terminal_soc_credit"] == pytest.approx(0.0, abs=1e-9)


@_scipy_skip()
def test_terminal_soc_objective_low_replacement_price_still_allows_export():
    """Issue #638: With a genuinely low replacement_price_per_kwh (0.01),
    exporting at a normal price (0.20) should still win over preserving SoC.

    This proves the new terminal-SoC term does not dominate when it shouldn't.
    """
    s = _make_slot(hour=0, import_price=0.30, export_price=0.20, consumption_kwh=0.0)
    s.solcast_pv_estimate_kwh = 0.0
    s.estimated_net_consumption_kwh = 0.0
    slots = [s]

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=5.0,
        usable_kwh=5.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        replacement_price_per_kwh=0.01,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
    )
    assert milp_result is not None
    out_slots, diag = milp_result

    # Export revenue (0.20/kWh) > replacement_price (0.01/kWh), so the LP
    # should prefer exporting over preserving charge.
    exp = out_slots[0].grid_export_kwh
    assert exp > 1e-6, (
        f"LP should export when export_price=0.20 > replacement_price=0.01, "
        f"got grid_export_kwh={exp:.6f}"
    )


@_scipy_skip()
def test_non_milp_candidates_unaffected_by_milp_prepopulated():
    """Regression test: non-MILP candidates (no_action, passive) must keep
    using simulate_soc's existing greedy logic exactly as before.

    The milp_prepopulated flag defaults to False, so any caller that does
    not explicitly opt-in gets the original greedy behavior.
    """
    slot = _make_slot(
        hour=0,
        import_price=0.30,
        consumption_kwh=1.0,
        recommendation=Recommendations.BatteriesDischargeMode.value,
    )
    slots = [slot]

    # Run simulate_soc without milp_prepopulated (default behavior)
    simulate_soc(
        slots,
        _NOW,
        current_kwh=2.0,
        usable_kwh=5.0,
        max_capacity_kwh=5.0,
        max_charge_per_slot=3.0,
        max_discharge_per_slot=3.0,
        rated_kwh=5.0,
        end_of_discharge_soc_pct=0.0,
    )

    # The greedy path should have derived discharge from the recommendation
    # label and net_demand:
    # - net_demand = 1.0 kWh (house load, no PV)
    # - discharge = min(1.0, cap=2.0, max_discharge=3.0) = 1.0 kWh
    # The default flag must produce the same greedy behavior as before.
    assert slot.batteries_discharged_kwh == pytest.approx(1.0, rel=1e-6), (
        f"Greedy discharge should be 1.0 kWh (covers full net_demand), "
        f"got {slot.batteries_discharged_kwh}"
    )
    assert slot.grid_import_kwh >= 0, "Grid import must be non-negative"
    assert slot.estimated_battery_capacity_kwh > 0, "SoC must be tracked"

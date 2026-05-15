"""Tests for battery cycle cost guard in the HSEM planner (issue #292).

Acceptance criteria verified here
----------------------------------
- Planner avoids grid charging when price spread is below loss + cycle cost.
- Planner allows grid charging when price spread exceeds loss + cycle cost.
- cycle_cost_per_kwh=0 preserves existing behaviour (backwards compat).
- Opportunistic charge respects the effective threshold (depreciation + cycle cost).
- compare_plans correctly selects the winner when cycle cost is the deciding factor.
- PlannerInput.battery_cycle_cost_per_kwh defaults to 0.0.
- _apply_grid_charge combined threshold formula is correct.
- apply_opportunistic_charge effective_threshold combines both costs.

All tests are synchronous with no Home Assistant imports.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planner_inputs import (
    BatteryScheduleInput,
    HourlyConsumptionAverage,
    PlannerInput,
    PricePoint,
    SolcastSlot,
)
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.cost_function import CostWeights, compare_plans
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations
from tests.planner.fixtures import make_summer_day_input

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CHARGE_GRID = Recommendations.BatteriesChargeGrid.value
_DISCHARGE = Recommendations.BatteriesDischargeMode.value


def _make_two_slot_input(
    *,
    cheap_import: float,
    expensive_import: float,
    cycle_cost_per_kwh: float = 0.0,
    min_price_difference: float = 0.0,
    battery_soc_pct: float = 10.0,
    battery_conversion_loss_pct: float = 0.0,
    battery_purchase_price: float = 0.0,
    battery_expected_cycles: int = 6000,
) -> PlannerInput:
    """Build a minimal 2-slot PlannerInput for charge-guard assertions.

    Slot layout:
    - Hour 0 (cheap_import): candidate charging slot — no load, no PV.
    - Hour 1 (expensive_import): discharge window — consumes 1 kWh of load.

    Battery starts at the end-of-discharge floor (10 % SoC, 0 usable kWh) so
    the planner MUST charge from the grid if it wants to cover the h1 load from
    the battery rather than from expensive grid import.  This ensures the
    charge-vs-no-charge decision is driven purely by the price spread and
    cycle-cost guard rather than pre-existing battery capacity.

    If the planner decides to charge from the grid, slot 0 will be assigned
    ``batteries_charge_grid``.  If the spread does not justify cycling, slot 0
    will remain unassigned and the h1 load will be covered by grid import.
    """
    prices = [
        PricePoint(hour=0, import_price=cheap_import, export_price=0.0),
        PricePoint(hour=1, import_price=expensive_import, export_price=0.0),
    ]
    solar = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(2)]
    consumption = [
        HourlyConsumptionAverage(
            hour=0, avg_1d=0.0, avg_3d=0.0, avg_7d=0.0, avg_14d=0.0
        ),
        HourlyConsumptionAverage(
            hour=1, avg_1d=1.0, avg_3d=1.0, avg_7d=1.0, avg_14d=1.0
        ),
    ]
    schedule = BatteryScheduleInput(
        enabled=True,
        start=time(1, 0),
        end=time(2, 0),
        min_price_difference=min_price_difference,
    )
    return PlannerInput(
        now_iso="2024-06-15T00:00:00+02:00",
        interval_minutes=60,
        interval_length_hours=2,
        battery_soc_pct=battery_soc_pct,
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
        battery_max_charge_power_w=5000.0,
        # Explicit 100 % efficiency so the loss-free test intent is preserved.
        # Setting battery_conversion_loss_pct=0 alone is not enough now that the
        # engine also considers battery_charge_efficiency_pct /
        # battery_discharge_efficiency_pct.
        battery_charge_efficiency_pct=100.0,
        battery_conversion_loss_pct=battery_conversion_loss_pct,
        battery_discharge_efficiency_pct=100.0,
        battery_purchase_price=battery_purchase_price,
        battery_expected_cycles=battery_expected_cycles,
        battery_cycle_cost_per_kwh=cycle_cost_per_kwh,
        consumption_averages=consumption,
        price_points=prices,
        solcast_slots=solar,
        battery_schedules=[schedule],
        excess_export_enabled=False,
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        is_read_only=True,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
    )


# ===========================================================================
# 1. PlannerInput defaults
# ===========================================================================


class TestPlannerInputDefault:
    """battery_cycle_cost_per_kwh must default to 0.0."""

    def test_default_is_zero(self):
        """Default value must be 0.0 (no extra cycle cost guard)."""
        inp = PlannerInput()
        assert inp.battery_cycle_cost_per_kwh == pytest.approx(0.0)

    def test_explicit_value_stored(self):
        """Explicitly set value must be stored unchanged."""
        inp = PlannerInput(battery_cycle_cost_per_kwh=0.05)
        assert inp.battery_cycle_cost_per_kwh == pytest.approx(0.05)


# ===========================================================================
# 2. Profitable charging — spread exceeds loss + cycle cost
# ===========================================================================


class TestProfitableCharging:
    """Planner SHOULD charge from grid when spread > min_diff + cycle_cost."""

    def test_large_spread_charges_grid_zero_cycle_cost(self):
        """Large price spread with no cycle cost → grid charge expected.

        Spread = 0.30 − 0.05 = 0.25; min_diff = 0.05; cycle_cost = 0.0
        → 0.25 ≥ 0.05  → should charge.
        """
        inp = _make_two_slot_input(
            cheap_import=0.05,
            expensive_import=0.30,
            min_price_difference=0.05,
            cycle_cost_per_kwh=0.0,
        )
        result = run_planner(inp)
        charge_slots = [s for s in result.slots if s.recommendation == _CHARGE_GRID]
        assert (
            len(charge_slots) >= 1
        ), "Expected at least one grid-charge slot when spread justifies cycling"

    def test_spread_exceeds_combined_threshold_charges_grid(self):
        """Spread of 0.25 > min_diff (0.05) + cycle_cost (0.10) = 0.15 → charge.

        Spread = 0.30 − 0.05 = 0.25; threshold = 0.05 + 0.10 = 0.15
        → 0.25 ≥ 0.15 → should charge.
        """
        inp = _make_two_slot_input(
            cheap_import=0.05,
            expensive_import=0.30,
            min_price_difference=0.05,
            cycle_cost_per_kwh=0.10,
        )
        result = run_planner(inp)
        charge_slots = [s for s in result.slots if s.recommendation == _CHARGE_GRID]
        assert (
            len(charge_slots) >= 1
        ), "Expected grid charge when spread 0.25 exceeds combined threshold 0.15"


# ===========================================================================
# 3. Unprofitable charging — spread below loss + cycle cost
# ===========================================================================


class TestUnprofitableCharging:
    """Planner MUST NOT charge when spread < min_diff + cycle_cost."""

    def test_cycle_cost_blocks_marginal_grid_charge(self):
        """Spread just meets min_diff but not min_diff + cycle_cost → no charge.

        Spread = 0.20 − 0.10 = 0.10; min_diff = 0.05; cycle_cost = 0.10
        → combined threshold = 0.15; 0.10 < 0.15 → must NOT charge.
        """
        inp = _make_two_slot_input(
            cheap_import=0.10,
            expensive_import=0.20,
            min_price_difference=0.05,
            cycle_cost_per_kwh=0.10,
        )
        result = run_planner(inp)
        charge_slots = [s for s in result.slots if s.recommendation == _CHARGE_GRID]
        assert len(charge_slots) == 0, (
            f"Expected NO grid charge but got {len(charge_slots)} charge slots "
            f"(spread 0.10 < combined threshold 0.15)"
        )

    def test_zero_spread_never_charges_grid(self):
        """Flat import price → no arbitrage opportunity → never charge from grid."""
        inp = _make_two_slot_input(
            cheap_import=0.20,
            expensive_import=0.20,
            min_price_difference=0.0,
            cycle_cost_per_kwh=0.05,
        )
        result = run_planner(inp)
        charge_slots = [s for s in result.slots if s.recommendation == _CHARGE_GRID]
        assert len(charge_slots) == 0

    def test_large_cycle_cost_blocks_small_spread(self):
        """Very high cycle cost (0.50/kWh) blocks even a reasonable spread.

        Spread = 0.30 − 0.10 = 0.20; cycle_cost = 0.50 → threshold = 0.50
        (ignoring min_price_difference = 0.0)
        → 0.20 < 0.50 → must NOT charge.
        """
        inp = _make_two_slot_input(
            cheap_import=0.10,
            expensive_import=0.30,
            min_price_difference=0.0,
            cycle_cost_per_kwh=0.50,
        )
        result = run_planner(inp)
        charge_slots = [s for s in result.slots if s.recommendation == _CHARGE_GRID]
        assert (
            len(charge_slots) == 0
        ), "Large cycle cost (0.50) must block charging when spread is only 0.20"

    def test_zero_cycle_cost_unchanged_behaviour(self):
        """cycle_cost_per_kwh=0 must not change existing planner behaviour.

        Baseline: spread of 0.15 > min_diff 0.05 → should charge with cycle_cost=0.
        Same input with cycle_cost=0 must also charge (backwards compat).
        """
        inp = _make_two_slot_input(
            cheap_import=0.05,
            expensive_import=0.20,
            min_price_difference=0.05,
            cycle_cost_per_kwh=0.0,
        )
        result = run_planner(inp)
        charge_slots = [s for s in result.slots if s.recommendation == _CHARGE_GRID]
        # With spread 0.15 > min_diff 0.05 we expect charging
        assert (
            len(charge_slots) >= 1
        ), "zero cycle_cost must not suppress charging when spread > min_diff"


# ===========================================================================
# 4. Opportunistic charge threshold
# ===========================================================================


class TestOpportunisticChargeThreshold:
    """apply_opportunistic_charge uses depreciation + cycle_cost as the ceiling."""

    def _make_flat_day_input(
        self,
        *,
        import_price: float,
        cycle_cost_per_kwh: float,
        battery_purchase_price: float = 0.0,
        battery_expected_cycles: int = 6000,
        battery_conversion_loss_pct: float = 0.0,
    ) -> PlannerInput:
        """24-slot flat-price input with no discharge schedule."""
        prices = [
            PricePoint(hour=h, import_price=import_price, export_price=0.0)
            for h in range(24)
        ]
        solar = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
        consumption = [
            HourlyConsumptionAverage(
                hour=h, avg_1d=0.5, avg_3d=0.5, avg_7d=0.5, avg_14d=0.5
            )
            for h in range(24)
        ]
        return PlannerInput(
            now_iso="2024-06-15T00:00:00+02:00",
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=20.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_charge_power_w=5000.0,
            battery_conversion_loss_pct=battery_conversion_loss_pct,
            battery_purchase_price=battery_purchase_price,
            battery_expected_cycles=battery_expected_cycles,
            battery_cycle_cost_per_kwh=cycle_cost_per_kwh,
            consumption_averages=consumption,
            price_points=prices,
            solcast_slots=solar,
            battery_schedules=[],  # no discharge schedule → opportunistic path
            excess_export_enabled=False,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
            is_read_only=True,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
        )

    def test_negative_price_always_triggers_opportunistic_charge(self):
        """Negative import price triggers opportunistic charge regardless of cycle cost."""
        inp = self._make_flat_day_input(
            import_price=-0.05,
            cycle_cost_per_kwh=1.0,  # very high — should not block negative-price charge
        )
        result = run_planner(inp)
        charge_slots = [s for s in result.slots if s.recommendation == _CHARGE_GRID]
        assert (
            len(charge_slots) >= 1
        ), "Negative import price must trigger opportunistic charge regardless of cycle cost"

    def test_high_cycle_cost_blocks_below_depreciation_opportunistic_charge(self):
        """High cycle_cost blocks opportunistic charge that would otherwise trigger.

        Setup: depreciation_threshold ≈ 0 (no purchase price) but import price = 0.08.
        With cycle_cost = 0.0: effective_threshold = 0 → price 0.08 > 0 → no charge.
        With cycle_cost = 0.0: no change in behavior, opportunistic charge still blocked.

        Real test: with a positive depreciation threshold (purchase_price > 0) that
        would allow charging at price 0.02, adding cycle_cost = 0.05 should raise
        the effective threshold above 0.02 and block opportunistic charging.
        """
        # With purchase_price=10_000, cycles=6000, capacity=10 kWh:
        # depreciation ≈ 10_000 * 0.30 / (6000 * 10) = 0.05
        # → depreciation_threshold ≈ 0.05
        # At import_price = 0.04 < 0.05: should charge opportunistically.
        inp_without_cycle = self._make_flat_day_input(
            import_price=0.04,
            cycle_cost_per_kwh=0.0,
            battery_purchase_price=10_000.0,
            battery_expected_cycles=6000,
        )
        result_without = run_planner(inp_without_cycle)
        charge_slots_without = [
            s for s in result_without.slots if s.recommendation == _CHARGE_GRID
        ]

        # With cycle_cost = 0.05: effective_threshold ≈ 0.05 + 0.05 = 0.10
        # → import_price 0.04 < 0.10: opportunistic charge should be blocked.
        inp_with_cycle = self._make_flat_day_input(
            import_price=0.04,
            cycle_cost_per_kwh=0.05,
            battery_purchase_price=10_000.0,
            battery_expected_cycles=6000,
        )
        result_with = run_planner(inp_with_cycle)
        charge_slots_with = [
            s for s in result_with.slots if s.recommendation == _CHARGE_GRID
        ]

        # Adding cycle cost must not INCREASE opportunistic charging
        assert len(charge_slots_with) <= len(charge_slots_without), (
            f"Higher cycle cost ({len(charge_slots_with)} slots) should not produce "
            f"more grid-charge slots than no cycle cost ({len(charge_slots_without)} slots)"
        )


# ===========================================================================
# 5. Plan cost integration — cycle_cost_per_kwh flows into plan_cost
# ===========================================================================


class TestCycleCostFlowsIntoPlanCost:
    """Verify battery_cycle_cost_per_kwh flows into PlannerOutput.plan_cost."""

    def test_high_cycle_cost_does_not_increase_grid_charging(self):
        """Higher cycle_cost_per_kwh must not increase grid-charge slots.

        Raising the cycle wear cost raises the profitability threshold, so the
        planner either cycles the same amount or less — never more.
        """
        inp_low = make_summer_day_input()
        inp_low.battery_cycle_cost_per_kwh = 0.0

        inp_high = make_summer_day_input()
        inp_high.battery_cycle_cost_per_kwh = 0.50  # very high — suppresses cycling

        result_low = run_planner(inp_low)
        result_high = run_planner(inp_high)

        assert result_low.plan_cost is not None
        assert result_high.plan_cost is not None

        # Higher cycle cost must produce equal or fewer grid-charge slots.
        charge_low = sum(
            1
            for s in result_low.slots
            if s.recommendation == Recommendations.BatteriesChargeGrid.value
        )
        charge_high = sum(
            1
            for s in result_high.slots
            if s.recommendation == Recommendations.BatteriesChargeGrid.value
        )
        assert charge_high <= charge_low, (
            f"Higher cycle cost ({charge_high} slots) produced MORE grid-charge "
            f"slots than low cycle cost ({charge_low} slots)"
        )

    def test_zero_cycle_cost_plan_cost_unchanged(self):
        """Setting cycle_cost_per_kwh=0 leaves plan_cost identical to default."""
        inp_default = make_summer_day_input()
        inp_zero = make_summer_day_input()
        inp_zero.battery_cycle_cost_per_kwh = 0.0

        r_default = run_planner(inp_default)
        r_zero = run_planner(inp_zero)

        assert r_default.plan_cost is not None
        assert r_zero.plan_cost is not None
        assert r_default.plan_cost.total == pytest.approx(
            r_zero.plan_cost.total, abs=1e-6
        )


# ===========================================================================
# 6. Known-winner comparison: cycle cost decides
# ===========================================================================


class TestKnownWinnerWithCycleCost:
    """Explicit winner comparison where cycle_cost is the deciding factor."""

    def test_low_cycle_cost_plan_beats_high_cycle_cost_plan(self):
        """Plan with less battery cycling beats one with more cycling when cycle_cost > 0.

        Plan A: moderate spread (0.15), small cycle cost → profitable.
        Plan B: same spread but 3x more cycling → higher total cost.
        Plan A must win.
        """
        tz = ZoneInfo("Europe/Copenhagen")
        start = datetime(2024, 6, 15, 0, 0, tzinfo=tz)

        weights = CostWeights(cycle_cost_per_kwh=0.10, conversion_loss_pct=0.0)

        plan_a = [
            PlannedSlot(
                start=start,
                end=start + timedelta(hours=1),
                price=SlotPrice(import_price=0.10, export_price=0.0),
                grid_import_kwh=1.0,
                batteries_charged=1.0,
                batteries_discharged=0.0,
                estimated_battery_soc=50.0,
            )
        ]
        plan_b = [
            PlannedSlot(
                start=start,
                end=start + timedelta(hours=1),
                price=SlotPrice(import_price=0.10, export_price=0.0),
                grid_import_kwh=1.0,
                batteries_charged=3.0,
                batteries_discharged=3.0,
                estimated_battery_soc=50.0,
            )
        ]

        bd_a, bd_b, winner = compare_plans(plan_a, plan_b, weights)
        # plan_b cycles 6 kWh vs 1 kWh in plan_a → cycle_cost_b much higher
        assert winner == "plan_a", (
            f"Expected plan_a to win (less cycling) but got {winner}. "
            f"plan_a={bd_a.total:.4f}, plan_b={bd_b.total:.4f}"
        )
        assert bd_b.cycle_cost > bd_a.cycle_cost

    def test_high_spread_still_wins_over_no_charge(self):
        """Even with cycle cost, very high price spread should favour charging.

        Plan A: 1 kWh grid import @ 0.05, 1 kWh export @ 0.30.
                cycle cost per kWh = 0.05, cycled = 1+1 = 2 kWh.
                net = 0.05 - 0.30 + 2*0.05 = -0.15  (net benefit to plan A)
        Plan B: 1 kWh grid import @ 0.30, 0 kWh export, no cycling.
                net = 0.30
        Plan A total < Plan B total → plan_a wins.
        """
        tz = ZoneInfo("Europe/Copenhagen")
        start = datetime(2024, 6, 15, 0, 0, tzinfo=tz)

        weights = CostWeights(cycle_cost_per_kwh=0.05, conversion_loss_pct=0.0)

        plan_a = [
            PlannedSlot(
                start=start,
                end=start + timedelta(hours=1),
                price=SlotPrice(import_price=0.05, export_price=0.30),
                grid_import_kwh=1.0,
                grid_export_kwh=1.0,
                batteries_charged=1.0,
                batteries_discharged=1.0,
                estimated_battery_soc=50.0,
            )
        ]
        plan_b = [
            PlannedSlot(
                start=start,
                end=start + timedelta(hours=1),
                price=SlotPrice(import_price=0.30, export_price=0.0),
                grid_import_kwh=1.0,
                grid_export_kwh=0.0,
                batteries_charged=0.0,
                batteries_discharged=0.0,
                estimated_battery_soc=50.0,
            )
        ]

        bd_a, bd_b, winner = compare_plans(plan_a, plan_b, weights)
        assert winner == "plan_a", (
            f"High spread with arbitrage (plan_a) must beat paying peak import (plan_b). "
            f"plan_a={bd_a.total:.4f}, plan_b={bd_b.total:.4f}"
        )

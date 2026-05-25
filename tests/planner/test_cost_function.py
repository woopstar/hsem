"""Tests for the HSEM planner cost function (issue #295).

Acceptance criteria verified here
----------------------------------
- Each candidate plan gets a numeric cost.
- Lower cost wins.
- Tests compare two candidate plans with known expected winner.
- All seven cost components are exercised individually.
- NaN prices are treated safely (no propagation).
- compare_plans helper returns correct winner.
- CostWeights.cycle_cost_per_kwh auto-calculation is correct.
- run_planner attaches plan_cost to PlannerOutput.

All tests are synchronous and import nothing from Home Assistant's runtime.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.cost_function import (
    CostWeights,
    PlanCostBreakdown,
    compare_plans,
    score_plan,
)
from custom_components.hsem.utils.prices import SlotPrice
from tests.planner.fixtures import (
    make_flat_price_input,
    make_summer_day_input,
    make_winter_day_input,
)

_TZ = ZoneInfo("Europe/Copenhagen")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_slot(
    *,
    hour: int = 0,
    import_price: float = 0.20,
    export_price: float = 0.05,
    grid_import_kwh: float = 0.0,
    grid_export_kwh: float = 0.0,
    batteries_charged_kwh: float = 0.0,
    batteries_discharged_kwh: float = 0.0,
    estimated_battery_soc_pct: float = 50.0,
    recommendation: str | None = None,
) -> PlannedSlot:
    """Build a single :class:`PlannedSlot` for unit tests."""
    start = datetime(2024, 6, 15, hour, 0, tzinfo=_TZ)
    return PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=import_price, export_price=export_price),
        grid_import_kwh=grid_import_kwh,
        grid_export_kwh=grid_export_kwh,
        batteries_charged_kwh=batteries_charged_kwh,
        batteries_discharged_kwh=batteries_discharged_kwh,
        estimated_battery_soc_pct=estimated_battery_soc_pct,
        recommendation=recommendation,
    )


def _make_day_of_slots(
    *,
    import_price: float = 0.20,
    export_price: float = 0.05,
    grid_import_kwh: float = 0.5,
    grid_export_kwh: float = 0.0,
    batteries_charged_kwh: float = 0.0,
    batteries_discharged_kwh: float = 0.0,
    estimated_battery_soc_pct: float = 50.0,
) -> list[PlannedSlot]:
    """Build 24 identical slots spanning a full day."""
    return [
        _make_slot(
            hour=h,
            import_price=import_price,
            export_price=export_price,
            grid_import_kwh=grid_import_kwh,
            grid_export_kwh=grid_export_kwh,
            batteries_charged_kwh=batteries_charged_kwh,
            batteries_discharged_kwh=batteries_discharged_kwh,
            estimated_battery_soc_pct=estimated_battery_soc_pct,
        )
        for h in range(24)
    ]


# ===========================================================================
# 1. Return-type contract
# ===========================================================================


class TestReturnTypeContract:
    """score_plan must always return a PlanCostBreakdown with a numeric total."""

    def test_returns_plan_cost_breakdown(self):
        """score_plan must return a PlanCostBreakdown instance."""
        result = score_plan([])
        assert isinstance(result, PlanCostBreakdown)

    def test_empty_slot_list_zero_cost(self):
        """An empty slot list must produce an all-zero breakdown."""
        bd = score_plan([])
        assert bd.total == pytest.approx(0.0)
        assert bd.import_cost == pytest.approx(0.0)
        assert bd.export_revenue == pytest.approx(0.0)
        assert bd.cycle_cost == pytest.approx(0.0)
        assert bd.soc_penalty == pytest.approx(0.0)
        assert bd.grid_limit_penalty == pytest.approx(0.0)
        assert bd.override_penalty == pytest.approx(0.0)

    def test_total_equals_sum_of_components(self):
        """total must equal the arithmetic sum of all components."""
        slots = _make_day_of_slots(
            grid_import_kwh=1.0,
            grid_export_kwh=0.5,
            batteries_charged_kwh=0.2,
            batteries_discharged_kwh=0.2,
        )
        weights = CostWeights(
            battery_purchase_price=10_000.0,
            battery_rated_capacity_kwh=10.0,
            battery_expected_cycles=6000,
            charge_efficiency_pct=90.0,
            discharge_efficiency_pct=90.0,
        )
        bd = score_plan(slots, weights)
        expected = (
            bd.import_cost
            - bd.export_revenue
            + bd.conversion_loss_cost
            + bd.cycle_cost
            + bd.soc_penalty
            + bd.grid_limit_penalty
            + bd.override_penalty
        )
        assert bd.total == pytest.approx(expected, abs=1e-9)


# ===========================================================================
# 2. Import cost component
# ===========================================================================


class TestImportCost:
    """Verify the import cost term is computed correctly."""

    def test_single_slot_pure_import(self):
        """1 kWh @ 0.30 → import_cost = 0.30."""
        slot = _make_slot(import_price=0.30, grid_import_kwh=1.0)
        bd = score_plan([slot], CostWeights())
        assert bd.import_cost == pytest.approx(0.30)
        assert bd.total == pytest.approx(0.30)

    def test_zero_import_no_cost(self):
        """No grid import → import_cost = 0."""
        slot = _make_slot(import_price=0.30, grid_import_kwh=0.0)
        bd = score_plan([slot], CostWeights())
        assert bd.import_cost == pytest.approx(0.0)

    def test_negative_import_price_reduces_cost(self):
        """Negative import price (surplus grid) reduces total cost."""
        slot = _make_slot(import_price=-0.05, grid_import_kwh=2.0)
        bd = score_plan([slot], CostWeights())
        # import_cost = 2.0 × −0.05 = −0.10
        assert bd.import_cost == pytest.approx(-0.10)
        assert bd.total < 0.0

    def test_multiple_slots_summed(self):
        """Import cost is accumulated across all slots."""
        slots = [
            _make_slot(hour=0, import_price=0.10, grid_import_kwh=2.0),
            _make_slot(hour=1, import_price=0.20, grid_import_kwh=3.0),
            _make_slot(hour=2, import_price=0.30, grid_import_kwh=1.0),
        ]
        bd = score_plan(slots, CostWeights())
        # 2×0.10 + 3×0.20 + 1×0.30 = 0.20 + 0.60 + 0.30 = 1.10
        assert bd.import_cost == pytest.approx(1.10)


# ===========================================================================
# 3. Export revenue component
# ===========================================================================


class TestExportRevenue:
    """Verify the export revenue term reduces total cost."""

    def test_single_slot_pure_export(self):
        """2 kWh @ 0.05 → export_revenue = 0.10, total = −0.10."""
        slot = _make_slot(export_price=0.05, grid_export_kwh=2.0)
        bd = score_plan([slot], CostWeights())
        assert bd.export_revenue == pytest.approx(0.10)
        assert bd.total == pytest.approx(-0.10)

    def test_zero_export_no_revenue(self):
        """No export → revenue = 0."""
        slot = _make_slot(export_price=0.10, grid_export_kwh=0.0)
        bd = score_plan([slot], CostWeights())
        assert bd.export_revenue == pytest.approx(0.0)

    def test_import_offset_by_export(self):
        """Import cost minus export revenue gives the net position."""
        slot = _make_slot(
            import_price=0.30,
            export_price=0.10,
            grid_import_kwh=1.0,
            grid_export_kwh=1.0,
        )
        bd = score_plan([slot], CostWeights())
        # import_cost = 0.30, export_revenue = 0.10 → net = 0.20
        assert bd.total == pytest.approx(0.20, abs=1e-6)

    def test_negative_export_price_makes_export_revenue_negative(self):
        """Negative export price → export_revenue is negative, increasing total cost.

        2 kWh exported at -0.05 → export_revenue = -0.10, total_cost = 0.10.
        """
        slot = _make_slot(export_price=-0.05, grid_export_kwh=2.0)
        bd = score_plan([slot], CostWeights())
        assert bd.export_revenue == pytest.approx(-0.10)
        assert bd.total_cost == pytest.approx(0.10)
        assert bd.score == pytest.approx(0.10)


class TestConversionLoss:
    """Verify conversion loss is computed from per-side efficiency losses."""

    def test_no_cycling_no_loss(self):
        """No battery activity → conversion_loss_cost = 0."""
        slot = _make_slot(
            import_price=0.20,
            export_price=0.05,
            batteries_charged_kwh=0.0,
            batteries_discharged_kwh=0.0,
        )
        bd = score_plan([slot], CostWeights())
        assert bd.conversion_loss_cost == pytest.approx(0.0)

    def test_charge_only_loss_computed(self):
        """1 kWh charged with 90 % efficiency → 0.1 kWh lost @ 0.20 = 0.02."""
        slot = _make_slot(
            import_price=0.20,
            export_price=0.05,
            batteries_charged_kwh=1.0,
            batteries_discharged_kwh=0.0,
        )
        bd = score_plan(
            [slot],
            CostWeights(charge_efficiency_pct=90.0, discharge_efficiency_pct=100.0),
        )
        # charge_loss_fraction = 1 - 0.90 = 0.10
        # lost_kwh = 1.0 × 0.10 = 0.10
        # cost = 0.10 × 0.20 = 0.02
        assert bd.conversion_loss_cost == pytest.approx(0.02, rel=1e-5)

    def test_discharge_only_loss_computed(self):
        """1 kWh discharged with 90 % efficiency → 0.1 kWh lost @ 0.20 = 0.02."""
        slot = _make_slot(
            import_price=0.20,
            export_price=0.05,
            batteries_charged_kwh=0.0,
            batteries_discharged_kwh=1.0,
        )
        bd = score_plan(
            [slot],
            CostWeights(charge_efficiency_pct=100.0, discharge_efficiency_pct=90.0),
        )
        # discharge_loss_fraction = 1 - 0.90 = 0.10
        # lost_kwh = 1.0 × 0.10 = 0.10
        # cost = 0.10 × 0.20 = 0.02
        assert bd.conversion_loss_cost == pytest.approx(0.02, rel=1e-5)

    def test_full_efficiency_disables_term(self):
        """100 % charge and discharge efficiency → conversion_loss_cost = 0."""
        slot = _make_slot(
            import_price=0.20,
            export_price=0.05,
            batteries_charged_kwh=5.0,
            batteries_discharged_kwh=5.0,
        )
        bd = score_plan(
            [slot],
            CostWeights(charge_efficiency_pct=100.0, discharge_efficiency_pct=100.0),
        )
        assert bd.conversion_loss_cost == pytest.approx(0.0)


# ===========================================================================
# 5. Battery cycle cost component
# ===========================================================================


class TestCycleCost:
    """Verify battery depreciation is computed from cycled energy."""

    def test_explicit_cycle_cost_per_kwh(self):
        """Explicit cycle_cost_per_kwh of 0.05 → max(1,1)=1 kWh cycled = 0.05."""
        slot = _make_slot(batteries_charged_kwh=1.0, batteries_discharged_kwh=1.0)
        bd = score_plan([slot], CostWeights(cycle_cost_per_kwh=0.05))
        assert bd.cycle_cost == pytest.approx(0.05, rel=1e-5)

    def test_auto_cycle_cost_from_economics(self):
        """Auto-derived cycle cost: 10000 / (2 × 10 × 6000) = 0.0833 per kWh.

        The 2× accounts for the max(charge, discharge) throughput counting
        which sees both directions of every full cycle.
        Sets min_soc_pct=0 so usable capacity equals rated capacity.
        """
        expected_per_kwh = 10_000.0 / (
            2.0 * 10.0 * 6000
        )  # 2× for throughput double-count
        slot = _make_slot(batteries_charged_kwh=1.0, batteries_discharged_kwh=0.0)
        bd = score_plan(
            [slot],
            CostWeights(
                cycle_cost_per_kwh=None,
                battery_purchase_price=10_000.0,
                battery_rated_capacity_kwh=10.0,
                battery_expected_cycles=6000,
                min_soc_pct=0.0,
                max_soc_pct=100.0,
            ),
        )
        assert bd.cycle_cost == pytest.approx(expected_per_kwh, rel=1e-5)

    def test_zero_purchase_price_disables_cycle_cost(self):
        """Zero battery price → cycle cost is 0."""
        slot = _make_slot(batteries_charged_kwh=3.0, batteries_discharged_kwh=3.0)
        bd = score_plan(
            [slot],
            CostWeights(
                cycle_cost_per_kwh=None,
                battery_purchase_price=0.0,
                battery_rated_capacity_kwh=10.0,
                battery_expected_cycles=6000,
            ),
        )
        assert bd.cycle_cost == pytest.approx(0.0)

    def test_zero_cycle_cost_weight_disables_term(self):
        """cycle_cost_per_kwh=0.0 disables the term entirely."""
        slot = _make_slot(batteries_charged_kwh=5.0, batteries_discharged_kwh=5.0)
        bd = score_plan([slot], CostWeights(cycle_cost_per_kwh=0.0))
        assert bd.cycle_cost == pytest.approx(0.0)


# ===========================================================================
# 6. SoC penalty component
# ===========================================================================


class TestSocPenalty:
    """Verify SoC guard penalties are quadratic in the violation magnitude."""

    def test_soc_within_bounds_no_penalty(self):
        """SoC in [min, max] → soc_penalty = 0."""
        slot = _make_slot(estimated_battery_soc_pct=50.0)
        bd = score_plan(
            [slot],
            CostWeights(min_soc_pct=10.0, max_soc_pct=100.0),
        )
        assert bd.soc_penalty == pytest.approx(0.0)

    def test_soc_below_min_penalty(self):
        """SoC 5 % below floor (10%) → violation=5 → penalty = weight × 25."""
        weights = CostWeights(
            min_soc_pct=10.0,
            soc_low_penalty_weight=0.01,
        )
        slot = _make_slot(estimated_battery_soc_pct=5.0)
        bd = score_plan([slot], weights)
        # violation = 10 - 5 = 5 pct → 0.01 × 5² = 0.25
        assert bd.soc_penalty == pytest.approx(0.25, rel=1e-5)

    def test_soc_above_max_penalty(self):
        """SoC 5 % above ceiling (95 %) → violation=5 → penalty = weight × 25."""
        weights = CostWeights(
            max_soc_pct=95.0,
            soc_high_penalty_weight=0.01,
        )
        slot = _make_slot(estimated_battery_soc_pct=100.0)
        bd = score_plan([slot], weights)
        # violation = 100 - 95 = 5 pct → 0.01 × 5² = 0.25
        assert bd.soc_penalty == pytest.approx(0.25, rel=1e-5)

    def test_zero_penalty_weight_disables_soc_check(self):
        """soc_low_penalty_weight=0 disables the low SoC check."""
        weights = CostWeights(
            min_soc_pct=50.0,
            soc_low_penalty_weight=0.0,
        )
        slot = _make_slot(estimated_battery_soc_pct=1.0)  # far below min
        bd = score_plan([slot], weights)
        assert bd.soc_penalty == pytest.approx(0.0)

    def test_soc_penalty_is_quadratic(self):
        """Doubling the violation quadruples the penalty."""
        weights = CostWeights(
            min_soc_pct=20.0,
            soc_low_penalty_weight=0.01,
            soc_high_penalty_weight=0.0,
        )
        slot_5pct = _make_slot(estimated_battery_soc_pct=15.0)  # violation = 5
        slot_10pct = _make_slot(estimated_battery_soc_pct=10.0)  # violation = 10
        bd5 = score_plan([slot_5pct], weights)
        bd10 = score_plan([slot_10pct], weights)
        assert bd10.soc_penalty == pytest.approx(bd5.soc_penalty * 4, rel=1e-5)


# ===========================================================================
# 7. Grid limit penalty component
# ===========================================================================


class TestGridLimitPenalty:
    """Verify the grid power-limit penalty is applied correctly."""

    def test_no_limit_configured_no_penalty(self):
        """grid_limit_kw=None → no penalty even for high import."""
        slot = _make_slot(grid_import_kwh=20.0)
        bd = score_plan([slot], CostWeights(grid_limit_kw=None))
        assert bd.grid_limit_penalty == pytest.approx(0.0)

    def test_import_within_limit_no_penalty(self):
        """Import power below limit → no penalty."""
        # 2 kWh in 1 h = 2 kW, limit = 5 kW → no violation
        slot = _make_slot(grid_import_kwh=2.0)
        bd = score_plan(
            [slot],
            CostWeights(grid_limit_kw=5.0, grid_limit_penalty_per_kwh=1.0),
            slot_duration_hours=1.0,
        )
        assert bd.grid_limit_penalty == pytest.approx(0.0)

    def test_import_exceeds_limit_penalty_applied(self):
        """Import power exceeds limit → penalty for excess energy."""
        # 10 kWh in 1 h = 10 kW, limit = 5 kW → excess = 5 kW × 1 h = 5 kWh
        # penalty = 5 kWh × 0.50 = 2.50
        slot = _make_slot(grid_import_kwh=10.0)
        bd = score_plan(
            [slot],
            CostWeights(grid_limit_kw=5.0, grid_limit_penalty_per_kwh=0.50),
            slot_duration_hours=1.0,
        )
        assert bd.grid_limit_penalty == pytest.approx(2.50, rel=1e-5)

    def test_export_exceeds_limit_penalty_applied(self):
        """Export power exceeds limit → penalty for excess energy."""
        slot = _make_slot(grid_export_kwh=8.0)
        bd = score_plan(
            [slot],
            CostWeights(grid_limit_kw=3.0, grid_limit_penalty_per_kwh=1.0),
            slot_duration_hours=1.0,
        )
        # excess = (8 - 3) × 1 h = 5 kWh × 1.0 = 5.0
        assert bd.grid_limit_penalty == pytest.approx(5.0, rel=1e-5)

    def test_grid_limit_via_keyword_override(self):
        """grid_limit_kw keyword arg overrides weights.grid_limit_kw."""
        slot = _make_slot(grid_import_kwh=10.0)
        bd = score_plan(
            [slot],
            CostWeights(grid_limit_kw=100.0, grid_limit_penalty_per_kwh=1.0),
            slot_duration_hours=1.0,
            grid_limit_kw=5.0,  # override: limit = 5 kW
        )
        # excess = (10 - 5) × 1 h × 1.0 = 5.0
        assert bd.grid_limit_penalty == pytest.approx(5.0, rel=1e-5)


# ===========================================================================
# 8. Override penalty component
# ===========================================================================


class TestOverridePenalty:
    """Verify forced-override slots accrue the override penalty."""

    def test_no_override_recommendation_no_penalty(self):
        """Normal recommendation (not override) → override_penalty = 0."""
        slot = _make_slot(recommendation="batteries_discharge_mode")
        bd = score_plan([slot], CostWeights(override_penalty_per_slot=0.05))
        assert bd.override_penalty == pytest.approx(0.0)

    def test_charge_grid_recommendation_is_override(self):
        """'batteries_charge_grid' is a forced schedule → override_penalty applied."""
        slot = _make_slot(recommendation="batteries_charge_grid")
        bd = score_plan([slot], CostWeights(override_penalty_per_slot=0.10))
        assert bd.override_penalty == pytest.approx(0.10)

    def test_override_penalty_accumulates_per_slot(self):
        """Three override slots → penalty × 3."""
        slots = [
            _make_slot(hour=h, recommendation="batteries_charge_grid") for h in range(3)
        ]
        bd = score_plan(slots, CostWeights(override_penalty_per_slot=0.05))
        assert bd.override_penalty == pytest.approx(0.15, rel=1e-5)

    def test_zero_override_weight_disables_term(self):
        """override_penalty_per_slot=0 disables the term."""
        slot = _make_slot(recommendation="batteries_charge_grid")
        bd = score_plan([slot], CostWeights(override_penalty_per_slot=0.0))
        assert bd.override_penalty == pytest.approx(0.0)


# ===========================================================================
# 9. NaN price safety
# ===========================================================================


class TestNanSafety:
    """NaN prices must be treated as 0.0 (no propagation into the total)."""

    def test_nan_import_price_treated_as_zero(self):
        """A slot with NaN import price must not produce NaN total."""
        import math

        slot = _make_slot(import_price=float("nan"), grid_import_kwh=2.0)
        bd = score_plan([slot], CostWeights())
        assert not math.isnan(bd.total)
        assert bd.import_cost == pytest.approx(0.0)

    def test_nan_export_price_treated_as_zero(self):
        """A slot with NaN export price must not produce NaN total."""
        import math

        slot = _make_slot(export_price=float("nan"), grid_export_kwh=2.0)
        bd = score_plan([slot], CostWeights())
        assert not math.isnan(bd.total)
        assert bd.export_revenue == pytest.approx(0.0)


# ===========================================================================
# 10. compare_plans helper — known winner tests
# ===========================================================================


class TestComparePlansKnownWinner:
    """The canonical acceptance criterion: lower-cost plan wins."""

    def test_cheaper_plan_wins_import_cost(self):
        """Plan with lower import price must win outright.

        Plan A: 10 kWh imported @ 0.10 → cost = 1.00
        Plan B: 10 kWh imported @ 0.30 → cost = 3.00
        Expected winner: plan_a.
        """
        plan_a = _make_day_of_slots(import_price=0.10, grid_import_kwh=1.0)
        plan_b = _make_day_of_slots(import_price=0.30, grid_import_kwh=1.0)
        bd_a, bd_b, winner = compare_plans(plan_a, plan_b)
        assert winner == "plan_a"
        assert bd_a.total < bd_b.total

    def test_export_revenue_makes_plan_cheaper(self):
        """Plan with export revenue must beat a plan that only imports.

        Plan A: 2 kWh imported @ 0.20, 1 kWh exported @ 0.10 → net = 0.30
        Plan B: 2 kWh imported @ 0.20, no export               → net = 0.40
        Expected winner: plan_a.
        """
        plan_a = [
            _make_slot(
                import_price=0.20,
                export_price=0.10,
                grid_import_kwh=2.0,
                grid_export_kwh=1.0,
            )
        ]
        plan_b = [
            _make_slot(
                import_price=0.20,
                export_price=0.10,
                grid_import_kwh=2.0,
                grid_export_kwh=0.0,
            )
        ]
        bd_a, bd_b, winner = compare_plans(plan_a, plan_b)
        assert winner == "plan_a"
        assert bd_a.total == pytest.approx(0.30, rel=1e-5)
        assert bd_b.total == pytest.approx(0.40, rel=1e-5)

    def test_excessive_cycling_increases_cost(self):
        """Plan with unnecessary battery cycling costs more due to depreciation.

        Plan A: no battery activity, 1 kWh import @ 0.20
        Plan B: 3 kWh cycled (max charge or discharge, not sum), same import
        Expected winner: plan_a (lower total because no cycle depreciation).
        """
        weights = CostWeights(
            cycle_cost_per_kwh=0.05,
            charge_efficiency_pct=100.0,
            discharge_efficiency_pct=100.0,
        )
        plan_a = [_make_slot(grid_import_kwh=1.0, import_price=0.20)]
        plan_b = [
            _make_slot(
                grid_import_kwh=1.0,
                import_price=0.20,
                batteries_charged_kwh=3.0,
                batteries_discharged_kwh=3.0,
            )
        ]
        bd_a, bd_b, winner = compare_plans(plan_a, plan_b, weights)
        assert winner == "plan_a"

    def test_soc_penalty_favours_plan_within_bounds(self):
        """Plan that keeps SoC in bounds beats one that violates the floor.

        Plan A: SoC = 50 % (well within [10, 100]) → no SoC penalty.
        Plan B: SoC = 5 % (below floor of 10 %) → SoC penalty applied.
        Expected winner: plan_a.
        """
        weights = CostWeights(
            min_soc_pct=10.0,
            soc_low_penalty_weight=0.05,
        )
        plan_a = [_make_slot(estimated_battery_soc_pct=50.0)]
        plan_b = [_make_slot(estimated_battery_soc_pct=5.0)]
        bd_a, bd_b, winner = compare_plans(plan_a, plan_b, weights)
        assert winner == "plan_a"
        assert bd_b.soc_penalty > 0.0

    def test_grid_limit_violation_increases_cost(self):
        """Plan that respects the grid limit beats one that violates it.

        Plan A: 3 kWh import (3 kW), limit = 5 kW → no violation.
        Plan B: 8 kWh import (8 kW), limit = 5 kW → 3 kW × 1 h penalty.
        Expected winner: plan_a.
        """
        weights = CostWeights(
            grid_limit_kw=5.0,
            grid_limit_penalty_per_kwh=1.0,
        )
        plan_a = [_make_slot(grid_import_kwh=3.0)]
        plan_b = [_make_slot(grid_import_kwh=8.0)]
        bd_a, bd_b, winner = compare_plans(
            plan_a, plan_b, weights, slot_duration_hours=1.0
        )
        assert winner == "plan_a"
        assert bd_b.grid_limit_penalty == pytest.approx(3.0, rel=1e-5)

    def test_tie_detected_when_costs_equal(self):
        """Two identical plans must be detected as a tie."""
        plan_a = _make_day_of_slots(grid_import_kwh=1.0, import_price=0.20)
        plan_b = _make_day_of_slots(grid_import_kwh=1.0, import_price=0.20)
        _, _, winner = compare_plans(plan_a, plan_b)
        assert winner == "tie"

    def test_combined_cost_terms_select_correct_winner(self):
        """A holistic comparison including import, export, cycle, and SoC terms.

        Plan A (battery-arbitrage plan):
          - 5 kWh cheap import @ 0.08 (charging)
          - 4 kWh export @ 0.06 (discharging)
          - 5 kWh charged + 5 kWh discharged (cycle wear)
          - SoC = 50 % (no SoC penalty)

        Plan B (do-nothing plan):
          - 5 kWh expensive import @ 0.30 (no battery)
          - 0 kWh export
          - No cycling, no SoC penalty

        With cheap-enough arbitrage (0.08 import vs 0.30 import), plan A
        should win despite the cycle depreciation cost.
        """
        weights = CostWeights(
            cycle_cost_per_kwh=0.02,
            charge_efficiency_pct=90.0,
            discharge_efficiency_pct=90.0,
            min_soc_pct=10.0,
            max_soc_pct=100.0,
        )
        plan_a = [
            _make_slot(
                import_price=0.08,
                export_price=0.06,
                grid_import_kwh=5.0,
                grid_export_kwh=4.0,
                batteries_charged_kwh=5.0,
                batteries_discharged_kwh=5.0,
                estimated_battery_soc_pct=50.0,
            )
        ]
        plan_b = [
            _make_slot(
                import_price=0.30,
                export_price=0.06,
                grid_import_kwh=5.0,
                grid_export_kwh=0.0,
                batteries_charged_kwh=0.0,
                batteries_discharged_kwh=0.0,
                estimated_battery_soc_pct=50.0,
            )
        ]
        bd_a, bd_b, winner = compare_plans(plan_a, plan_b, weights)
        # plan_a import: 5×0.08=0.40, export revenue: 4×0.06=0.24,
        # net import contribution: 0.40−0.24 = 0.16
        # plan_b import: 5×0.30=1.50; plan_a wins clearly
        assert winner == "plan_a"
        assert bd_a.total < bd_b.total

    def test_arbitrage_cycle_cost_no_double_count(self):
        """Regression: cycle cost counts throughput once per slot (max, not sum).

        Charge slot: 9 kWh charged @ 0.22 DKK, no discharge.
        Discharge slot: 9 kWh discharged @ 1.68 DKK, no charge.
        Usable capacity = 10 × (100−10)/100 = 9 kWh.
        cycle_cost_per_kwh (with 2× fix) = 25000 / (2 × 9 × 6000) ≈ 0.23148 DKK.
        Charged throughput counted = 9 kWh, discharged = 9 kWh.
        Each counted via max(charge, discharge) = 9 each = 18 total × 0.23148 ≈ 4.17.

        The arbitrage plan must be cheaper than a no-action plan importing
        9 kWh at 1.68 DKK (15.12 DKK).
        """
        charge_slot = _make_slot(
            hour=0,
            import_price=0.22,
            export_price=0.05,
            batteries_charged_kwh=9.0,
            batteries_discharged_kwh=0.0,
            grid_import_kwh=9.0 / 0.95,  # charge_stored / charge_eff
            grid_export_kwh=0.0,
            estimated_battery_soc_pct=50.0,
        )
        discharge_slot = _make_slot(
            hour=1,
            import_price=1.68,
            export_price=0.05,
            batteries_charged_kwh=0.0,
            batteries_discharged_kwh=9.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            estimated_battery_soc_pct=50.0,
        )
        no_action_slot = _make_slot(
            hour=0,
            import_price=1.68,
            export_price=0.05,
            batteries_charged_kwh=0.0,
            batteries_discharged_kwh=0.0,
            grid_import_kwh=9.0,
            grid_export_kwh=0.0,
            estimated_battery_soc_pct=50.0,
        )

        weights = CostWeights(
            battery_purchase_price=25_000.0,
            battery_rated_capacity_kwh=10.0,
            battery_expected_cycles=6000,
            min_soc_pct=10.0,
            max_soc_pct=100.0,
            charge_efficiency_pct=95.0,
            discharge_efficiency_pct=95.0,
            cycle_cost_per_kwh=None,
        )

        usable_kwh = 10.0 * (100.0 - 10.0) / 100.0  # 9.0
        expected_cycle_cost_per_kwh = 25_000.0 / (
            2.0 * usable_kwh * 6000
        )  # 2× for throughput double-count
        # max(9,0) + max(0,9) = 9 + 9 = 18 kWh throughput counted
        expected_cycle_cost = 18.0 * expected_cycle_cost_per_kwh

        bd_arbitrage = score_plan([charge_slot, discharge_slot], weights)
        bd_no_action = score_plan([no_action_slot], weights)

        assert bd_arbitrage.cycle_cost == pytest.approx(expected_cycle_cost, rel=1e-5)
        assert bd_arbitrage.total_cost < bd_no_action.total_cost


# ===========================================================================
# 11. Integration with run_planner
# ===========================================================================


class TestRunPlannerIntegration:
    """Verify that run_planner attaches plan_cost to the output."""

    def test_summer_day_has_plan_cost(self):
        """run_planner must populate plan_cost on a summer day input."""
        result = run_planner(make_summer_day_input())
        assert result.plan_cost is not None
        assert isinstance(result.plan_cost, PlanCostBreakdown)

    def test_winter_day_has_plan_cost(self):
        """run_planner must populate plan_cost on a winter day input."""
        result = run_planner(make_winter_day_input())
        assert result.plan_cost is not None

    def test_flat_price_has_plan_cost(self):
        """run_planner must populate plan_cost for a flat-price scenario."""
        result = run_planner(make_flat_price_input())
        assert result.plan_cost is not None

    def test_plan_cost_total_is_finite(self):
        """plan_cost.total must be a finite number (no NaN/Inf)."""
        import math

        result = run_planner(make_summer_day_input())
        assert result.plan_cost is not None
        assert math.isfinite(result.plan_cost.total)

    def test_plan_cost_components_non_negative_except_revenue(self):
        """All cost components except export_revenue must be ≥ 0."""
        result = run_planner(make_summer_day_input())
        assert result.plan_cost is not None
        bd = result.plan_cost
        assert bd.import_cost >= 0.0
        # export_revenue may be negative when export prices are negative
        # (curtailment penalty) — that's correct, not a bug.
        assert bd.conversion_loss_cost >= 0.0
        assert bd.cycle_cost >= 0.0
        assert bd.soc_penalty >= 0.0
        assert bd.grid_limit_penalty >= 0.0
        assert bd.override_penalty >= 0.0

    def test_summer_plan_total_equals_sum_of_components(self):
        """plan_cost.score must be consistent with all its components (issue #413).

        Money cost (``total_cost``) excludes synthetic penalties; the selector
        ``score`` adds them plus the terminal-SoC opportunity cost on top.
        """
        result = run_planner(make_summer_day_input())
        assert result.plan_cost is not None
        bd = result.plan_cost
        expected_total_cost = (
            bd.import_cost - bd.export_revenue + bd.conversion_loss_cost + bd.cycle_cost
        )
        expected_score = (
            expected_total_cost
            + bd.soc_penalty
            + bd.grid_limit_penalty
            + bd.override_penalty
            + bd.terminal_soc_value
        )
        assert bd.total_cost == pytest.approx(expected_total_cost, abs=1e-6)
        assert bd.score == pytest.approx(expected_score, abs=1e-6)
        # ``bd.total`` is a deprecated alias for ``bd.score``.
        assert bd.total == pytest.approx(bd.score, abs=1e-6)

    def test_high_price_plan_costs_more_than_low_price_plan(self):
        """A plan run on high-price days must have a higher cost than on cheap days."""
        from tests.planner.fixtures import make_flat_price_input

        cheap_result = run_planner(make_flat_price_input(import_price=0.05))
        expensive_result = run_planner(make_flat_price_input(import_price=0.50))

        assert cheap_result.plan_cost is not None
        assert expensive_result.plan_cost is not None
        # The expensive plan must have higher (or equal) total cost
        assert expensive_result.plan_cost.total >= cheap_result.plan_cost.total

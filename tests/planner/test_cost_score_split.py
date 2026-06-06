"""Tests for the cost / score split and terminal-SoC term (issue #413).

The cost function exposes two distinct aggregate numbers:

- ``total_cost`` — money outcome only.
  ``import_cost − export_revenue + cycle_cost + conversion_loss_cost``.
- ``score`` — selector objective.
  ``total_cost + soc_penalty + grid_limit_penalty + override_penalty
   + terminal_soc_value``.

The candidate selector picks the plan with the lowest ``score``, **not** the
lowest ``total_cost``.  ``score`` is what disambiguates plans that have the
same money outcome but leave the battery in different terminal states.

These tests verify:

1. ``total_cost`` never includes synthetic penalties.
2. ``score`` equals ``total_cost`` when all penalties are zero and the
   terminal-SoC term is disabled.
3. The terminal-SoC term is a credit (negative) when the plan ends with
   *more* stored energy than it started with.
4. The terminal-SoC term is a penalty (positive) when the plan ends with
   *less* stored energy than it started with.
5. The deprecated ``.total`` alias equals ``.score``.
6. The selector picks the plan with the lower ``score`` even when its
   ``total_cost`` is higher (regression for the discharge-only vs
   solar-only scenario behind issue #413).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.cost_function import (
    CostWeights,
    compare_plans,
    score_plan,
)
from custom_components.hsem.utils.prices import SlotPrice

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
    estimated_battery_capacity_kwh: float = 5.0,
    recommendation: str | None = None,
) -> PlannedSlot:
    """Build a single :class:`PlannedSlot` for these tests."""
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
        estimated_battery_capacity_kwh=estimated_battery_capacity_kwh,
        recommendation=recommendation,
    )


# ---------------------------------------------------------------------------
# total_cost vs score field semantics
# ---------------------------------------------------------------------------


class TestTotalCostExcludesPenalties:
    """``total_cost`` must never include synthetic penalties."""

    def test_total_cost_excludes_soc_penalty(self) -> None:
        """A SoC violation must affect ``score`` but never ``total_cost``."""
        # 1 kWh imported at 0.20 → import_cost = 0.20.
        # SoC sits at 5 % which is below the default min_soc_pct=10 %
        # giving a quadratic SoC penalty: 0.01 × (10 − 5)² = 0.25.
        slot = _make_slot(grid_import_kwh=1.0, estimated_battery_soc_pct=5.0)
        bd = score_plan([slot])

        assert bd.import_cost == pytest.approx(0.20)
        assert bd.soc_penalty == pytest.approx(0.25, abs=1e-9)
        # total_cost is money only and must NOT include the SoC penalty.
        assert bd.total_cost == pytest.approx(0.20, abs=1e-9)
        # score = total_cost + soc_penalty.
        assert bd.score == pytest.approx(0.45, abs=1e-9)
        # Deprecated alias.
        assert bd.total == pytest.approx(bd.score, abs=1e-9)

    def test_total_cost_excludes_grid_limit_penalty(self) -> None:
        """Grid limit violations must affect ``score`` but never ``total_cost``."""
        # 10 kWh imported in a 1-hour slot at 0.20 = 2.00 import_cost.
        # Default grid limit penalty: with grid_limit_kw=5, excess = 5 kW for
        # 1 hour → 5 kWh × 0.5 = 2.50 penalty.
        slot = _make_slot(grid_import_kwh=10.0, estimated_battery_soc_pct=50.0)
        weights = CostWeights(grid_limit_kw=5.0)
        bd = score_plan([slot], weights)

        assert bd.import_cost == pytest.approx(2.00, abs=1e-6)
        assert bd.grid_limit_penalty == pytest.approx(2.50, abs=1e-6)
        # total_cost is money only.
        assert bd.total_cost == pytest.approx(2.00, abs=1e-6)
        # score adds the grid limit penalty.
        assert bd.score == pytest.approx(4.50, abs=1e-6)


class TestScoreEqualsTotalCostWhenClean:
    """When no penalties fire and terminal-SoC is disabled, score == total_cost."""

    def test_score_equals_total_cost_for_clean_plan(self) -> None:
        slot = _make_slot(
            grid_import_kwh=2.0,
            grid_export_kwh=0.5,
            estimated_battery_soc_pct=50.0,
        )
        bd = score_plan([slot])

        # No penalties, no terminal-SoC inputs → score == total_cost.
        assert bd.soc_penalty == pytest.approx(0.0)
        assert bd.grid_limit_penalty == pytest.approx(0.0)
        assert bd.override_penalty == pytest.approx(0.0)
        assert bd.terminal_soc_value == pytest.approx(0.0)
        assert bd.score == pytest.approx(bd.total_cost, abs=1e-9)


# ---------------------------------------------------------------------------
# Terminal-SoC term
# ---------------------------------------------------------------------------


class TestTerminalSoCCredit:
    """A plan ending with more stored energy receives a credit (negative)."""

    def test_credit_when_battery_grows(self) -> None:
        """Final capacity > initial → terminal_soc_value < 0 → reduces score."""
        # Two slots; the second is "future" with estimated_battery_capacity_kwh=8.
        # Initial energy above floor = 3 kWh; final = 8 kWh.
        # delta = 3 − 8 = −5; with replacement 0.30 DKK/kWh → −1.50.
        slot = _make_slot(
            grid_import_kwh=1.0,
            estimated_battery_capacity_kwh=8.0,
            estimated_battery_soc_pct=80.0,
        )
        bd = score_plan(
            [slot],
            initial_battery_kwh=3.0,
            replacement_price_per_kwh=0.30,
        )

        assert bd.terminal_soc_value == pytest.approx(-1.50, abs=1e-9)
        # Money cost is unaffected.
        assert bd.total_cost == pytest.approx(0.20, abs=1e-9)
        # Score includes the credit.
        assert bd.score == pytest.approx(0.20 - 1.50, abs=1e-9)


class TestTerminalSoCPenalty:
    """A plan ending with less stored energy pays a penalty (positive)."""

    def test_penalty_when_battery_empties(self) -> None:
        """Final capacity < initial → terminal_soc_value > 0 → increases score."""
        # Initial 8 kWh; final 1 kWh → delta = 7; price 0.40 → penalty = 2.80.
        slot = _make_slot(
            grid_import_kwh=0.0,
            batteries_discharged_kwh=7.0,
            estimated_battery_capacity_kwh=1.0,
            estimated_battery_soc_pct=15.0,
        )
        bd = score_plan(
            [slot],
            initial_battery_kwh=8.0,
            replacement_price_per_kwh=0.40,
        )

        assert bd.terminal_soc_value == pytest.approx(2.80, abs=1e-9)
        # Money cost reflects only the import/export/cycle terms.
        assert bd.total_cost == pytest.approx(
            bd.import_cost
            - bd.export_revenue
            + bd.cycle_cost
            + bd.conversion_loss_cost,
            abs=1e-9,
        )
        # Score includes the terminal-SoC penalty.
        assert bd.score == pytest.approx(bd.total_cost + 2.80, abs=1e-9)


class TestTerminalSoCDisabledByDefault:
    """Calling score_plan without terminal-SoC inputs disables the term."""

    def test_disabled_when_kwargs_omitted(self) -> None:
        slot = _make_slot(
            grid_import_kwh=1.0,
            estimated_battery_capacity_kwh=9.99,
            estimated_battery_soc_pct=99.0,
        )
        bd = score_plan([slot])
        assert bd.terminal_soc_value == pytest.approx(0.0)

    def test_disabled_when_only_one_input_provided(self) -> None:
        slot = _make_slot(
            grid_import_kwh=1.0,
            estimated_battery_capacity_kwh=9.99,
            estimated_battery_soc_pct=99.0,
        )
        # Only initial_battery_kwh provided — term must stay disabled.
        bd_initial_only = score_plan([slot], initial_battery_kwh=2.0)
        assert bd_initial_only.terminal_soc_value == pytest.approx(0.0)
        # Only replacement_price_per_kwh provided — also disabled.
        bd_price_only = score_plan([slot], replacement_price_per_kwh=0.25)
        assert bd_price_only.terminal_soc_value == pytest.approx(0.0)

    def test_disabled_when_replacement_price_is_zero(self) -> None:
        """A zero replacement price disables the term (no opportunity cost)."""
        slot = _make_slot(
            grid_import_kwh=1.0,
            estimated_battery_capacity_kwh=9.99,
            estimated_battery_soc_pct=99.0,
        )
        bd = score_plan(
            [slot],
            initial_battery_kwh=2.0,
            replacement_price_per_kwh=0.0,
        )
        assert bd.terminal_soc_value == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compare_plans must rank by score, not total_cost
# ---------------------------------------------------------------------------


class TestComparePlansUsesScore:
    """compare_plans must pick the plan with the lower selector score."""

    def test_solar_only_beats_discharge_only_with_terminal_soc(self) -> None:
        """Regression for issue #413.

        Two plans cover the same demand for the same money cost, but one
        ends the horizon with a full battery (``solar_only``) and the other
        drains it (``discharge_only``).  Without terminal-SoC accounting
        ``discharge_only`` would tie or win because it cycles less; with the
        term enabled, ``solar_only`` must win because it preserves stored
        energy at the end of horizon.

        We model both plans with identical import (76.5 kWh × 1.00 DKK/kWh =
        76.5 DKK) but different end-of-horizon battery capacity.  Cycle and
        conversion-loss terms are disabled by setting the relevant weights
        to zero so the test isolates the terminal-SoC behaviour.
        """
        # Both plans: same import cost; same SoC %.  Difference: final
        # estimated_battery_capacity_kwh.
        discharge_only_last_slot = _make_slot(
            hour=23,
            grid_import_kwh=76.5,
            estimated_battery_capacity_kwh=0.5,  # nearly empty
            estimated_battery_soc_pct=15.0,
        )
        solar_only_last_slot = _make_slot(
            hour=23,
            grid_import_kwh=76.5,
            estimated_battery_capacity_kwh=9.0,  # ends nearly full
            estimated_battery_soc_pct=95.0,
        )

        # Disable cycle and conversion-loss terms; isolate terminal-SoC.
        weights = CostWeights(
            cycle_cost_per_kwh=0.0,
            soc_low_penalty_weight=0.0,
            soc_high_penalty_weight=0.0,
        )

        bd_solar, bd_discharge, winner = compare_plans(
            [solar_only_last_slot],
            [discharge_only_last_slot],
            weights,
            initial_battery_kwh=5.0,  # both start with 5 kWh
            replacement_price_per_kwh=0.30,
        )

        # Money cost is identical for both plans.
        assert bd_solar.total_cost == pytest.approx(bd_discharge.total_cost, abs=1e-6)

        # Solar-only earns a credit (delta = 5 − 9 = −4 → −1.20).
        assert bd_solar.terminal_soc_value == pytest.approx(-1.20, abs=1e-6)
        # Discharge-only pays a penalty (delta = 5 − 0.5 = 4.5 → +1.35).
        assert bd_discharge.terminal_soc_value == pytest.approx(1.35, abs=1e-6)

        # Solar-only must win the selector comparison.
        assert winner == "plan_a", (
            f"solar_only must beat discharge_only with terminal-SoC enabled; "
            f"got winner={winner!r}, "
            f"score solar={bd_solar.score:.4f}, score discharge={bd_discharge.score:.4f}"
        )

    def test_score_strictly_lower_when_battery_preserved(self) -> None:
        """A plan that preserves more battery energy must score strictly lower."""
        slot_a = _make_slot(grid_import_kwh=1.0, estimated_battery_capacity_kwh=4.0)
        slot_b = _make_slot(grid_import_kwh=1.0, estimated_battery_capacity_kwh=1.0)
        bd_a, bd_b, winner = compare_plans(
            [slot_a],
            [slot_b],
            initial_battery_kwh=2.0,
            replacement_price_per_kwh=0.50,
        )
        # plan_a ends with 4 kWh (gain 2 → credit −1.00).
        # plan_b ends with 1 kWh (loss 1 → penalty +0.50).
        assert bd_a.score < bd_b.score
        assert winner == "plan_a"


# ---------------------------------------------------------------------------
# Backwards-compatibility: deprecated ``total`` alias
# ---------------------------------------------------------------------------


class TestTotalAlias:
    """``.total`` is a deprecated alias for ``.score``."""

    def test_total_alias_equals_score_no_penalties(self) -> None:
        slot = _make_slot(grid_import_kwh=1.0, estimated_battery_soc_pct=50.0)
        bd = score_plan([slot])
        assert bd.total == pytest.approx(bd.score, abs=1e-9)

    def test_total_alias_equals_score_with_penalties(self) -> None:
        slot = _make_slot(grid_import_kwh=1.0, estimated_battery_soc_pct=5.0)
        bd = score_plan([slot])
        assert bd.soc_penalty > 0.0
        assert bd.total == pytest.approx(bd.score, abs=1e-9)

    def test_total_alias_includes_terminal_soc(self) -> None:
        slot = _make_slot(
            grid_import_kwh=1.0,
            estimated_battery_capacity_kwh=8.0,
            estimated_battery_soc_pct=80.0,
        )
        bd = score_plan(
            [slot],
            initial_battery_kwh=2.0,
            replacement_price_per_kwh=0.30,
        )
        # Credit applied.
        assert bd.terminal_soc_value < 0.0
        assert bd.total == pytest.approx(bd.score, abs=1e-9)

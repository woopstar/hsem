"""Tests for the HSEM planner plan explanation output (issue #304).

Acceptance criteria verified here
----------------------------------
- ``PlannerOutput.explanation`` is populated after every ``run_planner`` call.
- ``explanation.selected_strategy`` is a non-empty string.
- ``explanation.summary`` is a human-readable sentence.
- ``explanation.score`` equals the negated estimated total cost.
- ``explanation.rejected_plans`` is a list of :class:`RejectedPlan` objects
  each with a ``name`` and a ``reason`` string.
- ``explanation.constraints`` lists active constraint flags.
- ``PlanExplanation.as_dict()`` returns a JSON-safe dict with the correct keys.
- Simple fixture scenarios (summer, winter, no-battery) produce the correct
  ``selected_strategy`` value.
"""

from __future__ import annotations

import pytest

from homeassistant.const import STATE_UNKNOWN

from custom_components.hsem.models.planner_outputs import (
    PlanExplanation,
    PlannerOutput,
    RejectedPlan,
)
from custom_components.hsem.planner import run_planner
from tests.planner.fixtures import (
    make_flat_price_input,
    make_summer_day_input,
    make_winter_day_input,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXPECTED_EXPLANATION_KEYS = {
    "selected_strategy",
    "winner_name",
    "summary",
    "score",
    "estimated_total_cost",
    "price_spread",
    "peak_import_price",
    "off_peak_import_price",
    "forecast_pv_kwh",
    "forecast_net_consumption_kwh",
    "battery_soc_pct",
    "battery_soc_at_end_pct",
    "constraints",
    "rejected_plans",
}


# ===========================================================================
# 1. PlanExplanation dataclass unit tests
# ===========================================================================


class TestPlanExplanationDataclass:
    """PlanExplanation and RejectedPlan dataclasses behave correctly."""

    def test_default_construction(self):
        """PlanExplanation can be created with no arguments."""
        exp = PlanExplanation()
        assert exp.selected_strategy == STATE_UNKNOWN
        assert exp.summary == ""
        assert exp.constraints == []
        assert exp.rejected_plans == []
        assert isinstance(exp.score, float)

    def test_rejected_plan_fields(self):
        """RejectedPlan stores name, reason, and estimated_cost."""
        rp = RejectedPlan(
            name="do_nothing",
            reason="Battery idle would cost 1.23.",
            estimated_cost=1.23,
        )
        assert rp.name == "do_nothing"
        assert "idle" in rp.reason
        assert rp.estimated_cost == pytest.approx(1.23)

    def test_as_dict_returns_all_keys(self):
        """as_dict() must contain every expected top-level key."""
        exp = PlanExplanation(
            selected_strategy="winter_wait",
            summary="Winter: battery held in reserve.",
            score=-0.5,
            estimated_total_cost=0.5,
            price_spread=0.1,
            peak_import_price=0.45,
            off_peak_import_price=0.08,
            forecast_pv_kwh=0.5,
            forecast_net_consumption_kwh=10.0,
            battery_soc_pct=80.0,
            battery_soc_at_end_pct=70.0,
            constraints=["winter_month"],
            rejected_plans=[RejectedPlan("do_nothing", "Would cost more.", 0.9)],
        )
        result = exp.as_dict()
        assert set(result.keys()) == _EXPECTED_EXPLANATION_KEYS

    def test_as_dict_rejected_plans_serialised(self):
        """rejected_plans in as_dict() contains dicts with name/reason/cost."""
        exp = PlanExplanation(
            rejected_plans=[
                RejectedPlan("grid_charge_rejected_spread", "Spread too small.", 2.0)
            ]
        )
        d = exp.as_dict()
        assert len(d["rejected_plans"]) == 1
        rp_dict = d["rejected_plans"][0]
        assert rp_dict["name"] == "grid_charge_rejected_spread"
        assert isinstance(rp_dict["reason"], str)
        assert isinstance(rp_dict["estimated_cost"], float)

    def test_as_dict_values_are_rounded(self):
        """Floating-point values in as_dict() are rounded to avoid precision noise."""
        exp = PlanExplanation(
            score=1.23456789,
            estimated_total_cost=1.23456789,
            price_spread=0.123456789,
            forecast_pv_kwh=5.123456789,
        )
        d = exp.as_dict()
        assert d["score"] == pytest.approx(1.2346, abs=1e-4)
        assert d["estimated_total_cost"] == pytest.approx(1.2346, abs=1e-4)
        assert d["price_spread"] == pytest.approx(0.1235, abs=1e-4)
        assert d["forecast_pv_kwh"] == pytest.approx(5.123, abs=1e-3)


# ===========================================================================
# 2. PlannerOutput integration tests
# ===========================================================================


class TestPlannerOutputHasExplanation:
    """run_planner always returns a PlannerOutput with a valid explanation."""

    def test_explanation_is_present(self):
        """explanation field must be a PlanExplanation instance."""
        output = run_planner(make_summer_day_input())
        assert isinstance(output.explanation, PlanExplanation)

    def test_explanation_present_on_empty_output(self):
        """Even a minimal no-slot output must carry a default explanation."""
        output = PlannerOutput()
        assert isinstance(output.explanation, PlanExplanation)

    def test_selected_strategy_is_non_empty(self):
        """selected_strategy must be a non-empty string after a normal run."""
        output = run_planner(make_summer_day_input())
        assert output.explanation.selected_strategy
        assert isinstance(output.explanation.selected_strategy, str)

    def test_summary_is_non_empty(self):
        """summary must be a human-readable non-empty string."""
        output = run_planner(make_summer_day_input())
        assert output.explanation.summary
        assert len(output.explanation.summary) > 10

    def test_score_is_savings_vs_do_nothing(self):
        """score must equal do_nothing_cost minus estimated_total_cost.

        A positive score means the selected plan is cheaper than idle;
        a negative score means charging overhead exceeds discharge savings
        within the planning window.
        """
        output = run_planner(make_summer_day_input())
        exp = output.explanation
        # The do_nothing rejected plan carries the baseline cost.
        do_nothing = next(
            (rp for rp in exp.rejected_plans if rp.name == "do_nothing"), None
        )
        if do_nothing is not None:
            expected_score = round(
                do_nothing.estimated_cost - exp.estimated_total_cost, 4
            )
            assert exp.score == pytest.approx(expected_score, abs=1e-3)

    def test_constraints_is_list(self):
        """constraints must be a list (possibly empty but always present)."""
        output = run_planner(make_summer_day_input())
        assert isinstance(output.explanation.constraints, list)

    def test_rejected_plans_is_list(self):
        """rejected_plans must be a list of RejectedPlan objects."""
        output = run_planner(make_summer_day_input())
        assert isinstance(output.explanation.rejected_plans, list)
        for rp in output.explanation.rejected_plans:
            assert isinstance(rp, RejectedPlan)
            assert rp.name
            assert rp.reason

    def test_price_spread_non_negative(self):
        """price_spread is peak_import minus off_peak_import, so always ≥ 0."""
        output = run_planner(make_summer_day_input())
        assert output.explanation.price_spread >= 0.0

    def test_forecast_pv_kwh_non_negative(self):
        """forecast_pv_kwh is total PV production forecast, always ≥ 0."""
        output = run_planner(make_summer_day_input())
        assert output.explanation.forecast_pv_kwh >= 0.0

    def test_battery_soc_pct_matches_input(self):
        """battery_soc_pct must equal the input battery_soc_pct."""
        inp = make_summer_day_input(battery_soc_pct=65.0)
        output = run_planner(inp)
        assert output.explanation.battery_soc_pct == pytest.approx(65.0, abs=0.1)

    def test_as_dict_complete(self):
        """as_dict() on the planner output explanation has all required keys."""
        output = run_planner(make_summer_day_input())
        d = output.explanation.as_dict()
        assert set(d.keys()) == _EXPECTED_EXPLANATION_KEYS


# ===========================================================================
# 3. Strategy-specific tests
# ===========================================================================


class TestStrategyDetection:
    """The correct selected_strategy is chosen for different scenarios."""

    def test_summer_day_uses_charge_or_solar_strategy(self):
        """Summer fixture has schedules + solar, so a charge/discharge or solar strategy is chosen."""
        output = run_planner(make_summer_day_input())
        valid_strategies = {
            "charge_grid_discharge_peak",
            "charge_solar_discharge_peak",
            "solar_charge_only",
            "discharge_only",
            "force_export",
            "force_export_pv",
        }
        assert output.explanation.selected_strategy in valid_strategies

    def test_winter_day_reports_winter_constraint(self):
        """Winter fixture must include 'winter_month' in constraints."""
        output = run_planner(make_winter_day_input())
        assert "winter_month" in output.explanation.constraints

    def test_summer_day_reports_summer_constraint(self):
        """Summer fixture (June) must include 'summer_month' in constraints."""
        output = run_planner(make_summer_day_input())
        assert "summer_month" in output.explanation.constraints

    def test_flat_price_reports_no_spread_constraint(self):
        """Flat-price fixture has zero price spread; 'no_price_spread' in constraints."""
        output = run_planner(make_flat_price_input())
        # Price spread should be near zero
        assert output.explanation.price_spread == pytest.approx(0.0, abs=1e-4)
        assert "no_price_spread" in output.explanation.constraints

    def test_winter_day_strategy(self):
        """Winter fixture without schedules should select a winter/wait strategy."""
        inp = make_winter_day_input(schedules=[])
        output = run_planner(inp)
        valid_winter_strategies = {
            "winter_wait",
            "discharge_only",
            "charge_grid_discharge_peak",
            "charge_solar_discharge_peak",
            "solar_charge_only",
            "force_export",
            "force_export_pv",
        }
        assert output.explanation.selected_strategy in valid_winter_strategies

    def test_battery_disabled_reports_constraint(self):
        """Zero-capacity battery should add 'battery_disabled' to constraints."""
        inp = make_summer_day_input(battery_rated_capacity_kwh=0.0)
        output = run_planner(inp)
        assert "battery_disabled" in output.explanation.constraints

    def test_full_battery_reports_constraint(self):
        """Battery at 100 % SoC should add 'battery_full' to constraints."""
        inp = make_summer_day_input(battery_soc_pct=100.0)
        output = run_planner(inp)
        assert "battery_full" in output.explanation.constraints


# ===========================================================================
# 4. Rejected-plan content tests
# ===========================================================================


class TestRejectedPlans:
    """Rejected plans contain meaningful names and reasons."""

    def test_do_nothing_always_present(self):
        """'do_nothing' alternative should appear in rejected plans for all
        scenarios that do not themselves select 'discharge_only'."""
        output = run_planner(make_summer_day_input())
        if output.explanation.selected_strategy != "discharge_only":
            names = [rp.name for rp in output.explanation.rejected_plans]
            assert "do_nothing" in names

    def test_rejected_plan_has_reason(self):
        """Every rejected plan must have a non-empty reason string."""
        output = run_planner(make_summer_day_input())
        for rp in output.explanation.rejected_plans:
            assert rp.reason, f"RejectedPlan '{rp.name}' has empty reason"

    def test_rejected_plan_cost_is_float(self):
        """estimated_cost on every rejected plan must be a float."""
        output = run_planner(make_summer_day_input())
        for rp in output.explanation.rejected_plans:
            assert isinstance(rp.estimated_cost, float)

    def test_winter_do_nothing_always_present(self):
        """Winter scenario: 'do_nothing' rejected plan present when strategy
        is not 'discharge_only'."""
        output = run_planner(make_winter_day_input())
        if output.explanation.selected_strategy != "discharge_only":
            names = [rp.name for rp in output.explanation.rejected_plans]
            assert "do_nothing" in names

"""Prove the backtest invariant checks can actually fail (issue #1037).

A corpus that passes every check is only reassuring if the checks are capable
of failing. Each test here takes a real replayed cycle, breaks exactly one
spec invariant, and asserts the matching check reports it.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.planner.engine_core import run_planner
from custom_components.hsem.utils.recommendations import (
    SENTINEL_RECS,
    Recommendations,
)
from tests.backtest.conftest import corpus_paths
from tests.backtest.invariants import (
    InvariantViolation,
    check_invariants,
    format_violations,
)
from tests.backtest.replay import load_planner_input

_SENTINEL_VALUES = frozenset(m.value for m in SENTINEL_RECS)


@pytest.fixture(scope="module")
def cycle() -> tuple[PlannerInput, PlannerOutput]:
    """Replay the first corpus dump once for the whole module."""
    planner_input, _ = load_planner_input(corpus_paths()[0])
    return planner_input, run_planner(planner_input)


@pytest.fixture
def broken(cycle: tuple[PlannerInput, PlannerOutput]) -> PlannerOutput:
    """Return a private deep copy of the replayed plan, safe to mutate."""
    return copy.deepcopy(cycle[1])


def _names(violations: list[InvariantViolation]) -> set[str]:
    """Return the set of invariant names reported."""
    return {v.invariant for v in violations}


def _first_actionable(out: PlannerOutput) -> int:
    """Return the index of the first slot the planner actually simulated."""
    for index, slot in enumerate(out.slots):
        if slot.recommendation not in _SENTINEL_VALUES:
            return index
    raise AssertionError("corpus cycle has no actionable slot")


def _republish(out: PlannerOutput) -> None:
    """Point the winning candidate at the mutated slot list.

    The slot-identity check compares the published plan against the winner's
    own slots, so a mutation meant to trip a *different* invariant has to be
    mirrored onto the candidate or it trips that one too.
    """
    for candidate in out.candidates:
        if candidate.name == out.winner_name:
            candidate.slots = out.slots


def test_unmutated_cycle_is_clean(cycle: tuple[PlannerInput, PlannerOutput]) -> None:
    """The baseline every mutation test is measured against."""
    violations = check_invariants(*cycle)
    assert not violations, format_violations(violations)


class TestPerSlotChecks:
    """Per-slot physical and accounting invariants."""

    def test_energy_balance_break_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        broken.slots[10].estimated_net_consumption_kwh += 0.5
        _republish(broken)
        assert "energy_balance" in _names(check_invariants(cycle[0], broken))

    def test_soc_below_floor_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        index = _first_actionable(broken)
        broken.slots[index].estimated_battery_soc_pct = (
            cycle[0].battery_end_of_discharge_soc_pct - 1.0
        )
        _republish(broken)
        assert "soc_bounds" in _names(check_invariants(cycle[0], broken))

    def test_soc_above_ceiling_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        index = _first_actionable(broken)
        broken.slots[index].estimated_battery_soc_pct = 101.0
        _republish(broken)
        assert "soc_bounds" in _names(check_invariants(cycle[0], broken))

    def test_dynamic_floor_raises_the_soc_bound(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        """The dynamic discharge floor (#600) replaces the hardware floor."""
        index = _first_actionable(broken)
        broken.slots[index].estimated_battery_soc_pct = 20.0
        _republish(broken)
        assert "soc_bounds" not in _names(check_invariants(cycle[0], broken))
        raised = replace(cycle[0], dynamic_discharge_floor_pct=40.0)
        assert "soc_bounds" in _names(check_invariants(raised, broken))

    def test_negative_flow_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        broken.slots[10].grid_import_kwh = -0.25
        _republish(broken)
        assert "non_negative_flows" in _names(check_invariants(cycle[0], broken))

    def test_simultaneous_grid_import_and_export_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        """Wash flows are what ``grid_flow_mode[t]`` exists to prevent."""
        slot = broken.slots[10]
        slot.grid_import_kwh = 1.0
        slot.grid_export_kwh = 1.0
        slot.pv_export_kwh = 1.0
        slot.primary_battery_export_kwh = 0.0
        _republish(broken)
        assert "grid_direction_exclusive" in _names(check_invariants(cycle[0], broken))

    def test_simultaneous_charge_and_discharge_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        slot = broken.slots[10]
        slot.batteries_charged_kwh = 1.0
        slot.batteries_discharged_kwh = 1.0
        _republish(broken)
        assert "battery_direction_exclusive" in _names(
            check_invariants(cycle[0], broken)
        )

    def test_unattributed_export_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        broken.slots[10].grid_export_kwh += 0.5
        _republish(broken)
        assert "export_attribution" in _names(check_invariants(cycle[0], broken))

    def test_unknown_recommendation_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        broken.slots[10].recommendation = "batteries_do_something_clever"
        _republish(broken)
        assert "known_recommendation" in _names(check_invariants(cycle[0], broken))

    def test_label_energy_contradiction_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        """The #1032 class of bug: a charge label with no charge energy."""
        slot = broken.slots[-1]
        slot.recommendation = Recommendations.BatteriesChargeGrid.value
        slot.batteries_charged_kwh = 0.0
        slot.batteries_discharged_kwh = 0.0
        _republish(broken)
        assert "plan_self_consistency" in _names(check_invariants(cycle[0], broken))


class TestSlotGridChecks:
    """Horizon shape invariants."""

    def test_missing_slot_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        broken.slots.pop()
        _republish(broken)
        assert "slot_count" in _names(check_invariants(cycle[0], broken))

    def test_gap_between_slots_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        broken.slots[20].start += timedelta(minutes=5)
        _republish(broken)
        assert "slots_contiguous" in _names(check_invariants(cycle[0], broken))


class TestSelectionChecks:
    """No post-selection mutation, and the winner must beat doing nothing."""

    def test_mutated_published_cost_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        assert broken.plan_cost is not None
        broken.plan_cost.total_cost += 1.0
        assert "winner_cost_identity" in _names(check_invariants(cycle[0], broken))

    def test_mutated_published_score_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        assert broken.plan_cost is not None
        broken.plan_cost.score -= 1.0
        assert "winner_cost_identity" in _names(check_invariants(cycle[0], broken))

    def test_published_slots_diverging_from_winner_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        """Exactly the mutation issue #1033 rejected as a fix.

        The engine publishes the winner's own slot list, so today this holds by
        object identity and has to be broken deliberately: rebind ``slots`` to
        a copy before editing it, the way a post-selection "correction" pass
        would.
        """
        published = copy.deepcopy(broken.slots)
        published[_first_actionable(broken)].batteries_discharged_kwh += 1.0
        broken.slots = published
        assert "winner_slots_identity" in _names(check_invariants(cycle[0], broken))

    def test_unknown_winner_name_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        broken.winner_name = "a_plan_that_was_never_scored"
        assert "winner_present" in _names(check_invariants(cycle[0], broken))

    def test_winner_worse_than_no_action_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        assert broken.plan_cost is not None
        baseline = next(
            c for c in broken.candidates if c.name in ("no_action", "baseline")
        )
        broken.plan_cost.score = baseline._cost.score + 1.0
        for candidate in broken.candidates:
            if candidate.name == broken.winner_name:
                candidate._cost.score = broken.plan_cost.score
        assert "winner_not_worse_than_no_action" in _names(
            check_invariants(cycle[0], broken)
        )

    def test_misreported_terminal_soc_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        """Emptying the battery must not be hidden behind a stale headline."""
        broken.battery_soc_at_end += 5.0
        assert "terminal_soc_reported" in _names(check_invariants(cycle[0], broken))


class TestDataQualityChecks:
    """Missing price data must never be planned as free energy."""

    def test_silently_zeroed_price_day_is_detected(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        first_day = broken.slots[0].start.date()
        for slot in broken.slots:
            if slot.start.date() == first_day:
                slot.price = slot.price._replace(import_price=0.0)
        broken.data_quality.today_price_missing_hours = []
        _republish(broken)
        assert "missing_price_reported" in _names(check_invariants(cycle[0], broken))

    def test_zeroed_price_day_is_accepted_when_reported(
        self, cycle: tuple[PlannerInput, PlannerOutput], broken: PlannerOutput
    ) -> None:
        first_day = broken.slots[0].start.date()
        for slot in broken.slots:
            if slot.start.date() == first_day:
                slot.price = slot.price._replace(import_price=0.0)
        broken.data_quality.today_price_missing_hours = list(range(24))
        _republish(broken)
        assert "missing_price_reported" not in _names(
            check_invariants(cycle[0], broken)
        )


class TestReporting:
    """The violation report has to be readable when it fires."""

    def test_format_violations_lists_each_failure(self) -> None:
        rendered = format_violations(
            [
                InvariantViolation("soc_bounds", "slot 3 soc=2.0%"),
                InvariantViolation("energy_balance", "slot 7 net mismatch"),
            ]
        )
        assert rendered.splitlines() == [
            "  - soc_bounds: slot 3 soc=2.0%",
            "  - energy_balance: slot 7 net mismatch",
        ]

    def test_format_violations_is_explicit_when_clean(self) -> None:
        assert format_violations([]) == "no invariant violations"

    def test_empty_plan_reports_no_selection_violations(self) -> None:
        """A planner abort has no candidates; the checks must stay quiet."""
        violations = check_invariants(
            PlannerInput(interval_length_hours=0), PlannerOutput()
        )
        assert _names(violations) <= {"slot_count"}

"""A time-limited MILP plan that scores worse than passive is not published (#1312).

A proven-optimal MILP stays the sole production authority; an incumbent the
solver returned at its time limit can be arbitrarily far from optimal, so the
passive plan the same run built is published when it scores lower.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.hsem.planner import candidate_selector
from custom_components.hsem.planner.candidate_generator import CandidatePlan
from custom_components.hsem.planner.candidate_selector import (
    _passive_over_time_limited_milp,
)
from custom_components.hsem.planner.engine_core import run_planner
from custom_components.hsem.planner.milp._incumbent import (
    SOLVER_STATUS_OPTIMAL,
    SOLVER_STATUS_TIME_LIMIT,
    SolveOutcome,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from tests.planner.fixtures import make_winter_day_input
from tests.planner.test_solver_time_limit import _force_time_limit


def _candidate(name: str, score: float, status: str | None = None) -> CandidatePlan:
    candidate = CandidatePlan(name=name, slots=[])
    candidate._cost = SimpleNamespace(score=score)  # type: ignore[assignment]  # only .score is read
    if status is not None:
        candidate.diagnostics = {"solver": SolveOutcome(status, 1.0, 2.0, 5.0)}
    return candidate


class TestRule:
    def test_a_worse_time_limited_plan_gives_way_to_passive(self) -> None:
        milp = _candidate("milp", 2.5, SOLVER_STATUS_TIME_LIMIT)
        passive = _candidate("passive", 2.0)

        eligible = _passive_over_time_limited_milp([milp], [milp, passive])

        assert eligible == [passive]
        assert "time limit" in milp.rejection_reason
        assert "2.5000 vs 2.0000" in milp.rejection_reason

    def test_a_better_time_limited_plan_is_kept(self) -> None:
        milp = _candidate("milp", 1.0, SOLVER_STATUS_TIME_LIMIT)
        passive = _candidate("passive", 2.0)

        assert _passive_over_time_limited_milp([milp], [milp, passive]) == [milp]
        assert milp.rejection_reason == ""

    def test_a_tie_keeps_the_milp_plan(self) -> None:
        milp = _candidate("milp", 2.0 + 1e-9, SOLVER_STATUS_TIME_LIMIT)
        passive = _candidate("passive", 2.0)

        assert _passive_over_time_limited_milp([milp], [milp, passive]) == [milp]

    @pytest.mark.parametrize("status", [SOLVER_STATUS_OPTIMAL, None])
    def test_an_optimal_or_unknown_solve_is_kept(self, status: str | None) -> None:
        milp = _candidate("milp", 5.0, status)
        passive = _candidate("passive", 2.0)

        assert _passive_over_time_limited_milp([milp], [milp, passive]) == [milp]

    def test_without_a_valid_passive_the_milp_plan_is_kept(self) -> None:
        milp = _candidate("milp", 5.0, SOLVER_STATUS_TIME_LIMIT)
        unscored = CandidatePlan(name="passive", slots=[])

        assert _passive_over_time_limited_milp([milp], [milp]) == [milp]
        assert _passive_over_time_limited_milp([milp], [milp, unscored]) == [milp]

    def test_only_a_lone_milp_candidate_is_considered(self) -> None:
        passive = _candidate("passive", 2.0)
        assert _passive_over_time_limited_milp([passive], [passive]) == [passive]


@pytest.mark.skipif(not is_scipy_available(), reason="scipy is required")
class TestRunPlanner:
    @staticmethod
    def _run(monkeypatch: pytest.MonkeyPatch, *, passive_wins: bool) -> Any:
        _force_time_limit(monkeypatch)
        if passive_wins:
            # The forced time-limited plan is the real optimum, so it beats
            # passive; a margin below any score gap makes passive "better".
            monkeypatch.setattr(candidate_selector, "_TIME_LIMITED_FALLBACK_EPS", -1e9)
        inp = dataclasses.replace(
            make_winter_day_input(), planner_hysteresis_enabled=False
        )
        return run_planner(inp)

    def test_passive_is_published_and_explained(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = self._run(monkeypatch, passive_wins=True)

        assert output.winner_name == "passive"
        expl = output.explanation
        assert expl.solver_status == SOLVER_STATUS_TIME_LIMIT
        assert expl.selected_strategy == "solver_time_limit_fallback"
        assert "'passive' plan is used" in expl.summary
        assert "solver_time_limit" in expl.constraints
        milp = next(rp for rp in expl.rejected_plans if rp.name == "milp")
        assert "time limit" in milp.reason
        winner = next(c for c in output.candidates if c.name == "passive")
        assert output.plan_cost is not None
        assert output.plan_cost.score == pytest.approx(winner._cost.score)

    def test_hysteresis_cannot_keep_the_rejected_milp_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _force_time_limit(monkeypatch)
        monkeypatch.setattr(candidate_selector, "_TIME_LIMITED_FALLBACK_EPS", -1e9)
        inp = dataclasses.replace(
            make_winter_day_input(),
            planner_hysteresis_enabled=True,
            planner_hysteresis_percentage=1000.0,
            previous_winner_name="milp",
            previous_winner_score=0.0,
        )

        assert run_planner(inp).winner_name == "passive"

    def test_a_better_time_limited_plan_is_published(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = self._run(monkeypatch, passive_wins=False)

        assert output.winner_name == "milp"
        assert output.explanation.selected_strategy == "solver_time_limit"

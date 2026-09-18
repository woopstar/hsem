"""Tests for the selector's fail-closed fallback and percentage hysteresis.

Two paths matter here. When every candidate fails SoC validation the selector
must still publish an executable plan, and that plan must be ``passive`` —
``no_action`` is a diagnostic floor that must never become executable
(issue #897). Separately, plan-level hysteresis has a percentage threshold that
`test_hysteresis.py` only asserts conditionally; these tests pin it down.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.candidate_generator import (
    CANDIDATE_MILP,
    CANDIDATE_NO_ACTION,
    CANDIDATE_PASSIVE,
    CandidatePlan,
)
from custom_components.hsem.planner.candidate_selector import select_best_candidate
from custom_components.hsem.planner.cost_function import CostWeights
from custom_components.hsem.planner.cost_types import PlanCostBreakdown
from custom_components.hsem.utils.prices import SlotPrice

_MODULE = "custom_components.hsem.planner.candidate_selector"
_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_SLOT = timedelta(hours=1)


def _candidate(name: str) -> CandidatePlan:
    """Return a candidate with one future slot of its own."""
    slot = PlannedSlot(
        start=_NOW + _SLOT,
        end=_NOW + 2 * _SLOT,
        price=SlotPrice(2.0, 0.5),
        avg_house_consumption_kwh=1.0,
    )
    return CandidatePlan(name=name, slots=[slot])


def _select(candidates: list[CandidatePlan], **overrides: Any) -> Any:
    """Call ``select_best_candidate`` with a minimal viable battery model."""
    kwargs: dict[str, Any] = {
        "now": _NOW,
        "current_kwh": 4.5,
        "usable_kwh": 9.0,
        "max_soc_capacity_kwh": 9.0,
        "max_charge_per_slot": 1.25,
        "max_discharge_per_slot": None,
        "rated_kwh": 10.0,
        "end_of_discharge_soc_pct": 10.0,
        "cost_weights": CostWeights(),
        "slot_duration_hours": 1.0,
    }
    kwargs.update(overrides)
    return select_best_candidate(candidates, **kwargs)


def _all_invalid() -> Any:
    """Return a ``_validate_candidate`` stub that rejects every candidate."""
    return lambda _candidate, _floor: (False, "SoC below floor")


class TestFailClosedFallback:
    """With no eligible candidate the selector still publishes a safe plan."""

    def test_falls_back_to_passive_and_marks_it_executable(self) -> None:
        """Passive wins, is marked valid, and the fallback is logged."""
        milp = _candidate(CANDIDATE_MILP)
        passive = _candidate(CANDIDATE_PASSIVE)
        no_action = _candidate(CANDIDATE_NO_ACTION)

        with (
            patch(f"{_MODULE}._validate_candidate", _all_invalid()),
            patch(f"{_MODULE}.log_planner") as log,
        ):
            winner, _rejected, _hysteresis = _select([milp, passive, no_action])

        assert winner is passive
        assert winner.is_valid is True
        assert winner.rejection_reason == ""
        assert any(
            "No eligible candidates" in call.args[1]
            for call in log.call_args_list
            if len(call.args) > 1
        )

    def test_no_action_is_never_the_fallback(self) -> None:
        """Without a passive candidate, any other plan beats ``no_action``."""
        milp = _candidate(CANDIDATE_MILP)
        no_action = _candidate(CANDIDATE_NO_ACTION)

        with patch(f"{_MODULE}._validate_candidate", _all_invalid()):
            winner, _rejected, _hysteresis = _select([no_action, milp])

        assert winner is milp
        assert winner.name != CANDIDATE_NO_ACTION

    def test_only_a_diagnostic_candidate_is_an_error(self) -> None:
        """``no_action`` alone leaves nothing executable to publish."""
        no_action = _candidate(CANDIDATE_NO_ACTION)

        with (
            patch(f"{_MODULE}._validate_candidate", _all_invalid()),
            pytest.raises(RuntimeError, match="No candidates available"),
        ):
            _select([no_action])


_CANDIDATE_OTHER = "other"


class TestPercentageHysteresis:
    """A relative improvement below the percentage threshold keeps the plan.

    A validated MILP candidate is the sole eligible plan when present, so
    hysteresis can only compare two non-MILP candidates — the same shape
    ``test_hysteresis.py`` uses.
    """

    @staticmethod
    def _score_stub(mapping: dict[int, float]) -> Any:
        """Return a ``score_plan`` stub resolving scores by slot-list identity."""

        def _score(slots: Any, *_args: Any, **_kwargs: Any) -> PlanCostBreakdown:
            value = mapping[id(slots)]
            return PlanCostBreakdown(score=value, total_cost=value, total=value)

        return _score

    def test_small_relative_improvement_keeps_the_previous_plan(self) -> None:
        """A 4.76 % improvement is below a 10 % threshold, so nothing switches."""
        new_plan = _candidate(_CANDIDATE_OTHER)
        previous = _candidate(CANDIDATE_PASSIVE)
        scores = {id(new_plan.slots): 10.0, id(previous.slots): 10.5}

        with (
            patch(f"{_MODULE}.score_plan", self._score_stub(scores)),
            patch(f"{_MODULE}._validate_candidate", lambda _c, _f: (True, "")),
        ):
            winner, _rejected, hysteresis = _select(
                [new_plan, previous],
                hysteresis_enabled=True,
                hysteresis_absolute=0.0,
                hysteresis_percentage=10.0,
                previous_winner_name=CANDIDATE_PASSIVE,
                previous_winner_score=10.5,
            )

        assert winner is previous
        assert hysteresis.applied is True
        assert "percentage threshold" in hysteresis.reason
        assert "4.76%" in hysteresis.reason

    def test_large_relative_improvement_switches_plans(self) -> None:
        """A 50 % improvement clears a 10 % threshold."""
        new_plan = _candidate(_CANDIDATE_OTHER)
        previous = _candidate(CANDIDATE_PASSIVE)
        scores = {id(new_plan.slots): 5.0, id(previous.slots): 10.0}

        with (
            patch(f"{_MODULE}.score_plan", self._score_stub(scores)),
            patch(f"{_MODULE}._validate_candidate", lambda _c, _f: (True, "")),
        ):
            winner, _rejected, hysteresis = _select(
                [new_plan, previous],
                hysteresis_enabled=True,
                hysteresis_absolute=0.0,
                hysteresis_percentage=10.0,
                previous_winner_name=CANDIDATE_PASSIVE,
                previous_winner_score=10.0,
            )

        assert winner is new_plan
        assert hysteresis.applied is False

    def test_a_zero_previous_score_cannot_be_compared_relatively(self) -> None:
        """A zero baseline has no percentage to measure against."""
        new_plan = _candidate(_CANDIDATE_OTHER)
        previous = _candidate(CANDIDATE_PASSIVE)
        scores = {id(new_plan.slots): -1.0, id(previous.slots): 0.0}

        with (
            patch(f"{_MODULE}.score_plan", self._score_stub(scores)),
            patch(f"{_MODULE}._validate_candidate", lambda _c, _f: (True, "")),
        ):
            winner, _rejected, hysteresis = _select(
                [new_plan, previous],
                hysteresis_enabled=True,
                hysteresis_absolute=0.0,
                hysteresis_percentage=10.0,
                previous_winner_name=CANDIDATE_PASSIVE,
                previous_winner_score=0.0,
            )

        assert winner is new_plan
        assert hysteresis.applied is False

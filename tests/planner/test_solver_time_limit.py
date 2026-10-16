"""Tests for the MILP solver time limit and its visibility (issue #1309).

When HiGHS reaches its time limit, HSEM publishes the best plan found so far.
On slow hardware that plan can be Wait in every slot (#978).  These tests cover
the solve outcome carried on the result, the Plan Strategy explanation, the
latched warning, and the option's path from the config entry to every
``solve_milp()`` call.
"""

from __future__ import annotations

import dataclasses
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.hsem.const import (
    DEFAULT_CONFIG_VALUES,
    PLANNER_SOLVER_TIME_LIMIT_MAX_S,
    PLANNER_SOLVER_TIME_LIMIT_MIN_S,
)
from custom_components.hsem.coordinator_builder import build_planner_input
from custom_components.hsem.custom_sensors.config_reader import build_sensor_config
from custom_components.hsem.flows.battery_economics import (
    get_battery_economics_step_schema,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner.engine_core import run_planner
from custom_components.hsem.planner.milp import _incumbent
from custom_components.hsem.planner.milp._incumbent import (
    SOLVE_OUTCOME_ATTR,
    SOLVER_STATUS_FAILED,
    SOLVER_STATUS_OPTIMAL,
    SOLVER_STATUS_TIME_LIMIT,
    SolveOutcome,
    solve_and_validate,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils.misc import round_or_none, solver_time_limit_s
from custom_components.hsem.utils.solver_watch import (
    SOLVER_TIME_LIMIT_WARNING_CYCLES,
    note_solver_outcome,
)
from tests.planner.fixtures import make_winter_day_input

needs_scipy = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy is required for the MILP"
)

# A tiny model: x0 + x1 = 1, x2 <= 1, all in [0, 1], x0 integral.
_MODEL: dict[str, Any] = {
    "c_obj": [1.0, 2.0, -1.0],
    "a_ub": [[0.0, 0.0, 1.0]],
    "b_ub": [1.0],
    "a_eq": [[1.0, 1.0, 0.0]],
    "b_eq": [1.0],
    "bounds": [(0.0, 1.0)] * 3,
    "integrality": [1, 0, 0],
    "n_vars": 3,
    "slot_count": 2,
    "future_idx": [0, 1],
    "m": 2,
    "variable_blocks": {"battery": (0, 2), "ev": (2, 1)},
}


def _solve(linprog: Any, limit_s: float = 2.0) -> Any:
    return solve_and_validate(linprog, solver_time_limit_s=limit_s, **_MODEL)


def _time_limited_linprog(**result: Any) -> Any:
    """Return a fake linprog that stops at its time limit with a valid x."""

    def _linprog(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        fields: dict[str, Any] = {
            "status": 1,
            "message": "Time limit reached. (HiGHS Status 13: Time limit reached)",
            "success": False,
            "x": [1.0, 0.0, 1.0],
            "mip_gap": 0.125,
        }
        fields.update(result)
        return SimpleNamespace(**fields)

    return _linprog


def _force_time_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report every real HiGHS solve as stopped at its time limit."""
    import scipy.optimize

    real = scipy.optimize.linprog

    def _linprog(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        if result.success:
            result.status = 1
            result.success = False
            result.message = "Time limit reached. (HiGHS Status 13)"
            result.mip_gap = 0.04
        return result

    monkeypatch.setattr(scipy.optimize, "linprog", _linprog)


def _capture_limits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the time limit of every MILP solve."""
    limits: list[float] = []
    real = _incumbent.solve_and_validate

    def _capture(linprog: Any, **kwargs: Any) -> Any:
        limits.append(kwargs["solver_time_limit_s"])
        return real(linprog, **kwargs)

    monkeypatch.setattr(_incumbent, "solve_and_validate", _capture)
    return limits


# ---------------------------------------------------------------------------
# Solve outcome on the solver result
# ---------------------------------------------------------------------------


class TestSolveOutcome:
    @needs_scipy
    def test_a_finished_solve_is_optimal_with_its_wall_time(self) -> None:
        from scipy.optimize import linprog

        result = _solve(linprog, limit_s=3.0)

        outcome = getattr(result, SOLVE_OUTCOME_ATTR)
        assert outcome.status == SOLVER_STATUS_OPTIMAL
        assert outcome.time_s >= 0.0
        assert outcome.time_limit_s == pytest.approx(3.0)
        assert outcome.gap_pct == pytest.approx(0.0)

    def test_an_accepted_time_limited_solve_is_logged_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="custom_components.hsem"):
            result = _solve(_time_limited_linprog(), limit_s=1.5)

        outcome = getattr(result, SOLVE_OUTCOME_ATTR)
        assert outcome.status == SOLVER_STATUS_TIME_LIMIT
        assert outcome.time_limit_s == pytest.approx(1.5)
        assert outcome.gap_pct == pytest.approx(12.5)
        records = [
            r for r in caplog.records if "Accepted time-limited solution" in r.message
        ]
        assert len(records) == 1
        assert records[0].levelno == logging.DEBUG
        assert "gap=12.50%" in records[0].getMessage()

    @pytest.mark.parametrize("gap", [None, float("nan"), float("inf"), True, "x"])
    def test_an_unusable_gap_is_not_reported(self, gap: Any) -> None:
        result = _solve(_time_limited_linprog(mip_gap=gap))
        assert getattr(result, SOLVE_OUTCOME_ATTR).gap_pct is None

    def test_a_rejected_solve_returns_nothing(self) -> None:
        assert _solve(_time_limited_linprog(x=None)) is None

    def test_wall_time_is_left_out_of_equality(self) -> None:
        assert SolveOutcome("optimal", 0.1, 2.0, 0.0) == SolveOutcome(
            "optimal", 0.9, 2.0, 0.0
        )


# ---------------------------------------------------------------------------
# Plan explanation
# ---------------------------------------------------------------------------


@needs_scipy
class TestPlanExplanation:
    def test_a_finished_solve_publishes_optimal(self) -> None:
        output = run_planner(make_winter_day_input())

        expl = output.explanation
        assert output.winner_name == "milp"
        assert expl.solver_status == SOLVER_STATUS_OPTIMAL
        assert expl.solver_time_s is not None
        assert expl.solver_time_s >= 0.0
        assert expl.solver_time_limit_s == pytest.approx(2.0)
        assert expl.selected_strategy != "solver_time_limit"

    def test_a_time_limited_winner_is_named_in_the_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _force_time_limit(monkeypatch)
        inp = dataclasses.replace(
            make_winter_day_input(), planner_solver_time_limit_s=4.0
        )

        output = run_planner(inp)

        expl = output.explanation
        assert output.winner_name == "milp"
        assert expl.solver_status == SOLVER_STATUS_TIME_LIMIT
        assert expl.solver_time_limit_s == pytest.approx(4.0)
        assert expl.solver_gap_pct == pytest.approx(4.0)
        assert expl.selected_strategy == "solver_time_limit"
        assert "time limit" in expl.summary
        assert "solver_time_limit" in expl.constraints
        attrs = expl.as_dict()
        assert attrs["solver_status"] == SOLVER_STATUS_TIME_LIMIT
        assert attrs["solver_time_limit_s"] == pytest.approx(4.0)
        assert attrs["solver_gap_pct"] == pytest.approx(4.0)

    def test_a_tiny_limit_leaves_no_milp_plan(self) -> None:
        """A real solve under a microsecond limit finds no incumbent."""
        inp = dataclasses.replace(
            make_winter_day_input(), planner_solver_time_limit_s=1e-6
        )

        output = run_planner(inp)

        expl = output.explanation
        assert output.winner_name != "milp"
        assert expl.solver_status == SOLVER_STATUS_FAILED
        assert expl.solver_time_s is None
        assert expl.solver_time_limit_s == pytest.approx(1e-6)
        assert expl.selected_strategy == "solver_failed"
        assert output.winner_name in expl.summary

    def test_every_solve_of_the_run_gets_the_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        limits = _capture_limits(monkeypatch)
        inp = dataclasses.replace(
            make_winter_day_input(), planner_solver_time_limit_s=7.5
        )

        run_planner(inp)

        assert limits
        assert limits == pytest.approx([7.5] * len(limits))


def test_no_solver_outcome_publishes_none() -> None:
    attrs = PlanExplanation().as_dict()
    assert attrs["solver_status"] is None
    assert attrs["solver_time_s"] is None
    assert attrs["solver_gap_pct"] is None
    assert attrs["solver_time_limit_s"] is None


def test_round_or_none() -> None:
    assert round_or_none(None) is None
    assert round_or_none(1.23456) == pytest.approx(1.235)
    assert round_or_none(1.23456, 1) == pytest.approx(1.2)


# ---------------------------------------------------------------------------
# Latched warning
# ---------------------------------------------------------------------------


def _cycle(owner: object, status: str | None) -> None:
    note_solver_outcome(
        owner, PlanExplanation(solver_status=status, solver_time_limit_s=2.0)
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "hsem_planner_solver_time_limit_s" in r.getMessage()
    ]


class TestSolverWatch:
    def test_warns_once_after_consecutive_time_limited_cycles(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        owner = SimpleNamespace()
        with caplog.at_level(logging.DEBUG, logger="custom_components.hsem"):
            for _ in range(SOLVER_TIME_LIMIT_WARNING_CYCLES - 1):
                _cycle(owner, SOLVER_STATUS_TIME_LIMIT)
            assert _warnings(caplog) == []

            for _ in range(3):
                _cycle(owner, SOLVER_STATUS_TIME_LIMIT)

        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "3 consecutive cycles" in warnings[0].getMessage()

    def test_a_finished_solve_resets_the_run_and_rearms_the_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        owner = SimpleNamespace()
        with caplog.at_level(logging.DEBUG, logger="custom_components.hsem"):
            for _ in range(SOLVER_TIME_LIMIT_WARNING_CYCLES):
                _cycle(owner, SOLVER_STATUS_TIME_LIMIT)
            _cycle(owner, SOLVER_STATUS_OPTIMAL)
            for _ in range(SOLVER_TIME_LIMIT_WARNING_CYCLES - 1):
                _cycle(owner, SOLVER_STATUS_TIME_LIMIT)
            assert len(_warnings(caplog)) == 1

            _cycle(owner, SOLVER_STATUS_TIME_LIMIT)

        assert len(_warnings(caplog)) == 2

    @pytest.mark.parametrize("status", [SOLVER_STATUS_FAILED, None])
    def test_a_failed_or_missing_solve_leaves_the_run_unchanged(
        self, status: str | None, caplog: pytest.LogCaptureFixture
    ) -> None:
        owner = SimpleNamespace()
        with caplog.at_level(logging.DEBUG, logger="custom_components.hsem"):
            _cycle(owner, SOLVER_STATUS_TIME_LIMIT)
            _cycle(owner, status)
            _cycle(owner, SOLVER_STATUS_TIME_LIMIT)
            assert _warnings(caplog) == []
            _cycle(owner, SOLVER_STATUS_TIME_LIMIT)

        assert len(_warnings(caplog)) == 1


# ---------------------------------------------------------------------------
# The option
# ---------------------------------------------------------------------------


class TestOption:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, 2.0),
            ("unavailable", 2.0),
            (float("nan"), 2.0),
            ("4.5", 4.5),
            (0.2, PLANNER_SOLVER_TIME_LIMIT_MIN_S),
            (60, PLANNER_SOLVER_TIME_LIMIT_MAX_S),
        ],
    )
    def test_the_value_is_defaulted_and_clamped(
        self, value: Any, expected: float
    ) -> None:
        assert solver_time_limit_s(value) == pytest.approx(expected)

    def test_an_existing_entry_keeps_two_seconds(self) -> None:
        entry = MagicMock()
        entry.options = {}
        entry.data = {}

        cfg = build_sensor_config(entry)

        assert DEFAULT_CONFIG_VALUES["hsem_planner_solver_time_limit_s"] == (
            pytest.approx(2.0)
        )
        assert cfg.planner_solver_time_limit_s == pytest.approx(2.0)

    def test_a_configured_value_reaches_the_planner_input(self) -> None:
        entry = MagicMock()
        entry.options = {"hsem_planner_solver_time_limit_s": 6.5}
        entry.data = {}
        cfg = build_sensor_config(entry)

        planner_input = build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
            batteries_schedules=[],
        )

        assert planner_input.planner_solver_time_limit_s == pytest.approx(6.5)

    def test_the_builder_clamps_an_out_of_range_value(self) -> None:
        cfg = SensorConfig()
        cfg.planner_solver_time_limit_s = 30.0

        planner_input = build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
            batteries_schedules=[],
        )

        assert planner_input.planner_solver_time_limit_s == pytest.approx(
            PLANNER_SOLVER_TIME_LIMIT_MAX_S
        )

    @pytest.mark.asyncio
    async def test_the_battery_economics_step_offers_the_option(self) -> None:
        schema = await get_battery_economics_step_schema(None)

        keys = {str(key): key for key in schema.schema}
        key = keys["hsem_planner_solver_time_limit_s"]
        assert key.default() == pytest.approx(2.0)
        number = schema.schema[key].config
        assert number["min"] == pytest.approx(1.0)
        assert number["max"] == pytest.approx(10.0)

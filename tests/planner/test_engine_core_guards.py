"""Tests for ``run_planner`` warnings and the dynamic discharge floor guard.

The planner must degrade loudly rather than silently: unusable battery
capacity, consumption weights that do not sum to 100, an empty slot horizon,
and a MILP solution that only closed by using penalty variables all have to
reach ``PlannerOutput.warnings`` so the diagnostics sensors can surface them.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from custom_components.hsem.planner.candidate_generator import CANDIDATE_MILP
from custom_components.hsem.planner.engine_core import (
    _resolve_effective_discharge_floor_pct,
    run_planner,
)
from tests.planner.fixtures import make_summer_day_input

_MODULE = "custom_components.hsem.planner.engine_core"


class TestBatteryCapacityWarnings:
    """A battery that cannot store anything disables the simulation loudly."""

    @pytest.mark.parametrize("rated_kwh", [0.0, -5.0])
    def test_non_positive_capacity_warns_and_disables_the_battery(
        self, rated_kwh: float
    ) -> None:
        """The warning names the offending input so it reaches diagnostics."""
        inp = make_summer_day_input(battery_rated_capacity_kwh=rated_kwh)

        output = run_planner(inp)

        assert any(
            "battery_rated_capacity_kwh is zero or negative" in w
            for w in output.warnings
        )
        # With no usable capacity no slot may plan battery movement.
        assert all(
            slot.batteries_charged_kwh == pytest.approx(0.0)
            and slot.batteries_discharged_kwh == pytest.approx(0.0)
            for slot in output.slots
        )


class TestConsumptionWeightWarning:
    """Weights that do not sum to 100 make the blend meaningless."""

    def test_mismatched_weights_are_reported(self) -> None:
        """The warning states the actual sum."""
        inp = make_summer_day_input()
        inp.weight_1d = 10
        inp.weight_3d = 10
        inp.weight_7d = 10
        inp.weight_14d = 10

        output = run_planner(inp)

        assert any(
            "Consumption weights sum to 40, not 100" in w for w in output.warnings
        )

    def test_weights_summing_to_one_hundred_are_silent(self) -> None:
        """The default fixture weights produce no weight warning."""
        output = run_planner(make_summer_day_input())

        assert not any("Consumption weights sum to" in w for w in output.warnings)


class TestEmptyHorizon:
    """A horizon that generates no slots aborts with a warning."""

    def test_no_slots_returns_an_empty_plan_with_a_reason(self) -> None:
        """No slots means no plan, and the reason names the two inputs.

        ``interval_length_hours = 0`` is rejected earlier by the time-series
        index, so the empty-horizon guard is reached by an empty slot build.
        """
        with patch(f"{_MODULE}.build_slots", return_value=[]):
            output = run_planner(make_summer_day_input())

        assert output.slots == []
        assert any("No slots generated" in w for w in output.warnings)


class TestDynamicDischargeFloor:
    """A learned floor above the hardware floor is logged and applied."""

    def test_active_floor_is_logged_and_raises_the_reserve(self) -> None:
        """A 30 % learned floor above a 10 % hardware floor is announced."""
        inp = make_summer_day_input(battery_end_of_discharge_soc_pct=10.0)
        inp.dynamic_discharge_floor_pct = 30.0

        with patch(f"{_MODULE}.log_planner") as log:
            run_planner(inp)

        assert any(
            call.args[1].startswith("[core] Dynamic discharge floor active")
            for call in log.call_args_list
            if len(call.args) > 1
        )

    def test_resolver_returns_the_hardware_floor_when_no_learned_floor(self) -> None:
        """Without a learned floor the hardware floor is effective."""
        inp = make_summer_day_input(battery_end_of_discharge_soc_pct=12.0)

        hardware, effective, maximum = _resolve_effective_discharge_floor_pct(inp)

        assert hardware == pytest.approx(12.0)
        assert effective == pytest.approx(12.0)
        assert maximum > effective

    @pytest.mark.parametrize(
        "floor",
        [
            pytest.param(float("nan"), id="not_a_number"),
            pytest.param(float("inf"), id="infinite"),
            pytest.param("not a number", id="text"),
        ],
    )
    def test_unusable_learned_floor_falls_back_to_the_hardware_floor(
        self, floor: Any
    ) -> None:
        """A corrupt learned floor never becomes the reserve."""
        inp = make_summer_day_input(battery_end_of_discharge_soc_pct=10.0)
        inp.dynamic_discharge_floor_pct = floor

        hardware, effective, _maximum = _resolve_effective_discharge_floor_pct(inp)

        assert effective == pytest.approx(hardware)

    def test_learned_floor_below_the_hardware_floor_is_ignored(self) -> None:
        """The hardware reserve is a hard minimum."""
        inp = make_summer_day_input(battery_end_of_discharge_soc_pct=20.0)
        inp.dynamic_discharge_floor_pct = 5.0

        hardware, effective, _maximum = _resolve_effective_discharge_floor_pct(inp)

        assert hardware == pytest.approx(20.0)
        assert effective == pytest.approx(20.0)


class TestMilpPenaltyWarnings:
    """A MILP plan that only closed via penalties must say so."""

    @staticmethod
    def _select_with_violations(**diagnostics: float) -> Any:
        """Return a patched ``_select_candidate`` whose winner used penalties."""
        from custom_components.hsem.planner.engine_core import (
            _select_candidate as real_select,
        )

        def _select(*args: Any, **kwargs: Any) -> Any:
            candidates, winner, rejected, hyst = real_select(*args, **kwargs)
            winner.name = CANDIDATE_MILP
            winner.diagnostics = {"has_violations": True, **diagnostics}
            return candidates, winner, rejected, hyst

        return _select

    @pytest.mark.parametrize(
        ("diagnostics", "expected"),
        [
            pytest.param(
                {"total_violation_kwh": 0.5}, "SoC penalty=0.5000 kWh", id="soc_penalty"
            ),
            pytest.param(
                {"total_fuse_violation_kwh": 0.25},
                "fuse excess=0.2500 kWh",
                id="fuse_excess",
            ),
        ],
    )
    def test_penalty_violations_reach_the_warnings(
        self, diagnostics: dict[str, float], expected: str
    ) -> None:
        """Each penalty kind is named with its magnitude."""
        with patch(
            f"{_MODULE}._select_candidate",
            self._select_with_violations(**diagnostics),
        ):
            output = run_planner(make_summer_day_input())

        assert any("Penalty violations detected" in w for w in output.warnings)
        assert any(expected in w for w in output.warnings)

    def test_negligible_violations_are_not_reported(self) -> None:
        """A violation below the epsilon is not worth a warning."""
        with patch(
            f"{_MODULE}._select_candidate",
            self._select_with_violations(total_violation_kwh=0.0),
        ):
            output = run_planner(make_summer_day_input())

        assert not any("Penalty violations detected" in w for w in output.warnings)

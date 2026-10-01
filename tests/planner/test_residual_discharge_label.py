"""Regression tests for issue #1199 — a 1 Wh discharge on a wait-mode slot.

A battery 1 Wh above its hardware floor still has that watt-hour to give, and
the MILP uses it.  The write-out labels the slot ``batteries_discharge_mode``
and publishes ``batteries_discharged_kwh = 0.001``.  Three places decide
whether battery energy is "material", and they disagreed:

- the write-out acts on the raw LP value above ``_MIN_ACTION_KWH`` (1e-4);
- the self-consistency gate (issue #1035) reads the published field against
  ``MATERIAL_ENERGY_KWH`` (1e-9);
- ``concentrate_discharge_on_expensive_slots`` reserved LP slots with the
  applier's residue threshold, strictly more than 0.001 kWh.

When another LP discharge on the same calendar day had spent the day budget,
concentration relabelled the 1 Wh slot to ``batteries_wait_mode`` and left the
energy on it.  The gate then reported the plan on every replan while the
battery sat that close to its floor.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.candidate_generator import CANDIDATE_MILP
from custom_components.hsem.planner.milp._write_results import _MIN_ACTION_KWH
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils.recommendations import (
    MATERIAL_ENERGY_KWH,
    Recommendations,
)
from custom_components.hsem.utils.units import PLANNED_ENERGY_ROUNDING_KWH
from tests.test_dynamic_floor_reference_plan import _NOW, _planner_input

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_WAIT = Recommendations.BatteriesWaitMode.value
_DISCHARGE = Recommendations.BatteriesDischargeMode.value
_REPLAN_AT = _NOW.replace(hour=23, minute=0)
_DISCHARGE_EFF = 0.97


def _near_floor_input(soc_pct: float) -> PlannerInput:
    """23:00, battery at *soc_pct* on a 5 % floor, a 0.45 export spike at 21-23.

    Hourly slots, 48 h, a 0.5-0.8 kW house load and a 0.15-0.19 night.  The
    spike on tomorrow evening makes the plan sell the battery then, which
    spends tomorrow's whole concentration budget.
    """
    planner_input = replace(
        _planner_input(0.15),
        now_iso=_REPLAN_AT.isoformat(),
        battery_soc_pct=soc_pct,
        excess_export_enabled=True,
    )
    planner_input.price_points = [
        PricePoint(
            hour=point.hour,
            import_price=max(point.import_price, 0.47),
            export_price=0.45,
            day_offset=point.day_offset,
        )
        if point.hour in (21, 22)
        else point
        for point in planner_input.price_points
    ]
    return planner_input


def _future(output: PlannerOutput) -> list[PlannedSlot]:
    return [slot for slot in output.slots if slot.end > _REPLAN_AT]


def _contradictions(output: PlannerOutput) -> list[PlannedSlot]:
    """Return wait slots that still carry battery energy."""
    return [
        slot
        for slot in _future(output)
        if slot.recommendation == _WAIT
        and (
            slot.batteries_discharged_kwh > MATERIAL_ENERGY_KWH
            or slot.batteries_charged_kwh > MATERIAL_ENERGY_KWH
        )
    ]


class TestOneWattHourAboveTheFloor:
    """The issue's reproduction: live SoC 5.01 % on a 5 % hardware floor."""

    def test_plan_is_self_consistent(self) -> None:
        output = run_planner(_near_floor_input(5.01))

        assert output.winner_name == CANDIDATE_MILP
        assert output.plan_consistency_violations == []
        assert _contradictions(output) == []
        assert not any("self-consistency" in warning for warning in output.warnings)

    def test_the_watt_hour_is_dispatched_under_a_discharge_label(self) -> None:
        """The LP's decision is kept: label, energy and balance agree."""
        output = run_planner(_near_floor_input(5.01))
        dispatched = [
            slot for slot in _future(output) if slot.batteries_discharged_kwh > 0.0
        ]
        residual = min(dispatched, key=lambda slot: slot.start)

        assert residual.start < _REPLAN_AT + timedelta(hours=4)
        assert residual.batteries_discharged_kwh == pytest.approx(0.001)
        assert residual.recommendation == _DISCHARGE
        # Energy balance: the house load is met by the grid and that one Wh.
        assert residual.grid_import_kwh == pytest.approx(
            residual.avg_house_consumption_kwh
            - residual.batteries_discharged_kwh * _DISCHARGE_EFF,
            abs=1e-3,
        )
        assert residual.grid_export_kwh == pytest.approx(0.0)

    def test_winner_cost_is_the_published_cost(self) -> None:
        output = run_planner(_near_floor_input(5.01))
        winner = next(c for c in output.candidates if c.name == output.winner_name)

        assert winner.slots is output.slots
        assert winner._cost is not None and output.plan_cost is not None
        assert winner._cost.total_cost == pytest.approx(output.plan_cost.total_cost)

    @pytest.mark.parametrize("soc_pct", [5.0, 5.004, 5.005, 5.01, 5.015, 5.02, 5.05])
    def test_no_wait_slot_carries_energy_near_the_floor(self, soc_pct: float) -> None:
        """Every watt-hour count around the publication resolution."""
        output = run_planner(_near_floor_input(soc_pct))

        assert output.plan_consistency_violations == []
        assert _contradictions(output) == []


class TestMaterialityThresholdsAgree:
    """What "material battery energy" means at each stage."""

    def test_every_published_discharge_was_an_lp_action(self) -> None:
        """A flow that publishes as non-zero always cleared the write-out's gate.

        The write-out labels from the raw LP value and publishes at 3 decimals.
        The smallest raw value that publishes as 0.001 is 0.0005, so the label
        threshold has to sit below that: then a published discharge is always
        a labelled one, and reading the published field against
        ``MATERIAL_ENERGY_KWH`` is the same decision.
        """
        assert _MIN_ACTION_KWH < PLANNED_ENERGY_ROUNDING_KWH / 2
        assert MATERIAL_ENERGY_KWH < PLANNED_ENERGY_ROUNDING_KWH

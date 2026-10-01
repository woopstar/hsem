"""The floor inside the night charge window (issue #1238, accepted).

Inside the window the floor-free reference plan buys what the morning peak
still needs, and that purchase shrinks to nothing as the window fills the
battery.  A charge is credited to the live slot there, so the floor is the
configured minimum while the plan still buys and the bridge to the solar
surplus once it does not, and with 15-minute replans it alternates.  What
the spec accepts is pinned here: whichever of the two floors a replan gets,
the battery is below the bridge's reserve, the published plan keeps it for
the morning peak and the plan costs the same (issue #1222).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from tests.planner.test_dynamic_floor_closed_loop import _MIDNIGHT, _floor

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

#: 03:00 on the second day: inside the 02:00-06:00 window at 0.15, the
#: morning peak at 0.25 ahead, the PV surplus at 08:00.
_IN_THE_WINDOW = _MIDNIGHT + timedelta(days=1, hours=3)
_PEAK_HOURS = {6, 7, 8}
_HARDWARE_FLOOR_PCT = 5.0

#: ``(soc_pct, floor_pct, diagnostics, plan with the floor, plan released)``.
_Replan = tuple[float, float, dict, PlannerOutput, PlannerOutput]


def _peak_discharge_kwh(slots: list[PlannedSlot]) -> float:
    return sum(
        slot.batteries_discharged_kwh
        for slot in slots
        if slot.start.date() == _IN_THE_WINDOW.date() and slot.start.hour in _PEAK_HOURS
    )


class TestTheFloorInsideTheChargeWindow:
    """Two possible floors, one plan."""

    @pytest.fixture(scope="class", params=[14.0, 16.0, 17.4, 18.6, 20.0, 24.0])
    def replan(self, request: pytest.FixtureRequest) -> _Replan:
        soc_pct: float = request.param
        floor_pct, diag, final_input = _floor(_IN_THE_WINDOW, soc_pct)
        with_floor = run_planner(final_input)
        released = run_planner(
            replace(
                final_input,
                dynamic_discharge_floor_pct=_HARDWARE_FLOOR_PCT,
                dynamic_floor_profile=None,
            )
        )
        return soc_pct, floor_pct, diag, with_floor, released

    def test_the_floor_is_the_minimum_or_the_bridge_to_the_surplus(
        self, replan: _Replan
    ) -> None:
        """A charge in the live slot releases it; no charge bridges the night."""
        _soc_pct, floor_pct, diag, _with_floor, _released = replan

        if diag["refill_type"] == "grid_charge":
            assert diag["next_refill_slot"] == _IN_THE_WINDOW.isoformat()
            assert floor_pct == pytest.approx(_HARDWARE_FLOOR_PCT)
        else:
            assert diag["refill_type"] == "solar_surplus"
            assert (
                diag["next_refill_slot"]
                == (_MIDNIGHT + timedelta(days=1, hours=8)).isoformat()
            )
            assert floor_pct > 25.0

    def test_the_battery_is_below_the_bridge_reserve(self, replan: _Replan) -> None:
        soc_pct, floor_pct, diag, _with_floor, _released = replan

        if diag["refill_type"] == "solar_surplus":
            assert soc_pct < floor_pct

    def test_the_plan_costs_the_same_either_way(self, replan: _Replan) -> None:
        """The reserve the bridge asks for costs nothing: the plan already keeps
        the battery for the peak and spends it there (issue #1222).

        The slots are not identical: which of the equally priced 0.15 slots
        holds a few Wh of charge, and where the next day's charge sits, are
        ties the solver breaks either way.
        """
        _soc_pct, _floor_pct, _diag, with_floor, released = replan

        assert with_floor.winner_name == released.winner_name == "milp"
        assert with_floor.plan_cost is not None and released.plan_cost is not None
        assert with_floor.plan_cost.total_cost == pytest.approx(
            released.plan_cost.total_cost, abs=1e-3
        )
        assert _peak_discharge_kwh(with_floor.slots) > 1.0
        assert _peak_discharge_kwh(released.slots) > 1.0
        assert _peak_discharge_kwh(with_floor.slots) == pytest.approx(
            _peak_discharge_kwh(released.slots), abs=0.2
        )

"""Regression tests for issue #1474 — a cleared live-slot discharge that
left the slot short of energy.

The executable-minimum rule (issue #1364) clears a live-slot discharge that is
too small to run as a full self-consumption slot and relabels the slot
``batteries_wait_mode``.  It zeroed ``batteries_discharged_kwh`` and left the
slot's grid fields as the MILP wrote them.  ``simulate_soc`` trusts a MILP
slot's grid fields verbatim, so the published slot had a house load that no
source supplied: ``grid_import_kwh`` was short by ``cleared × η_dis``.  The
self-consistency gate reported it on every cycle of that slot, and the plan
cost counted the missing import as free.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.candidate_generator import CANDIDATE_MILP
from custom_components.hsem.planner.executable_minimum import (
    apply_executable_discharge_minimum,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.planner.plan_consistency import (
    ENERGY_BALANCE_TOLERANCE_KWH,
    check_plan_self_consistency,
    energy_balance_deficit,
)
from custom_components.hsem.utils.misc import clamp_efficiency
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations
from tests.test_dynamic_floor_reference_plan import _NOW, _planner_input

_WAIT = Recommendations.BatteriesWaitMode.value
_DISCHARGE = Recommendations.BatteriesDischargeMode.value
_REPLAN_AT = _NOW.replace(hour=17, minute=5)
_ETA = 0.98


def _input(soc_pct: float, interval_minutes: int = 60) -> PlannerInput:
    """17:05 with a battery that holds a little more than the evening needs.

    The MILP spends the remainder on the live slot as a discharge far below
    the slot's house load, which the executable-minimum rule then clears.
    """
    return replace(
        _planner_input(0.15, interval_minutes=interval_minutes),
        now_iso=_REPLAN_AT.isoformat(),
        battery_soc_pct=soc_pct,
    )


def _live_slot(slots: list[PlannedSlot], now: datetime) -> PlannedSlot:
    return next(slot for slot in slots if slot.start <= now < slot.end)


@pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)
class TestClearedLiveDischargeCycle:
    """Synthetic cycles that reproduce the live install's daily warning."""

    @pytest.mark.parametrize(
        ("soc_pct", "interval_minutes"), [(22.8, 60), (21.6, 60), (7.2, 15)]
    )
    def test_plan_is_self_consistent(
        self, soc_pct: float, interval_minutes: int
    ) -> None:
        inp = _input(soc_pct, interval_minutes)
        output = run_planner(inp)

        assert output.winner_name == CANDIDATE_MILP
        assert output.plan_consistency_violations == []
        assert not any("self-consistency" in warning for warning in output.warnings)

    def test_live_wait_slot_imports_its_whole_house_load(self) -> None:
        """The term that was short: grid import on the cleared slot."""
        inp = _input(22.8)
        output = run_planner(inp)
        live = _live_slot(output.slots, _REPLAN_AT)

        assert live.recommendation == _WAIT
        assert live.batteries_discharged_kwh == pytest.approx(0.0)
        assert live.avg_house_consumption_kwh > 0.1
        assert live.grid_import_kwh == pytest.approx(
            live.avg_house_consumption_kwh - live.solcast_pv_estimate_kwh, abs=1e-3
        )
        eta_chg = clamp_efficiency(inp.battery_charge_efficiency_pct)
        eta_dis = clamp_efficiency(inp.battery_discharge_efficiency_pct)
        assert abs(energy_balance_deficit(live, eta_chg, eta_dis)) < (
            ENERGY_BALANCE_TOLERANCE_KWH
        )


def _milp_slot(
    *,
    discharged_kwh: float,
    grid_import_kwh: float = 0.0,
    grid_export_kwh: float = 0.0,
    battery_export_kwh: float = 0.0,
    house_kwh: float = 1.2,
    pv_kwh: float = 0.0,
) -> PlannedSlot:
    """Return a live discharge slot as the MILP write-out leaves it."""
    return PlannedSlot(
        start=_REPLAN_AT.replace(minute=0),
        end=_REPLAN_AT.replace(minute=0) + timedelta(minutes=15),
        price=SlotPrice(import_price=2.0, export_price=0.5),
        recommendation=_DISCHARGE,
        solcast_pv_estimate_kwh=pv_kwh,
        avg_house_consumption_kwh=house_kwh,
        estimated_net_consumption_kwh=house_kwh - pv_kwh,
        batteries_discharged_kwh=discharged_kwh,
        grid_import_kwh=grid_import_kwh,
        grid_export_kwh=grid_export_kwh,
        primary_battery_export_kwh=battery_export_kwh,
        pv_export_kwh=grid_export_kwh - battery_export_kwh,
    )


def _clear(slot: PlannedSlot, **kwargs: float) -> PlannedSlot | None:
    return apply_executable_discharge_minimum(
        [slot],
        _REPLAN_AT,
        max_discharge_per_slot_kwh=1.25,
        available_kwh=7.5,
        discharge_eff=_ETA,
        **kwargs,
    )


class TestClearedDischargeMovesToGrid:
    """Clearing the discharge re-derives the slot's grid flows in one step."""

    def test_balanced_slot_stays_balanced(self) -> None:
        """The 2026-11-22 16:15 shape: 0.223 kWh planned on a 1.257 kWh load."""
        slot = _milp_slot(discharged_kwh=0.223, grid_import_kwh=1.038, house_kwh=1.257)
        assert energy_balance_deficit(slot, _ETA, _ETA) == pytest.approx(0.0, abs=1e-3)

        assert _clear(slot) is slot

        assert slot.recommendation == _WAIT
        assert slot.batteries_discharged_kwh == pytest.approx(0.0)
        assert slot.grid_import_kwh == pytest.approx(1.257, abs=1e-3)
        assert slot.grid_export_kwh == pytest.approx(0.0)
        assert energy_balance_deficit(slot, _ETA, _ETA) == pytest.approx(0.0, abs=1e-3)
        assert (
            check_plan_self_consistency(
                [slot], now=_REPLAN_AT, charge_eff=_ETA, discharge_eff=_ETA
            )
            == []
        )

    def test_slot_cost_follows_the_new_import(self) -> None:
        slot = _milp_slot(discharged_kwh=0.2, grid_import_kwh=1.004)

        _clear(slot)

        assert slot.estimated_cost_currency == pytest.approx(
            slot.grid_import_kwh * 2.0, abs=1e-4
        )

    def test_cleared_discharge_is_netted_against_export(self) -> None:
        """An export on the slot shrinks before any import is added."""
        slot = _milp_slot(
            discharged_kwh=0.2,
            grid_export_kwh=0.05,
            battery_export_kwh=0.05,
            house_kwh=1.0,
        )

        assert _clear(slot, export_min_price=0.1, export_fee_per_kwh=0.05) is slot

        assert slot.grid_import_kwh == pytest.approx(0.2 * _ETA - 0.05, abs=1e-3)
        assert slot.grid_export_kwh == pytest.approx(0.0)
        assert slot.primary_battery_export_kwh == pytest.approx(0.0)
        assert slot.pv_export_kwh == pytest.approx(0.0)

    def test_kept_discharge_leaves_grid_fields_alone(self) -> None:
        slot = _milp_slot(discharged_kwh=0.8, grid_import_kwh=0.416)

        assert _clear(slot) is None

        assert slot.grid_import_kwh == pytest.approx(0.416)
        assert slot.batteries_discharged_kwh == pytest.approx(0.8)

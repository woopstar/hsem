"""The seasonal fill must not book a solar charge on MILP export slots (issue #1158).

On the MILP candidate, a PV-surplus slot the LP left idle (``ec = ed = 0``) is
an LP decision to export that surplus.  ``apply_optimization_strategy`` used to
label such a slot ``batteries_charge_solar`` and write the surplus into
``batteries_charged_kwh``.  ``simulate_soc(milp_prepopulated=True)`` then kept
the LP's grid export as well, so the published slot counted the same PV as
both stored and exported, and the applier charged the battery against a plan
that exported.

Structure:

1. The issue's four-slot reproduction, through the fill and ``simulate_soc``.
2. Non-MILP candidates keep the fill's solar charge.
3. End to end: every published future slot balances, with and without
   battery headroom.

Backport to the 6.3.x line, which has no plan self-consistency gate, so the
balance is computed here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.discharge_scheduler import (
    apply_optimization_strategy,
)
from custom_components.hsem.planner.ev_load_accounting import split_house_and_ev_load
from custom_components.hsem.planner.soc_simulation import simulate_soc
from custom_components.hsem.utils.misc import clamp_efficiency
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations
from tests.planner.fixtures import (
    make_flat_price_input,
    make_negative_price_input,
    make_summer_day_input,
    make_winter_day_input,
)

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2024, 6, 15, 11, 0, tzinfo=_TZ)
_WINTER = [1, 2, 3, 10, 11, 12]
_CHARGE_SOLAR = Recommendations.BatteriesChargeSolar.value
_WAIT = Recommendations.BatteriesWaitMode.value

_BUILDERS: dict[str, Callable[[], PlannerInput]] = {
    "summer": make_summer_day_input,
    "winter": make_winter_day_input,
    "flat": make_flat_price_input,
    "negative": make_negative_price_input,
}


def energy_balance_deficit(
    slot: PlannedSlot, charge_eff: float, discharge_eff: float
) -> float:
    """Return ``demand − supply`` for one slot's published flows (kWh)."""
    house, ev = split_house_and_ev_load(slot)
    supply = (
        slot.solcast_pv_estimate_kwh
        + slot.grid_import_kwh
        + slot.batteries_discharged_kwh * discharge_eff
    )
    demand = house + ev + slot.grid_export_kwh + slot.batteries_charged_kwh / charge_eff
    return demand - supply


def _lp_export_slots() -> list[PlannedSlot]:
    """Return four 15-min slots as the MILP write-out leaves an export slot.

    Load 0.250 against PV 0.277 kWh: the LP exports the 0.027 surplus and
    leaves the battery idle, so ``recommendation`` stays ``None``.
    """
    slots = []
    for i in range(4):
        start = _NOW + timedelta(minutes=15 * i)
        slot = PlannedSlot(start=start, end=start + timedelta(minutes=15))
        slot.avg_house_consumption_kwh = 0.250
        slot.solcast_pv_estimate_kwh = 0.277
        slot.estimated_net_consumption_kwh = -0.027
        slot.grid_export_kwh = 0.027
        slot.price = SlotPrice(import_price=0.30, export_price=0.10)
        slots.append(slot)
    return slots


def _fill_and_simulate(slots: list[PlannedSlot], *, milp: bool) -> None:
    """Run the fill and the SoC simulation as the selector does (8 of 14 kWh)."""
    apply_optimization_strategy(
        slots,
        _NOW,
        current_capacity=8.0,
        usable_capacity=14.0,
        required_capacity=0.0,
        months_winter=_WINTER,
        unassigned_slots_are_lp_decisions=milp,
    )
    simulate_soc(
        slots,
        _NOW,
        current_kwh=8.0,
        usable_kwh=14.0,
        max_capacity_kwh=14.0,
        max_charge_per_slot=1.25,
        max_discharge_per_slot=1.25,
        milp_prepopulated=milp,
    )


# ===========================================================================
# 1. The issue's reproduction
# ===========================================================================


class TestMilpExportSlot:
    """An LP export slot keeps the LP's flows and a non-charging label."""

    def test_no_charge_is_booked(self) -> None:
        slots = _lp_export_slots()
        _fill_and_simulate(slots, milp=True)
        for slot in slots:
            assert slot.batteries_charged_kwh == pytest.approx(0.0)
            assert slot.recommendation == _WAIT

    def test_the_lp_export_survives(self) -> None:
        slots = _lp_export_slots()
        _fill_and_simulate(slots, milp=True)
        for slot in slots:
            assert slot.grid_export_kwh == pytest.approx(0.027)
            assert slot.grid_import_kwh == pytest.approx(0.0)

    def test_every_slot_balances(self) -> None:
        slots = _lp_export_slots()
        _fill_and_simulate(slots, milp=True)
        for slot in slots:
            assert energy_balance_deficit(slot, 1.0, 1.0) == pytest.approx(
                0.0, abs=1e-3
            )

    def test_the_soc_trajectory_stays_flat(self) -> None:
        """The battery stored nothing, so its energy must not rise."""
        slots = _lp_export_slots()
        _fill_and_simulate(slots, milp=True)
        for slot in slots:
            assert slot.estimated_battery_capacity_kwh == pytest.approx(8.0)


# ===========================================================================
# 2. Non-MILP candidates
# ===========================================================================


class TestNonMilpFillUnchanged:
    """Without an LP, the fill still schedules the solar charge."""

    def test_the_fill_charges_the_surplus(self) -> None:
        slots = [_lp_export_slots()[0]]
        slots[0].grid_export_kwh = 0.0
        apply_optimization_strategy(
            slots,
            _NOW,
            current_capacity=8.0,
            usable_capacity=14.0,
            required_capacity=0.0,
            months_winter=_WINTER,
        )
        assert slots[0].recommendation == _CHARGE_SOLAR
        assert slots[0].batteries_charged_kwh == pytest.approx(0.027)

    def test_the_simulated_slot_balances(self) -> None:
        """``simulate_soc`` derives the flows itself, so the charge is not exported."""
        slots = _lp_export_slots()
        for slot in slots:
            slot.grid_export_kwh = 0.0
        _fill_and_simulate(slots, milp=False)
        for slot in slots:
            assert slot.recommendation == _CHARGE_SOLAR
            assert slot.batteries_charged_kwh == pytest.approx(0.027)
            assert slot.grid_export_kwh == pytest.approx(0.0)
            assert energy_balance_deficit(slot, 1.0, 1.0) == pytest.approx(
                0.0, abs=1e-3
            )


# ===========================================================================
# 3. End to end
# ===========================================================================


class TestPublishedPlanBalances:
    """Every published future slot satisfies the site energy balance."""

    @pytest.mark.parametrize("name", sorted(_BUILDERS))
    @pytest.mark.parametrize("soc_pct", [10.0, 50.0, 100.0])
    def test_every_future_slot_balances(self, name: str, soc_pct: float) -> None:
        """Within 1e-3 kWh in both directions, with and without headroom.

        The stock fixtures' export prices are all positive, so the LP never
        curtails and no slot may carry unused supply either.
        """
        inp = _BUILDERS[name]()
        inp.battery_soc_pct = soc_pct
        output = run_planner(inp)
        now = datetime.fromisoformat(inp.now_iso)
        charge_eff = clamp_efficiency(inp.battery_charge_efficiency_pct)
        discharge_eff = clamp_efficiency(inp.battery_discharge_efficiency_pct)
        offenders = [
            (slot.start.isoformat(), slot.recommendation, round(deficit, 4))
            for slot in output.slots
            if slot.end > now
            and abs(deficit := energy_balance_deficit(slot, charge_eff, discharge_eff))
            > 1e-3
        ]
        assert offenders == []

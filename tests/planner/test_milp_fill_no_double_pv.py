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
4. The plan self-consistency gate reports an energy-balance deficit.
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
from custom_components.hsem.planner.plan_consistency import (
    ENERGY_BALANCE_TOLERANCE_KWH,
    check_plan_self_consistency,
    energy_balance_deficit,
)
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

    @pytest.mark.parametrize("name", sorted(_BUILDERS))
    def test_no_balance_warning_is_published(self, name: str) -> None:
        output = run_planner(_BUILDERS[name]())
        assert not any("energy balance" in w for w in output.warnings)


# ===========================================================================
# 4. The plan self-consistency gate
# ===========================================================================


def _double_counted_slot(hour: int = 0) -> PlannedSlot:
    """Return the published slot shape #1158 produced: PV stored and exported."""
    slot = _lp_export_slots()[0]
    slot.start = _NOW + timedelta(hours=hour)
    slot.end = slot.start + timedelta(minutes=15)
    slot.recommendation = _CHARGE_SOLAR
    slot.batteries_charged_kwh = 0.027
    return slot


class TestGateReportsBalance:
    """The gate reports energy from nowhere, and nothing else."""

    def test_deficit_is_reported(self) -> None:
        violations = check_plan_self_consistency(
            [_double_counted_slot()], charge_eff=0.98, discharge_eff=0.98
        )
        assert len(violations) == 1
        assert "energy balance short by 0.028 kWh" in violations[0]

    def test_without_efficiencies_the_balance_is_not_checked(self) -> None:
        """Callers that pass no efficiencies keep the label-only gate."""
        assert check_plan_self_consistency([_double_counted_slot()]) == []

    def test_past_slots_are_skipped(self) -> None:
        slot = _double_counted_slot()
        assert (
            check_plan_self_consistency(
                [slot], now=slot.end, charge_eff=0.98, discharge_eff=0.98
            )
            == []
        )

    def test_unused_supply_is_not_a_violation(self) -> None:
        """Curtailed PV leaves supply unused, and curtailment is not a field."""
        slot = _lp_export_slots()[0]
        slot.grid_export_kwh = 0.0
        slot.recommendation = _WAIT
        assert energy_balance_deficit(slot, 0.98, 0.98) < 0.0
        assert (
            check_plan_self_consistency([slot], charge_eff=0.98, discharge_eff=0.98)
            == []
        )

    def test_a_deficit_within_rounding_is_tolerated(self) -> None:
        slot = _lp_export_slots()[0]
        slot.recommendation = _WAIT
        slot.grid_export_kwh += ENERGY_BALANCE_TOLERANCE_KWH / 2
        assert (
            check_plan_self_consistency([slot], charge_eff=0.98, discharge_eff=0.98)
            == []
        )

    def test_an_unlabelled_slot_is_balance_checked(self) -> None:
        slot = _double_counted_slot()
        slot.recommendation = None
        assert (
            len(
                check_plan_self_consistency([slot], charge_eff=0.98, discharge_eff=0.98)
            )
            == 1
        )

    def test_ev_load_counts_as_demand(self) -> None:
        """EV energy is split as ``simulate_soc`` splits it, not ignored."""
        slot = _lp_export_slots()[0]
        slot.recommendation = _WAIT
        slot.ev_planned_load_kwh = 1.0
        slot.grid_import_kwh = 1.0
        assert energy_balance_deficit(slot, 0.98, 0.98) == pytest.approx(0.0)

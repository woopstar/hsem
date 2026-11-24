"""Executable minimum for the live slot's planned discharge (issue #1364).

``batteries_discharge_mode`` executes as MaximizeSelfConsumption, where the
inverter serves the whole house load from the battery for the whole slot.
The planned amount is not a setpoint there: a solved 0.053 kWh "use the
surplus" discharge ran as 0.51 kWh, the next re-solve bought it back, and
the battery flapped around the point the plan was trying to hold.

What MaximizeSelfConsumption actually delivers in a slot is bounded by the
house load net of PV, by the battery's per-slot maximum and by the energy
still available above the floor.  A planned discharge on the live slot that
is smaller than :data:`EXECUTABLE_DISCHARGE_FRACTION` of that deliverable
amount is therefore not executable as planned: the inverter would deliver
several times more.  It is cleared and the slot relabelled
``batteries_wait_mode`` *before* the SoC simulation, so the energy balance,
the SoC trajectory and the plan cost all see the slot as a hold and the
surplus rides along to a later slot or the terminal SoC.  The house load the
cleared discharge would have served moves to the slot's grid flows in the same
step (issue #1474): the simulation trusts a MILP slot's grid fields verbatim,
so a cleared discharge that left them alone published a slot whose load no
source supplied, and scored that energy as free.  A plan that
discharges the whole deliverable amount — the normal evening slot, or the
last energy above the floor — is untouched, however small.  Only the live
slot is touched: a future slot is re-solved before it executes.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.cost_helpers import slot_grid_cash_flow_cost
from custom_components.hsem.utils.datetime_utils import slot_contains
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.recommendations import Recommendations

#: A live-slot discharge below this fraction of what self-consumption would
#: deliver in the slot is cleared instead of executed as a full slot.
EXECUTABLE_DISCHARGE_FRACTION: float = 0.25

#: Only the planner's own self-consumption windows are subject to the rule.
#: The two forcible modes are setpoint-driven, and EV labels carry charger
#: intent that must survive.
_RELABELLED_RECS: frozenset[str] = frozenset(
    {
        Recommendations.BatteriesDischargeMode.value,
        Recommendations.BatteriesDischargeWindowMode.value,
    }
)


def deliverable_discharge_kwh(
    slot: PlannedSlot,
    *,
    max_discharge_per_slot_kwh: float,
    available_kwh: float,
) -> float:
    """Return what self-consumption would actually discharge in *slot*.

    Args:
        slot: The slot; its net consumption after PV bounds the house draw.
        max_discharge_per_slot_kwh: The battery's maximum discharge per slot.
        available_kwh: Energy above the discharge floor at the slot's start.

    Returns:
        The smallest of the three bounds, never negative.
    """
    net_load = max(float(slot.estimated_net_consumption_kwh or 0.0), 0.0)
    return max(min(net_load, max_discharge_per_slot_kwh, available_kwh), 0.0)


def executable_discharge_minimum_kwh(
    deliverable_kwh: float,
    fraction: float = EXECUTABLE_DISCHARGE_FRACTION,
) -> float:
    """Return the smallest live-slot discharge worth executing, in kWh."""
    return max(deliverable_kwh, 0.0) * max(min(fraction, 1.0), 0.0)


def _move_cleared_discharge_to_grid(
    slot: PlannedSlot,
    cleared_kwh: float,
    *,
    discharge_eff: float,
    export_min_price: float,
    export_fee_per_kwh: float,
) -> None:
    """Re-derive a slot's grid flows after its discharge was cleared.

    The battery no longer delivers ``cleared_kwh × η_dis`` to the AC bus, so
    the slot's net grid flow rises by that amount: first out of any export,
    then as import.  Nothing is left to export from the battery.

    Args:
        slot: The slot whose discharge was cleared, mutated in place.
        cleared_kwh: The battery-side discharge that was removed.
        discharge_eff: Discharge efficiency fraction (0-1].
        export_min_price: Minimum export price, for the slot's cash flow.
        export_fee_per_kwh: Export fee per kWh, for the slot's cash flow.
    """
    net_flow = (
        float(slot.grid_import_kwh)
        - float(slot.grid_export_kwh)
        + cleared_kwh * discharge_eff
    )
    slot.grid_import_kwh = round(max(net_flow, 0.0), 3)
    slot.grid_export_kwh = round(max(-net_flow, 0.0), 3)
    slot.primary_battery_export_kwh = 0.0
    slot.pv_export_kwh = slot.grid_export_kwh
    slot.estimated_cost_currency = round(
        slot_grid_cash_flow_cost(
            slot,
            export_min_price=export_min_price,
            export_fee_per_kwh=export_fee_per_kwh,
        ),
        4,
    )


def apply_executable_discharge_minimum(
    slots: list[PlannedSlot],
    now: datetime,
    *,
    max_discharge_per_slot_kwh: float,
    available_kwh: float,
    discharge_eff: float,
    export_min_price: float = 0.0,
    export_fee_per_kwh: float = 0.0,
    fraction: float = EXECUTABLE_DISCHARGE_FRACTION,
) -> PlannedSlot | None:
    """Clear a sub-executable discharge on the live slot and relabel it.

    The cleared slot stays balanced: the load the discharge would have served
    is added to its grid flows (issue #1474).

    Args:
        slots: The candidate's slots, mutated in place.
        now: Timezone-aware current time; selects the live slot.
        max_discharge_per_slot_kwh: The battery's maximum discharge per slot.
        available_kwh: Energy above the discharge floor right now.
        discharge_eff: Discharge efficiency fraction (0-1].
        export_min_price: Minimum export price, for the slot's cash flow.
        export_fee_per_kwh: Export fee per kWh, for the slot's cash flow.
        fraction: Fraction of the deliverable amount below which a planned
            discharge is cleared.

    Returns:
        The slot that was cleared, or ``None`` when nothing changed.
    """
    for slot in slots:
        if not slot_contains(slot.start, slot.end, now):
            continue
        if slot.recommendation not in _RELABELLED_RECS:
            return None
        discharge = float(slot.batteries_discharged_kwh or 0.0)
        deliverable = deliverable_discharge_kwh(
            slot,
            max_discharge_per_slot_kwh=max_discharge_per_slot_kwh,
            available_kwh=available_kwh,
        )
        minimum_kwh = executable_discharge_minimum_kwh(deliverable, fraction)
        if minimum_kwh <= 0.0 or discharge <= 1e-9 or discharge >= minimum_kwh:
            return None
        log_planner(
            "debug",
            "[exec_min] live slot %s: planned discharge %.3f kWh is below the "
            "executable minimum %.3f kWh (%.0f%% of the %.3f kWh self-consumption "
            "would deliver) — cleared and relabelled %s → batteries_wait_mode",
            slot.start.isoformat(),
            discharge,
            minimum_kwh,
            fraction * 100.0,
            deliverable,
            slot.recommendation,
        )
        slot.batteries_discharged_kwh = 0.0
        slot.recommendation = Recommendations.BatteriesWaitMode.value
        _move_cleared_discharge_to_grid(
            slot,
            discharge,
            discharge_eff=discharge_eff,
            export_min_price=export_min_price,
            export_fee_per_kwh=export_fee_per_kwh,
        )
        return slot
    return None

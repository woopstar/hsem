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
surplus rides along to a later slot or the terminal SoC.  A plan that
discharges the whole deliverable amount — the normal evening slot, or the
last energy above the floor — is untouched, however small.  Only the live
slot is touched: a future slot is re-solved before it executes.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.planned_slot import PlannedSlot
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


def apply_executable_discharge_minimum(
    slots: list[PlannedSlot],
    now: datetime,
    *,
    max_discharge_per_slot_kwh: float,
    available_kwh: float,
    fraction: float = EXECUTABLE_DISCHARGE_FRACTION,
) -> PlannedSlot | None:
    """Clear a sub-executable discharge on the live slot and relabel it.

    Args:
        slots: The candidate's slots, mutated in place.
        now: Timezone-aware current time; selects the live slot.
        max_discharge_per_slot_kwh: The battery's maximum discharge per slot.
        available_kwh: Energy above the discharge floor right now.
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
        return slot
    return None

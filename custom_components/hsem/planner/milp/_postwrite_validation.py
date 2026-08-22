"""Validate executable MILP writeback against physical inventory bounds."""

from __future__ import annotations

import math

from custom_components.hsem.models.planned_slot import PlannedSlot

_TOLERANCE_KWH = 1e-6


def validate_primary_inventory(
    slots: list[PlannedSlot],
    future_idx: list[int],
    *,
    current_kwh: float,
    usable_kwh: float,
) -> dict[str, object]:
    """Validate cumulative rounded primary-battery energy fields."""
    initial = float(current_kwh)
    capacity = float(usable_kwh)
    if not math.isfinite(initial) or not math.isfinite(capacity) or capacity < 0.0:
        return {"valid": False, "reason": "invalid_inventory_bounds"}

    lower = min(initial, 0.0)
    upper = max(initial, capacity)
    running = initial
    for sequence, slot_i in enumerate(future_idx):
        slot = slots[slot_i]
        charge = float(slot.batteries_charged_kwh)
        discharge = float(slot.batteries_discharged_kwh)
        if (
            not math.isfinite(charge)
            or not math.isfinite(discharge)
            or charge < 0.0
            or discharge < 0.0
        ):
            return {
                "valid": False,
                "reason": "invalid_primary_energy",
                "slot": sequence,
            }
        running += charge - discharge
        if running < lower - _TOLERANCE_KWH:
            return {
                "valid": False,
                "reason": "primary_inventory_below_floor",
                "slot": sequence,
                "inventory_kwh": round(running, 6),
            }
        if running > upper + _TOLERANCE_KWH:
            return {
                "valid": False,
                "reason": "primary_inventory_above_ceiling",
                "slot": sequence,
                "inventory_kwh": round(running, 6),
            }
    return {
        "valid": True,
        "reason": "ok",
        "initial_kwh": round(initial, 6),
        "final_kwh": round(running, 6),
        "lower_bound_kwh": round(lower, 6),
        "upper_bound_kwh": round(upper, 6),
    }

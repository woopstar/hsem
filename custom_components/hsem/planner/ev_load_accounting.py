"""Shared planner split between normalized house demand and EV demand."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from custom_components.hsem.models.planned_slot import PlannedSlot


def split_house_and_ev_load(
    slot: PlannedSlot,
    *,
    accounted_load_kwh: float | None = None,
) -> tuple[float, float]:
    """Return pure-house and total EV AC demand for one slot.

    ``ev_accounted_load_kwh`` is the only EV energy still embedded in the
    normalized house baseline. ``ev_planned_load_kwh`` is separate demand.
    The house result is intentionally not clamped: a negative value exposes an
    accounting-contract violation instead of silently erasing genuine demand.
    """
    accounted = (
        max(slot.ev_accounted_load_kwh, 0.0)
        if accounted_load_kwh is None
        else max(accounted_load_kwh, 0.0)
    )
    planned = max(slot.ev_planned_load_kwh, 0.0)
    return slot.avg_house_consumption_kwh - accounted, planned + accounted

"""Shared helper for charge scheduling passes."""

from __future__ import annotations

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.utils.recommendations import (
    CHARGE_RECS as _CHARGE_RECS,
)

# ---------------------------------------------------------------------------
# Helper: sum of already-planned charge energy
# ---------------------------------------------------------------------------


def _already_planned_charge_kwh(slots: list[PlannedSlot]) -> float:
    """Return the sum of ``batteries_charged_kwh`` across all charge-type slots.

    Used by downstream charge passes (e.g. opportunistic charge) to avoid
    exceeding the battery's remaining capacity when an earlier pass has
    already assigned charge energy.

    Args:
        slots: The mutable slot list to scan.

    Returns:
        Total kWh of charge energy already planned.
    """
    return sum(
        s.batteries_charged_kwh for s in slots if s.recommendation in _CHARGE_RECS
    )

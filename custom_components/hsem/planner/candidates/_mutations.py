"""Slot mutation helpers for candidate generation.

Shared low-level helpers that copy slots and apply simple recommendation
mutations (solar charging, grid charging, discharge clearing).
"""

from __future__ import annotations

import copy
from datetime import datetime

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.recommendations import (
    CHARGE_RECS as _CHARGE_RECS,
    DISCHARGE_RECS as _DISCHARGE_RECS,
    Recommendations,
)


def _copy_slots(slots: list[PlannedSlot]) -> list[PlannedSlot]:
    """Return an independent copy of *slots* for candidate isolation.

    Uses a shallow copy of each :class:`PlannedSlot` dataclass.  This is
    safe because every field on ``PlannedSlot`` is either an immutable scalar
    (``float``, ``str | None``) or an immutable named-tuple
    (:class:`~custom_components.hsem.utils.prices.SlotPrice`, ``datetime``).
    There are intentionally **no mutable container fields** (lists, dicts)
    on ``PlannedSlot`` — if any are added in the future this function must be
    updated to use ``copy.deepcopy`` instead.

    Each returned slot is an independent object: mutating ``recommendation``,
    ``batteries_charged``, ``ev_planned_load_kwh``, or any other scalar field
    on a copy does **not** affect the original or any other copy.
    """
    return [copy.copy(s) for s in slots]


def _clear_all_charge_discharge(slots: list[PlannedSlot]) -> None:
    """Reset every charge and discharge recommendation to ``None``.

    ``batteries_charged`` is also zeroed on cleared slots so the SoC
    simulation starts from a clean slate for the no-action candidate.

    ``ev_planned_load_kwh`` is intentionally **not** touched: it represents
    real AC-side demand for EV charging that exists regardless of what the
    battery does.  The no-action candidate still carries EV load in its net
    consumption; only battery scheduling is removed.
    """
    for slot in slots:
        if slot.recommendation in _CHARGE_RECS | _DISCHARGE_RECS:
            slot.recommendation = None
            slot.batteries_charged_kwh = 0.0


def _apply_passive_solar(slots: list[PlannedSlot], now: datetime) -> None:
    """Allow solar charging wherever estimated_net_consumption < 0 (PV surplus).

    This models the inverter default behaviour: no grid charging, no forced
    discharge, but passive absorption of PV surplus into the battery.
    estimated_net_consumption is negative when PV production exceeds house
    load (including EV).  The surplus magnitude is used directly as
    batteries_charged.  The SoC simulation caps it against actual battery
    limits afterwards.
    """
    for slot in slots:
        # Clear all active scheduling first
        if slot.recommendation in _CHARGE_RECS | _DISCHARGE_RECS:
            slot.recommendation = None
            slot.batteries_charged_kwh = 0.0

        # Passively absorb surplus into battery for future slots only
        # NaN < 0.0 is False in Python so no explicit NaN guard is needed
        if (
            as_tz(slot.end, now.tzinfo) > now
            and slot.estimated_net_consumption_kwh is not None
            and slot.estimated_net_consumption_kwh < 0.0
        ):
            slot.recommendation = Recommendations.BatteriesChargeSolar.value
            slot.batteries_charged_kwh = round(-slot.estimated_net_consumption_kwh, 3)


def _remove_solar_charge(slots: list[PlannedSlot]) -> None:
    """Clear solar-charge slots, leaving grid-charge and discharge intact."""
    for slot in slots:
        if slot.recommendation == Recommendations.BatteriesChargeSolar.value:
            slot.recommendation = None
            slot.batteries_charged_kwh = 0.0


def _remove_grid_charge(slots: list[PlannedSlot]) -> None:
    """Clear grid-charge slots, leaving solar-charge and discharge intact."""
    for slot in slots:
        if slot.recommendation == Recommendations.BatteriesChargeGrid.value:
            slot.recommendation = None
            slot.batteries_charged_kwh = 0.0


def _remove_all_charge(slots: list[PlannedSlot]) -> None:
    """Clear all charge slots, leaving discharge slots intact."""
    for slot in slots:
        if slot.recommendation in _CHARGE_RECS:
            slot.recommendation = None
            slot.batteries_charged_kwh = 0.0

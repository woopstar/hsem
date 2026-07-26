"""Aggressive strategy — force-charge cheapest slots, force-discharge priciest.

This strategy ignores schedule windows and min-price-difference guards.
It provides an upper-bound on arbitrage potential within the planning horizon.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.recommendations import (
    CHARGE_RECS as _CHARGE_RECS,
    DISCHARGE_RECS as _DISCHARGE_RECS,
    Recommendations,
)


def _apply_aggressive_strategy(
    slots: list[PlannedSlot],
    now: datetime,
    max_charge_per_slot: float,
    *,
    current_kwh: float = 0.0,
    usable_kwh: float = 0.0,
    max_discharge_per_slot: float | None = None,
) -> None:
    """Force-charge during the cheapest slots and force-discharge during the priciest.

    This strategy ignores schedule windows and min-price-difference guards.
    It provides an upper-bound on arbitrage potential within the planning horizon.

    Selection criteria:
    - Charge candidates: future slots not already assigned to discharge with the
      lowest import prices.  Charge slots before **all** existing discharge windows
      so that charging is never scheduled after a window it is supposed to serve
      (Bug 5 fix — previously only the *first* discharge window was guarded).
    - Discharge candidates: future slots not already assigned to charge with the
      highest import prices.

    The number of charge slots is derived dynamically from the remaining battery
    headroom so it scales with battery size and horizon length.  Previously a
    hard-coded constant of 3 was used regardless of the horizon, which under-
    utilised the battery for large systems and over-committed it for small ones
    (Bug 2 fix, issue #416).

    The number of discharge slots is also derived dynamically from
    ``ceil(usable_kwh / max_discharge_per_slot)`` so it matches the battery's
    actual discharge capacity (same formula as ``top_n`` in engine.py).

    Args:
        slots: Mutable slot list to update in place.
        now: Timezone-aware current datetime used to filter past slots.
        max_charge_per_slot: Maximum energy storable per slot (kWh).
        current_kwh: Current battery energy above the discharge floor (kWh).
        usable_kwh: Maximum usable battery capacity (kWh).
        max_discharge_per_slot: Maximum energy dischargeable per slot (kWh).
            ``None`` means unlimited (fallback to 3).
    """
    import math

    future = [s for s in slots if as_tz(s.end, now.tzinfo) > now]

    # -----------------------------------------------------------------------
    # Bug 2 fix: derive N dynamically from battery headroom.
    # Compute how many slots are needed to fill the battery from its current
    # charge to max capacity.  Fall back to 3 when capacity data is absent.
    # When the battery is already full (headroom ≈ 0) the strategy claims
    # 0 charge slots — there is no point charging a full battery.
    # -----------------------------------------------------------------------
    headroom_kwh = max(usable_kwh - current_kwh, 0.0)
    if abs(max_charge_per_slot) > 1e-9:
        if headroom_kwh > 1e-9:
            aggressive_charge_slots = math.ceil(headroom_kwh / max_charge_per_slot)
        else:
            aggressive_charge_slots = 0  # battery full — no charging needed
    else:
        aggressive_charge_slots = 3  # safe fallback when inputs are degenerate

    # -----------------------------------------------------------------------
    # Bug 5 fix: guard against ALL discharge windows — both baseline and
    # prospective, not just the first baseline window.  We apply charge
    # first, then discharge, then remove any charge slot that starts at or
    # after the first discharge slot (Bug D fix).
    # -----------------------------------------------------------------------
    if max_discharge_per_slot is not None and max_discharge_per_slot > 1e-9:
        aggressive_discharge_slots = math.ceil(usable_kwh / max_discharge_per_slot)
    else:
        aggressive_discharge_slots = 3  # safe fallback

    # Apply force-charge to cheapest N slots (N derived dynamically above).
    # Two-pass approach: (a) collect the N cheapest slots (prefer later slots
    # among equal prices), (b) among those, assign latest-first so unforecast
    # PV has a chance to cover the need before grid charging actually runs.
    charge_candidates = [s for s in future if s.recommendation not in _DISCHARGE_RECS]
    # Phase 1: sort by price only, take the N cheapest
    price_sorted = sorted(
        charge_candidates,
        key=lambda s: s.price.import_price,
    )
    selected = price_sorted[:aggressive_charge_slots]
    # Phase 2: within selected, assign latest-first
    for slot in sorted(selected, key=lambda s: s.start, reverse=True):
        if slot.recommendation in _CHARGE_RECS:
            continue
        slot.recommendation = Recommendations.BatteriesChargeGrid.value
        slot.batteries_charged_kwh = round(max_charge_per_slot, 3)

    # Apply force-discharge to most-expensive M slots.
    discharge_candidates = sorted(
        (s for s in future if s.recommendation not in _CHARGE_RECS),
        key=lambda s: (-s.price.import_price, s.start),
    )
    discharged = 0
    for slot in discharge_candidates:
        if discharged >= aggressive_discharge_slots:
            break
        if slot.recommendation in _DISCHARGE_RECS:
            discharged += 1
            continue
        slot.recommendation = Recommendations.BatteriesDischargeMode.value
        discharged += 1

    # Post-hoc overlap cleanup (Bug D fix): remove any charge slot that
    # starts at or after the earliest discharge slot start.  This prevents
    # charging from being scheduled after the discharge it is meant to serve.
    first_discharge_start = min(
        (s.start for s in future if s.recommendation in _DISCHARGE_RECS),
        default=None,
    )
    if first_discharge_start is not None:
        for slot in slots:
            if (
                slot.recommendation in _CHARGE_RECS
                and slot.start >= first_discharge_start
            ):
                slot.recommendation = None
                slot.batteries_charged_kwh = 0.0

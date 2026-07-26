"""BatPred-inspired SoC plan: charge only what's needed, then hold.

This strategy identifies discharge windows, calculates the energy needed,
scales it by a charge fraction, and charges only enough to cover the need
using the cheapest slots before the first discharge window.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.misc import (
    calculate_recommended_threshold,
    clamp_efficiency,
)
from custom_components.hsem.utils.recommendations import (
    CHARGE_RECS as _CHARGE_RECS,
    DISCHARGE_RECS as _DISCHARGE_RECS,
    Recommendations,
)


def _apply_soc_plan(
    slots: list[PlannedSlot],
    now: datetime,
    max_charge_per_slot: float,
    *,
    current_kwh: float = 0.0,
    usable_kwh: float = 0.0,
    cycle_cost_per_kwh: float = 0.0,
    charge_fraction: float = 1.0,
    charge_efficiency_pct: float = 97.0,
    discharge_efficiency_pct: float = 97.0,
    purchase_price: float = 0.0,
    expected_cycles: int = 6000,
    capacity_loss_pct: float = 30.0,
) -> float | None:
    """BatPred-inspired SoC plan: charge only what's needed, then hold.

    This strategy:
    1. Identifies all discharge windows (slots with BatteriesDischargeMode).
    2. Calculates the total net energy needed across all discharge windows.
    3. Scales the needed energy by *charge_fraction* (e.g. 0.50 charges
       half of what's needed, 1.25 charges 25 % extra).
    4. Clears all existing charge/discharge recommendations.
    5. Charges only enough to cover the (scaled) needed energy, using the
       cheapest slots before the first discharge window.
    6. Keeps solar charging where PV surplus exists (free energy).
    7. Leaves remaining slots as None — the seasonal fill pass will assign
       BatteriesWaitMode or BatteriesDischargeMode as appropriate.

    Multiple charge_fraction values are generated as separate candidates
    (soc_plan_25, soc_plan_50, etc.) so the selector can pick the optimal
    charge level instead of a binary "empty or full" decision.

    Unlike the aggressive strategy which fills the battery completely, this
    strategy charges only what's strictly needed, avoiding unnecessary
    cycle wear and conversion losses on energy that won't be used.

    Returns:
        The computed charge_target in kWh (battery-side), or None when no
        discharge windows exist.  Used by the caller for deduplication.

    Args:
        slots: Mutable slot list to update in place.
        now: Timezone-aware current datetime used to filter past slots.
        max_charge_per_slot: Maximum energy storable per slot (kWh).
        current_kwh: Current battery energy above the discharge floor (kWh).
        usable_kwh: Maximum usable battery capacity (kWh).
    """

    future = [s for s in slots if as_tz(s.end, now.tzinfo) > now]

    # Step 1: Identify discharge windows and calculate total energy needed
    discharge_slots = [s for s in future if s.recommendation in _DISCHARGE_RECS]
    if not discharge_slots:
        # No discharge windows — nothing to plan for.  Keep solar charging
        # but clear all grid charging.
        for slot in slots:
            if slot.recommendation == Recommendations.BatteriesChargeGrid.value:
                slot.recommendation = None
                slot.batteries_charged_kwh = 0.0
        return None

    # Total net energy needed across all discharge windows.
    # This is the sum of positive net consumption in each discharge slot.
    total_needed_kwh = sum(
        max(s.estimated_net_consumption_kwh, 0.0) for s in discharge_slots
    )

    # Account for charge and discharge efficiency.
    charge_eff = clamp_efficiency(charge_efficiency_pct)
    discharge_eff = clamp_efficiency(discharge_efficiency_pct)
    battery_energy_needed = total_needed_kwh / discharge_eff

    # Subtract what's already in the battery
    charge_needed = max(battery_energy_needed - current_kwh, 0.0)

    # Cap at usable capacity (don't charge more than the battery can hold)
    charge_needed = min(charge_needed, usable_kwh - current_kwh)
    charge_needed = max(charge_needed, 0.0)

    # When the battery is already mostly charged, charge_needed is tiny
    # and all soc_plan* candidates become identical (no grid charging).
    # Switch to discharge_fraction mode: limit how much of the battery's
    # stored energy is actually used during discharge.
    # discharge_fraction = charge_fraction (same values: 0.25-1.25, 2.0=full).
    # This lets the selector choose partial discharge levels.
    if charge_needed < 0.5 and current_kwh > 1.0:
        # Discharge-fraction mode: only use charge_fraction of battery.
        if charge_fraction >= 1.99:
            discharge_target = usable_kwh  # use full battery
        else:
            discharge_target = current_kwh * charge_fraction
            discharge_target = min(discharge_target, usable_kwh)
            discharge_target = max(discharge_target, 0.5)

        charge_target = 0.0  # no grid charging needed
        # Return discharge_target so the caller's dedup loop can distinguish
        # different fractions — charge_target is always 0.0 in this mode.
        _dedup_target = discharge_target
    else:
        # Normal charge-fraction mode: charge_fraction of what's needed.
        # Apply charge efficiency: to store charge_target kWh in the battery,
        # the grid must supply charge_target / charge_eff kWh (Bug G fix).
        if charge_fraction >= 1.99:
            charge_target = max(usable_kwh - current_kwh, 0.0)
        else:
            charge_target = (charge_needed * charge_fraction) / charge_eff
            charge_target = min(charge_target, usable_kwh - current_kwh)
            charge_target = max(charge_target, 0.0)

        _dedup_target = charge_target

    # Step 2: Clear all existing charge/discharge recommendations
    for slot in slots:
        if slot.recommendation in _CHARGE_RECS | _DISCHARGE_RECS:
            slot.recommendation = None
            slot.batteries_charged_kwh = 0.0

    # Step 3: Re-apply discharge window labels — but in discharge-fraction
    # mode, only apply to the most expensive slots within the discharge_target.
    if charge_needed < 0.5 and current_kwh > 1.0:
        # Sort discharge slots by price descending, keep only the top N
        # that fit within discharge_target (accounting for discharge efficiency).
        sorted_discharge = sorted(
            discharge_slots, key=lambda s: s.price.import_price, reverse=True
        )
        remaining = discharge_target
        kept_discharge: list = []
        for s in sorted_discharge:
            slot_demand = max(s.estimated_net_consumption_kwh, 0.0)
            battery_needed = (
                slot_demand / discharge_eff if discharge_eff > 1e-9 else 0.0
            )
            if battery_needed <= remaining:
                remaining -= battery_needed
                kept_discharge.append(s)
            else:
                break  # Not enough battery — cheaper slots are skipped
        # Ensure at least the most expensive slot is kept
        if not kept_discharge and sorted_discharge:
            kept_discharge = [sorted_discharge[0]]
        for s in kept_discharge:
            s.recommendation = Recommendations.BatteriesDischargeMode.value
        # Remaining discharge slots stay None → will become WaitMode in fill pass
        discharge_slots = kept_discharge
    else:
        for slot in discharge_slots:
            slot.recommendation = Recommendations.BatteriesDischargeMode.value

    # Step 4: Apply solar charging where PV surplus exists (free energy).
    # Sort by estimated_net_consumption ascending (most negative = most PV surplus)
    # so the largest available surplus is consumed first (Bug F fix).
    charged = 0.0
    for slot in sorted(
        (
            s
            for s in future
            if s.recommendation is None
            and s.estimated_net_consumption_kwh is not None
            and s.estimated_net_consumption_kwh < 0.0
        ),
        key=lambda x: (x.estimated_net_consumption_kwh, x.start),
    ):
        if charged >= charge_target:
            break
        available_solar = abs(slot.estimated_net_consumption_kwh)
        energy = min(max_charge_per_slot, charge_target - charged, available_solar)
        if energy > 0:
            slot.recommendation = Recommendations.BatteriesChargeSolar.value
            slot.batteries_charged_kwh = round(energy, 3)
            charged += energy

    # Step 5: Charge remaining needed energy from cheapest grid slots
    # before the first discharge window, but only if the price spread
    # covers the cycle cost (avoid uneconomical cycling).
    if not discharge_slots:
        return None  # No discharge slots after filtering — nothing to plan for

    first_discharge_start = min(as_tz(s.start, now.tzinfo) for s in discharge_slots)

    # Average discharge price — what we'd save by discharging instead of importing
    avg_discharge_price = (
        sum(s.price.import_price for s in discharge_slots) / len(discharge_slots)
        if discharge_slots
        else 0.0
    )

    grid_candidates = sorted(
        (
            s
            for s in future
            if s.recommendation is None
            and as_tz(s.end, now.tzinfo) <= first_discharge_start
        ),
        key=lambda x: (x.price.import_price, -x.start.timestamp()),
    )

    # Only charge when the price spread covers the depreciation + cycle cost.
    # This mirrors the guard in _apply_grid_charge which uses:
    #   min_diff = recommended_threshold + cycle_cost_per_kwh
    # where recommended_threshold is the depreciation-derived price floor.
    cheapest_price = grid_candidates[0].price.import_price if grid_candidates else 0.0
    price_spread = avg_discharge_price - cheapest_price
    # Use the canonical calculation instead of a hardcoded proxy.
    approx_threshold = calculate_recommended_threshold(
        purchase_price=purchase_price,
        expected_cycles=expected_cycles,
        usable_capacity=usable_kwh,
        capacity_loss_pct=capacity_loss_pct,
    )
    min_profitable_spread = approx_threshold + cycle_cost_per_kwh

    if price_spread < min_profitable_spread - 1e-9:
        # Spread too small — skip grid charging, just let solar charging happen
        pass
    else:
        for slot in grid_candidates:
            if charged >= charge_target:
                break
            energy = min(max_charge_per_slot, charge_target - charged)
            if energy > 0:
                slot.recommendation = Recommendations.BatteriesChargeGrid.value
                slot.batteries_charged_kwh = round(energy, 3)
                charged += energy

    # Step 6: Remaining slots stay as None — the seasonal fill pass will
    # assign BatteriesWaitMode or BatteriesDischargeMode as appropriate.

    return _dedup_target

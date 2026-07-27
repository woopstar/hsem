"""EV charger power computation and plan-building helpers."""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.planner.ev_planner import (
    EVChargingPlan,
    EVPlannerInput,
    apply_ev_planned_load_to_slots,
    build_ev_charging_plan,
)
from custom_components.hsem.utils.logger import log_planner


def _compute_ev_charger_power(
    slots: list,
    slot_starts: list[datetime],
    ev_plan: EVChargingPlan | None,
    interval_minutes: int,
    now: datetime,
    *,
    second: bool = False,
) -> None:
    """Compute per-slot EV charger target power (W) and write to slots.

    ``EVChargingSlot.ac_load_kwh`` is the AC-side energy the charger draws
    from the grid/PV.  The target power is::

        AC power (W) = (ac_load_kwh / slot_hours) × 1000

    For the **current** (partially elapsed) slot the divisor is the
    remaining slot duration, not the full slot width, because the EV
    planner already scales ``ac_load_kwh`` to the remaining minutes.
    Using the full slot width would understate the required charge power.

    When the plan is ``None`` or empty the field stays at the default 0.0.

    Args:
        slots: Mutable planner slot list to update in place.
        slot_starts: Slot start datetimes (same length as *slots*).
        ev_plan: EV charging plan (may be ``None``).
        interval_minutes: Slot width in minutes.
        now: Current time (timezone-aware), used to detect the current slot.
        second: If ``True``, write to ``ev_second_charger_calculated_power``;
            otherwise write to ``ev_charger_calculated_power``.
    """
    if ev_plan is None or not ev_plan.charging_slots:
        return

    # Build a lookup from UTC key → slot index.
    from custom_components.hsem.utils.datetime_utils import utc_key

    slot_map = {utc_key(s): i for i, s in enumerate(slot_starts)}
    full_hours = interval_minutes / 60.0

    for ev_slot in ev_plan.charging_slots:
        idx = slot_map.get(utc_key(ev_slot.start))
        if idx is None:
            continue
        if ev_slot.ac_load_kwh < 1e-9:
            continue

        # For the current (partially elapsed) slot, the EV planner has
        # already scaled ``ac_load_kwh`` to the *remaining* minutes.
        # Divide by remaining hours to get the correct target power.
        # For future slots the full slot width is used.
        slot_end = slots[idx].end
        if slots[idx].start <= now < slot_end:
            remaining_min = max((slot_end - now).total_seconds() / 60.0, 0.0167)
            slot_hours = remaining_min / 60.0
        else:
            slot_hours = full_hours

        ac_power_w = round((ev_slot.ac_load_kwh / slot_hours) * 1000)

        # Cap at the charger's rated AC power — the EV planner may allocate
        # a full slot's worth of energy to a slot with only a few minutes
        # remaining.  The charger physically cannot exceed its nameplate.
        if ev_plan.charger_power_kw > 1e-9:
            max_ac_power_w = round(ev_plan.charger_power_kw * 1000)
            ac_power_w = min(ac_power_w, max_ac_power_w)

        # Floor at the charger's minimum operating power — if the target
        # power is below the minimum the charger needs to start, it will
        # never deliver any energy.  Zero out the field so the applier
        # does not attempt to throttle the charger below its minimum.
        if (
            ev_plan.charger_min_power_w > 1e-9
            and ac_power_w < ev_plan.charger_min_power_w
        ):
            ac_power_w = 0

        attr = (
            "ev_second_charger_calculated_power"
            if second
            else "ev_charger_calculated_power"
        )
        setattr(slots[idx], attr, ac_power_w)


def _build_and_inject_for_ev(
    enabled: bool,
    connected: bool,
    smart: bool,
    soc: float,
    target: float,
    cap_kwh: float,
    pwr_kw: float,
    eff: float,
    min_pwr_w: float,
    deadline: datetime | None,
    base_includes: bool,
    allow_past_target: bool,
    label: str,
    now: datetime,
    slots: list,
    slot_starts: list,
    slot_ends: list,
    slot_prices: list,
    slot_net_surplus: list[float],
    combined_ev_raw_load: list[float],
    combined_ev_injected_load: list[float],
    warnings: list[str],
) -> EVChargingPlan | None:
    """Build an EV charging plan and accumulate its loads."""
    if not enabled:
        return None
    log_planner(
        "debug",
        "[core] _build_and_inject_for_ev  label=%s  connected=%s  smart=%s  "
        "soc=%.1f%%  target=%.1f%%  cap=%.2f  pwr=%.2f  eff=%.1f%%  min_pwr=%.0fW",
        label,
        connected,
        smart,
        soc,
        target,
        cap_kwh,
        pwr_kw,
        eff,
        min_pwr_w,
    )
    ev_inp = EVPlannerInput(
        enabled=enabled,
        ev_connected=connected,
        smart_charging_enabled=smart,
        current_soc_pct=soc,
        target_soc_pct=target,
        battery_capacity_kwh=cap_kwh,
        charger_power_kw=pwr_kw,
        charger_efficiency_pct=eff,
        charger_min_power_w=min_pwr_w,
        deadline=deadline,
        base_load_includes_ev=base_includes,
        allow_charge_past_target_soc=allow_past_target,
        now=now,
    )
    plan = build_ev_charging_plan(
        ev_inp,
        slots_start=slot_starts,
        slots_end=slot_ends,
        slot_net_surplus_kwh=slot_net_surplus,
        slot_import_price=slot_prices,
    )
    raw = [0.0] * len(slots)
    apply_ev_planned_load_to_slots(
        slot_starts=slot_starts,
        slot_ev_planned_load_kwh=raw,
        ev_plan=plan,
        base_load_includes_ev=False,
    )
    for i in range(len(slots)):
        combined_ev_raw_load[i] += raw[i]
    inj = [0.0] * len(slots)
    apply_ev_planned_load_to_slots(
        slot_starts=slot_starts,
        slot_ev_planned_load_kwh=inj,
        ev_plan=plan,
        base_load_includes_ev=base_includes,
    )
    for i in range(len(slots)):
        combined_ev_injected_load[i] += inj[i]
    if plan.state not in ("not_connected", "smart_charging_disabled", "fully_charged"):
        warnings.append(
            f"EV planned load ({label}): state={plan.state}, total_kwh_needed={plan.total_kwh_needed:.2f}, charging_slots={len(plan.charging_slots)}, base_load_includes_ev={base_includes}."
        )
    log_planner(
        "debug",
        "[core] _build_and_inject_for_ev DONE  label=%s  state=%s  slots=%d  "
        "total_kwh=%.3f",
        label,
        plan.state,
        len(plan.charging_slots),
        plan.total_kwh_needed,
    )
    return plan

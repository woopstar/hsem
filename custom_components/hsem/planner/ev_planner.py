"""EV planned load module for the HSEM planner.

Single responsibility: compute per-slot EV planned charging energy from EV
configuration and state, and inject ``ev_planned_load_kwh`` into planner slots
**before** net consumption and solar surplus are recalculated.

Design goals:
- No circular dependency: EV plan is built once from raw inputs, independent of
  home-battery planner output.
- No double-counting: the caller specifies whether house load already includes
  EV.  When it does, planned EV load is not added again.
- Deadline safety: slots beyond the deadline receive zero EV load.
- Partial current slot: the current slot is scaled by remaining minutes.
- Slot selection: prefer solar-surplus slots first, then cheapest import slots,
  up to ``energy_needed_kwh``.

All functions are pure (no I/O, no HA imports).  The module is safe to call
from synchronous test code.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.const import STATE_UNAVAILABLE

from custom_components.hsem.planner.ev_planner_models import (  # noqa: F401
    EVChargingPlan,
    EVChargingSlot,
    EVPlannerInput,
)
from custom_components.hsem.utils.datetime_utils import utc_key
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import clamp_efficiency
from custom_components.hsem.utils.units import ev_dc_to_ac_kwh

# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def compute_ev_energy_needed(
    current_soc_pct: float,
    target_soc_pct: float,
    battery_capacity_kwh: float,
) -> float:
    """Return EV energy needed to reach target SoC from current SoC.

    The returned value is the energy to be **delivered to the EV battery**
    (DC side, post charger-efficiency).  To find the AC grid/PV draw divide
    by the charger efficiency fraction.

    Args:
        current_soc_pct: Current EV battery SoC (0–100).
        target_soc_pct: Desired EV battery SoC (0–100).
        battery_capacity_kwh: EV battery nameplate capacity in kWh.

    Returns:
        kWh of energy to deliver to EV battery (≥ 0).
    """
    delta = max(target_soc_pct - current_soc_pct, 0.0)
    return max(delta / 100.0 * battery_capacity_kwh, 0.0)


def slot_duration_minutes(start: datetime, end: datetime) -> float:
    """Return slot duration in minutes (float, ≥ 0)."""
    return max((end - start).total_seconds() / 60.0, 0.0)


def max_charge_energy_for_slot(
    slot_duration_min: float,
    charger_power_kw: float,
    charger_efficiency_pct: float = 100.0,
) -> float:
    """Return the maximum energy deliverable to the EV battery in one slot.

    This is the **battery-side** (DC) energy delivered to the EV battery after
    charger efficiency losses.  The AC draw from the grid or PV is
    ``charger_power_kw × hours`` — larger than the returned value when
    ``charger_efficiency_pct < 100``.

    Args:
        slot_duration_min: Duration of the slot in minutes.
        charger_power_kw: Charger AC output power in kW.
        charger_efficiency_pct: Charger efficiency (0–100 %).

    Returns:
        kWh delivered to the EV battery (battery-side, post-efficiency).
    """
    hours = slot_duration_min / 60.0
    return charger_power_kw * hours * clamp_efficiency(charger_efficiency_pct)


def remaining_minutes_in_slot(now: datetime, slot_end: datetime) -> float:
    """Return minutes remaining in the current slot (clamped to ≥ 0)."""
    return max((slot_end - now).total_seconds() / 60.0, 0.0)


def _max_planning_horizon_end(now: datetime) -> datetime:
    """Return the latest instant the EV planner is allowed to schedule into.

    The EV charging window is **always** rooted at ``now`` and may span at
    most **one midnight crossing**, i.e. it may extend into tomorrow but
    must not reach into the day after tomorrow.  Concretely, the returned
    value is the local-midnight that starts the day after tomorrow — the
    first instant the planner must NOT touch.

    Examples (with ``now`` in Europe/Copenhagen):

    - ``now = 2024-06-15 14:00`` → returns ``2024-06-17 00:00``.
      Window allows today afternoon + all of tomorrow.  One midnight crossed
      (the today→tomorrow boundary at 2024-06-16 00:00).
    - ``now = 2024-06-15 23:55`` → returns ``2024-06-17 00:00``.
      Window of ~24 h 5 min, still one midnight crossing.
    - ``now = 2024-06-15 00:00`` → returns ``2024-06-17 00:00``.
      Exactly 48 h window, one midnight crossing.

    The returned instant uses ``now``'s timezone so "midnight" refers to the
    user's local time, not UTC.  Across DST transitions ``replace(hour=0)``
    pins the local clock value as a user would expect.

    Args:
        now: Timezone-aware current datetime.

    Returns:
        Timezone-aware datetime for the start of the day after tomorrow in
        ``now``'s timezone.  Slots starting at or after this instant must be
        excluded from the EV charging plan.
    """
    return (now + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)


def _effective_deadline(
    now: datetime,
    user_deadline: datetime | None,
) -> datetime:
    """Return the deadline the EV planner will actually use.

    The effective deadline is the **earlier** of:

    - The user-configured ``user_deadline`` (if any), and
    - The "one-midnight-crossing" horizon cap from
      :func:`_max_planning_horizon_end`.

    When the user has not set a deadline, the horizon cap is used directly.

    This guarantees the EV charging window is at most ``[now, end-of-tomorrow]``
    even when the planner's overall slot horizon extends to 48 h or 72 h.
    Without this clamp the EV scheduler would spread charging across multiple
    days, which is not what users expect from a "must be done by 17:00 tomorrow"
    deadline.

    Args:
        now: Timezone-aware current datetime.
        user_deadline: User-configured charging deadline, or ``None``.

    Returns:
        Timezone-aware datetime.  EV slots starting at or after this instant
        must not be selected.
    """
    horizon_cap = _max_planning_horizon_end(now)
    if user_deadline is None:
        return horizon_cap
    return min(user_deadline, horizon_cap)


def build_ev_charging_plan(
    inp: EVPlannerInput,
    slots_start: list[datetime],
    slots_end: list[datetime],
    slot_net_surplus_kwh: list[float],
    slot_import_price: list[float],
) -> EVChargingPlan:
    """Build an EV charging plan and return per-slot planned loads.

    Selection order:
    1. Slots with net surplus (solar minus house load) are prioritised — free
       energy the house is already not using.
    2. Among remaining slots, cheapest import price first.
    3. Allocation stops once ``energy_needed_kwh`` is satisfied or the
       deadline is reached.

    The surplus parameter ``slot_net_surplus_kwh`` must be derived from the
    *net* load after house consumption, i.e.::

        slot_net_surplus_kwh[i] = max(-estimated_net_consumption[i], 0.0)

    This correctly models that the house uses solar power first; only what
    is left over (net surplus) is available to the EV charger at no extra
    grid cost.  Using raw PV estimates would over-state available free energy.

    The current slot is scaled by its remaining duration, not the full
    slot width, to avoid over-counting energy in the partially elapsed slot.

    Args:
        inp: EV planner inputs.
        slots_start: List of slot start datetimes (same length as other lists).
        slots_end: List of slot end datetimes.
        slot_net_surplus_kwh: Net surplus available per slot (kWh, ≥ 0).  This
            is ``max(-estimated_net_consumption, 0)`` — solar minus house load.
        slot_import_price: Import electricity price per slot.

    Returns:
        An :class:`EVChargingPlan` with state, charging slots, and a
        ``planned_load_by_slot`` mapping.
    """
    plan = EVChargingPlan(
        ev_connected=inp.ev_connected,
        base_load_includes_ev=inp.base_load_includes_ev,
        current_soc_pct=inp.current_soc_pct,
        target_soc_pct=inp.target_soc_pct,
        battery_capacity_kwh=inp.battery_capacity_kwh,
        charger_power_kw=inp.charger_power_kw,
        charger_min_power_w=inp.charger_min_power_w,
    )

    # --- Guard states ---
    if not inp.enabled:
        plan.state = "smart_charging_disabled"
        return plan

    if not inp.ev_connected:
        plan.state = "not_connected"
        return plan

    if not inp.smart_charging_enabled:
        plan.state = "smart_charging_disabled"
        return plan

    if inp.battery_capacity_kwh <= 0 or inp.charger_power_kw <= 0:
        plan.state = STATE_UNAVAILABLE
        plan.data_quality = {
            "error": "battery_capacity_kwh or charger_power_kw is zero"
        }
        return plan

    energy_needed = compute_ev_energy_needed(
        inp.current_soc_pct, inp.target_soc_pct, inp.battery_capacity_kwh
    )
    plan.total_kwh_needed = round(energy_needed, 3)
    plan.deadline = inp.deadline

    log_planner(
        "debug",
        "[ev_planner] build_ev_charging_plan START  soc=%.1f%%  target=%.1f%%  "
        "needed=%.3fkWh  deadline=%s",
        inp.current_soc_pct,
        inp.target_soc_pct,
        energy_needed,
        inp.deadline.isoformat() if inp.deadline else "none",
    )

    # When energy_needed ≈ 0 the EV is at or above target SoC.
    # Charge-past-target is handled exclusively by the MILP — the EV
    # planner plays no role in that decision.
    if abs(energy_needed) < 1e-9:
        plan.state = "fully_charged"
        log_planner(
            "debug",
            "[ev_planner] build_ev_charging_plan DONE  state=fully_charged  "
            "energy_needed=0 (already at or above target SoC)",
        )
        return plan

    # --- Candidate slot filtering (before effective deadline) ---
    #
    # The "effective deadline" is the earlier of the user-configured
    # deadline and the one-midnight-crossing horizon cap (end of tomorrow
    # in ``now``'s timezone).  This guarantees the EV charging window stays
    # rooted at ``now`` and never reaches into the day after tomorrow, even
    # when the planner's overall slot horizon extends to 48 h or 72 h.
    # See ``_effective_deadline`` for details.
    now_tz = inp.now
    effective_deadline = _effective_deadline(now_tz, inp.deadline)
    # We surface a diagnostic in ``plan.data_quality`` only when the cap
    # actually changed the deadline — otherwise the field is noise.
    deadline_clamped = inp.deadline is not None and effective_deadline < inp.deadline

    candidate_indices: list[int] = []
    for i, (s_start, s_end) in enumerate(zip(slots_start, slots_end)):
        # Skip past slots
        if s_end <= now_tz:
            continue
        # Skip slots starting at or beyond the effective deadline.
        # ``effective_deadline`` is always non-None by construction.
        if s_start >= effective_deadline:
            break
        candidate_indices.append(i)

    if not candidate_indices:
        plan.state = "waiting"
        plan.data_quality = {"warning": "No candidate slots before deadline"}
        if deadline_clamped:
            plan.data_quality["effective_deadline"] = effective_deadline.isoformat()
            plan.data_quality["deadline_clamped"] = True
        log_planner(
            "debug",
            "[ev_planner] build_ev_charging_plan DONE  state=waiting  "
            "no candidate slots before effective_deadline=%s",
            effective_deadline.isoformat(),
        )
        return plan

    # --- Two-pass slot selection ---
    # Pass 1: net-surplus slots (sorted by descending net surplus → free energy first).
    #   Net surplus = max(-estimated_net_consumption, 0) = solar minus house load.
    #   The house already uses solar first; only the leftover is free for the EV.
    # Pass 2: remaining slots sorted by ascending import price.
    surplus_slots = sorted(
        [i for i in candidate_indices if slot_net_surplus_kwh[i] > 1e-9],
        key=lambda i: -slot_net_surplus_kwh[i],
    )
    non_surplus_slots = sorted(
        [i for i in candidate_indices if i not in set(surplus_slots)],
        key=lambda i: slot_import_price[i],
    )
    ordered = surplus_slots + non_surplus_slots

    remaining_energy = energy_needed
    selected: list[EVChargingSlot] = []

    for i in ordered:
        if remaining_energy < 1e-9:
            break

        s_start = slots_start[i]
        s_end = slots_end[i]

        # Scale current slot by remaining minutes
        is_current = s_start <= now_tz < s_end
        if is_current:
            avail_min = remaining_minutes_in_slot(now_tz, s_end)
        else:
            avail_min = slot_duration_minutes(s_start, s_end)

        # Clamp to the effective deadline (one-midnight-crossing horizon
        # cap, possibly tightened further by the user-configured deadline).
        if s_end > effective_deadline:
            avail_min = min(
                avail_min,
                max(
                    (effective_deadline - max(s_start, now_tz)).total_seconds() / 60.0,
                    0.0,
                ),
            )

        max_charge = max_charge_energy_for_slot(
            avail_min, inp.charger_power_kw, inp.charger_efficiency_pct
        )
        allocated = min(max_charge, remaining_energy)
        if allocated < 1e-9:
            continue

        # Check minimum charger power — if the AC power for this slot
        # falls below the charger's minimum operating power, the charger
        # physically cannot start and the energy would not be delivered.
        # Skip the slot so the planner doesn't waste PV surplus or cheap
        # grid slots on allocations that can never be realised.
        eff = clamp_efficiency(inp.charger_efficiency_pct)
        slot_hours = avail_min / 60.0
        ac_power_w = (allocated / eff) / slot_hours * 1000.0
        if ac_power_w < inp.charger_min_power_w - 1e-9:
            log_planner(
                "debug",
                "[ev_planner] slot_skipped  start=%s  end=%s  "
                "reason=ac_power_below_minimum  ac_power_w=%.0f  min_power_w=%.0f",
                s_start.isoformat(),
                s_end.isoformat(),
                ac_power_w,
                inp.charger_min_power_w,
            )
            continue

        # ``allocated`` is battery-side kWh delivered to the EV.
        # AC load = battery-side / charger_efficiency (what grid/PV must supply).
        ac_load = ev_dc_to_ac_kwh(
            allocated, clamp_efficiency(inp.charger_efficiency_pct)
        )

        net_surplus = slot_net_surplus_kwh[i]
        # net_surplus_used / import_needed are expressed as battery-side kWh
        # for the EV plan display; ac_load_kwh is the grid/PV draw used for
        # net consumption and SoC simulation.
        # Net surplus is solar MINUS house load — the energy available to the
        # EV at no extra grid cost, since the house has already consumed solar.
        net_surplus_used = min(allocated, net_surplus)
        import_needed = max(allocated - net_surplus_used, 0.0)
        # Cost = grid AC draw × price.  Grid AC draw = import_needed / eff.
        cost = round((import_needed / eff) * slot_import_price[i], 4)

        ev_slot = EVChargingSlot(
            start=s_start,
            end=s_end,
            estimated_charged_kwh=round(allocated, 3),
            ac_load_kwh=round(ac_load, 3),
            solar_surplus_kwh=round(net_surplus_used, 3),
            import_needed_kwh=round(import_needed, 3),
            import_price=slot_import_price[i],
            estimated_cost=cost,
        )
        selected.append(ev_slot)
        remaining_energy -= allocated

        log_planner(
            "debug",
            "[ev_planner] slot_selected  start=%s  end=%s  "
            "surplus=%.3fkWh  import_price=%.4f  "
            "allocated=%.3fkWh  ac_load=%.3fkWh  "
            "solar_surplus=%.3fkWh  import_needed=%.3fkWh  cost=%.4f",
            s_start.isoformat(),
            s_end.isoformat(),
            net_surplus,
            slot_import_price[i],
            allocated,
            ac_load,
            net_surplus_used,
            import_needed,
            cost,
        )

    # --- Pass 3 removed ---
    # Charge-past-target is now handled exclusively by the MILP.
    # When the MILP wins, it co-optimises the EV alongside the battery
    # with a surplus-only constraint and a tiny tiebreaker benefit
    # (0.0001/kWh AC).  When the MILP fails, the baseline candidate
    # does not attempt charge-past-target — the MILP is the only path.

    # Build output
    plan.charging_slots = selected
    plan.planned_load_by_slot = {
        s.start.isoformat(): s.estimated_charged_kwh for s in selected
    }

    # Identify current slot load
    for s in selected:
        if s.start <= now_tz < s.end:
            plan.current_slot_planned_load_kwh = s.estimated_charged_kwh
            break

    if selected:
        plan.state = (
            "charging" if plan.current_slot_planned_load_kwh > 1e-9 else "waiting"
        )
    else:
        plan.state = "waiting"

    log_planner(
        "debug",
        "[ev_planner] build_ev_charging_plan DONE  state=%s  slots=%d  "
        "total_allocated=%.3fkWh  remaining=%.3fkWh",
        plan.state,
        len(selected),
        energy_needed - remaining_energy,
        remaining_energy,
    )

    # Surface the effective deadline (and whether the one-midnight-crossing
    # cap actually changed the user-configured deadline) so the success path
    # exposes the same diagnostic the "no candidates" path does.  Useful for
    # dashboards and for debugging cases where EV slots appear to be missing
    # from the late part of the horizon.
    if deadline_clamped:
        plan.data_quality["effective_deadline"] = effective_deadline.isoformat()
        plan.data_quality["deadline_clamped"] = True
    elif inp.deadline is None:
        # Even without a user deadline, surface the horizon cap so it's
        # obvious why the EV planner didn't reach further into the horizon.
        plan.data_quality["effective_deadline"] = effective_deadline.isoformat()
        plan.data_quality["deadline_clamped"] = False

    return plan


def apply_ev_planned_load_to_slots(
    slot_starts: list[datetime],
    slot_ev_planned_load_kwh: list[float],
    ev_plan: EVChargingPlan,
    base_load_includes_ev: bool,
) -> None:
    """Accumulate EV planned AC load into the per-slot totals (in-place, additive).

    This function is **always additive** — it never overwrites existing values.
    Call it once per EV plan; primary and secondary EV loads will be summed
    across calls because each call adds to (not replaces) the existing values.

    When ``base_load_includes_ev`` is True the function is a no-op: the EV
    load is already counted in the house consumption baseline and must not be
    injected a second time into net consumption.  The caller is responsible
    for tracking the accounted load separately via the raw EV plan totals.

    Args:
        slot_starts: Slot start datetimes aligned with the planner slot list.
        slot_ev_planned_load_kwh: Mutable list to accumulate into (same
            length as slot_starts).  Existing values are preserved and the
            new EV load is *added* to them.
        ev_plan: Computed EV charging plan.
        base_load_includes_ev: If True, skip injection to avoid double-counting.
    """
    if base_load_includes_ev:
        return

    # Pre-build a lookup from UTC-normalised key → slot index for O(n) matching.
    slot_key_map = {utc_key(s): i for i, s in enumerate(slot_starts)}

    for ev_slot in ev_plan.charging_slots:
        idx = slot_key_map.get(utc_key(ev_slot.start))
        if idx is not None:
            # Accumulate AC-side load (grid/PV draw), not battery-side delivered
            # energy.  With charger_efficiency < 100 %, the AC load is larger
            # than the kWh arriving in the EV battery.  The += operator ensures
            # multiple EVs sharing the same slot are summed, not overwritten.
            slot_ev_planned_load_kwh[idx] += ev_slot.ac_load_kwh


def rebuild_ev_plan_from_slots(
    original_plan: EVChargingPlan,
    slots: list,
    now: datetime,
    charger_efficiency_pct: float = 100.0,
    *,
    is_second: bool = False,
) -> EVChargingPlan:
    """Rebuild an EVChargingPlan from MILP-decided per-EV slot fields.

    When the MILP wins, its per-slot EV decisions (written to
    ``PlannedSlot.ev_charger_calculated_power`` or
    ``PlannedSlot.ev_second_charger_calculated_power``) replace the
    EV planner's original charging plan.  This function scans the winning
    slots and produces an updated :class:`EVChargingPlan` that the sensor
    can display, so the user sees what the system *actually* plans to do
    rather than the EV planner's pre-MILP estimate.

    The original plan provides metadata (SoC, target, capacity, etc.) that
    the MILP does not recompute.

    .. important::

       This function reads **per-EV** power fields, not the combined
       ``ev_planned_load_kwh`` / ``ev_accounted_load_kwh`` totals.
       The combined fields sum across both EVs and cannot distinguish
       which EV contributed how much load (issue #646/#655).

    Args:
        original_plan: The EV planner's original plan (for metadata).
        slots: The winning slot list with MILP-populated EV fields.
        now: Current time (timezone-aware), used to detect the current slot.
        charger_efficiency_pct: Charger efficiency (0–100 %) for converting
            AC load back to DC-side delivered energy.
        is_second: When ``True``, read from
            ``ev_second_charger_calculated_power`` instead of
            ``ev_charger_calculated_power``.  Must match the EV identity
            the caller is rebuilding for.

    Returns:
        A new :class:`EVChargingPlan` with ``charging_slots`` derived from
        the MILP's per-EV slot decisions.
    """
    from custom_components.hsem.utils.datetime_utils import as_tz, slot_contains
    from custom_components.hsem.utils.units import slot_duration_hours

    eff = clamp_efficiency(charger_efficiency_pct)
    charging_slots: list[EVChargingSlot] = []
    planned_load_by_slot: dict[str, float] = {}
    current_slot_planned_load_kwh: float = 0.0
    total_charged_kwh: float = 0.0

    # Read from the per-EV charger power field, not the combined
    # ev_planned_load_kwh / ev_accounted_load_kwh totals (issue #646/#655).
    power_field = (
        "ev_second_charger_calculated_power"
        if is_second
        else "ev_charger_calculated_power"
    )
    for s in slots:
        power_w = getattr(s, power_field, 0.0)
        if power_w < 1e-9:
            continue
        # Current-slot commands apply only for the remaining physical duration.
        slot_hours = (
            slot_duration_hours(max(now, s.start), s.end)
            if slot_contains(s.start, s.end, now)
            else slot_duration_hours(s.start, s.end)
        )
        ac_load = power_w * slot_hours / 1000.0

        # Convert AC load back to DC-side delivered energy for display.
        dc_kwh = ac_load * eff
        total_charged_kwh += dc_kwh

        # Solar/import split: the MILP decides EV charging alongside PV, so
        # any PV surplus in this slot is consumed by the EV first (before
        # the battery).  Attribute as much of the AC load as possible to
        # the slot's PV surplus; the remainder is grid import.  Use the
        # house-netted surplus (PV minus house consumption) so the split
        # matches the energy balance used elsewhere.
        pv_kwh = max(getattr(s, "solcast_pv_estimate_kwh", 0.0), 0.0)
        house_kwh = max(getattr(s, "avg_house_consumption_kwh", 0.0), 0.0)
        surplus_kwh = max(pv_kwh - house_kwh, 0.0)
        solar_used_ac = min(ac_load, surplus_kwh)
        solar_used_dc = solar_used_ac * eff
        import_needed = max(dc_kwh - solar_used_dc, 0.0)

        ev_slot = EVChargingSlot(
            start=s.start,
            end=s.end,
            estimated_charged_kwh=round(dc_kwh, 3),
            ac_load_kwh=round(ac_load, 3),
            solar_surplus_kwh=round(solar_used_dc, 3),
            import_needed_kwh=round(import_needed, 3),
            import_price=getattr(
                getattr(s, "price", None),
                "import_price",
                getattr(s, "import_price", 0.0),
            ),
            estimated_cost=round(
                (import_needed / eff)
                * getattr(
                    getattr(s, "price", None),
                    "import_price",
                    getattr(s, "import_price", 0.0),
                ),
                4,
            ),
        )
        charging_slots.append(ev_slot)
        planned_load_by_slot[s.start.isoformat()] = dc_kwh

        # Detect current slot
        s_start_tz = as_tz(s.start, now.tzinfo)
        s_end_tz = as_tz(s.end, now.tzinfo)
        if s_start_tz <= now < s_end_tz:
            current_slot_planned_load_kwh = dc_kwh

    # Determine state
    if charging_slots:
        state = "charging" if current_slot_planned_load_kwh > 1e-9 else "waiting"
    elif original_plan.state == "fully_charged":
        state = "fully_charged"
    else:
        state = original_plan.state

    return EVChargingPlan(
        state=state,
        ev_connected=original_plan.ev_connected,
        base_load_includes_ev=original_plan.base_load_includes_ev,
        current_soc_pct=original_plan.current_soc_pct,
        target_soc_pct=original_plan.target_soc_pct,
        battery_capacity_kwh=original_plan.battery_capacity_kwh,
        charger_power_kw=original_plan.charger_power_kw,
        charger_min_power_w=original_plan.charger_min_power_w,
        total_kwh_needed=round(total_charged_kwh, 3),
        deadline=original_plan.deadline,
        charging_slots=charging_slots,
        planned_load_by_slot=planned_load_by_slot,
        current_slot_planned_load_kwh=round(current_slot_planned_load_kwh, 3),
        data_quality=original_plan.data_quality,
    )

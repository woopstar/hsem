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

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.hsem.utils.datetime_utils import utc_key

# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------


@dataclass
class EVChargingSlot:
    """Per-slot EV charging plan entry.

    Energy semantics
    ----------------
    EV chargers draw AC power from the grid or from solar and deliver a
    fraction of that energy to the EV battery.

    - ``estimated_charged_kwh``: energy **delivered to the EV battery** (DC
      side, post charger-efficiency loss).  This is what advances the EV SoC.
    - ``ac_load_kwh``: AC energy **consumed from the house/grid/PV side**.
      ``ac_load_kwh = estimated_charged_kwh / charger_efficiency``.
      With 90 % efficiency, 10 kWh delivered ⇒ 11.11 kWh AC load.
      This value is injected into ``PlannedSlot.ev_planned_load_kwh`` so that
      net consumption, SoC simulation, and cost calculations all see the true
      grid/PV demand.

    Other attributes:
        start: Timezone-aware start of the slot.
        end: Timezone-aware end of the slot.
        solar_surplus_kwh: Solar surplus (battery-side) used for EV charging.
        import_needed_kwh: Battery-side energy from grid (= estimated_charged_kwh
            − solar_surplus_kwh).
        import_price: Import price for this slot (currency/kWh).
        estimated_cost: Estimated grid cost for EV charging this slot.
    """

    start: datetime
    end: datetime
    estimated_charged_kwh: float = 0.0
    ac_load_kwh: float = 0.0
    solar_surplus_kwh: float = 0.0
    import_needed_kwh: float = 0.0
    import_price: float = 0.0
    estimated_cost: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for HA sensor attributes."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "estimated_charged_kwh": round(self.estimated_charged_kwh, 3),
            "ac_load_kwh": round(self.ac_load_kwh, 3),
            "solar_surplus_kwh": round(self.solar_surplus_kwh, 3),
            "import_needed_kwh": round(self.import_needed_kwh, 3),
            "import_price": round(self.import_price, 4),
            "estimated_cost": round(self.estimated_cost, 4),
        }


@dataclass
class EVPlannerInput:
    """All inputs required to compute an EV charging plan.

    Attributes:
        enabled: Whether EV planned load integration is active.
        ev_connected: True when a vehicle is physically plugged in.
        smart_charging_enabled: True when smart charging is permitted.
        current_soc_pct: Vehicle battery SoC in percent (0–100).
        target_soc_pct: Target SoC in percent (0–100).
        battery_capacity_kwh: EV battery nameplate capacity in kWh.
        charger_power_kw: Charger output power in kW.
        charger_efficiency_pct: Charger efficiency as a percentage (0–100).
        deadline: Timezone-aware datetime by which charging must be complete.
        base_load_includes_ev: True when the house consumption sensor already
            includes EV charging power.  When True, planned EV load must not
            be added to net consumption a second time.
        allow_charge_past_target_soc: When True, the planner may continue
            charging past the target SoC using surplus PV that would otherwise
            be curtailed (e.g. battery full, negative export prices).
            Only applies when the EV has reached target SoC but is below 100 %.
        now: Timezone-aware current datetime.
    """

    enabled: bool = False
    ev_connected: bool = False
    smart_charging_enabled: bool = True
    current_soc_pct: float = 0.0
    target_soc_pct: float = 80.0
    battery_capacity_kwh: float = 0.0
    charger_power_kw: float = 0.0
    charger_efficiency_pct: float = 100.0
    deadline: datetime | None = None
    base_load_includes_ev: bool = False
    allow_charge_past_target_soc: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class EVChargingPlan:
    """Output of the EV charging planner.

    Attributes:
        state: Human-readable state string for the HA sensor.
        ev_connected: Whether the EV is connected.
        current_soc_pct: EV battery SoC at plan time.
        target_soc_pct: Target SoC.
        battery_capacity_kwh: EV battery capacity.
        charger_power_kw: Charger rated power.
        total_kwh_needed: Total energy needed to reach target.
        deadline: Planning deadline.
        charging_slots: Selected slots with per-slot details.
        planned_load_by_slot: Mapping of slot-start ISO string → planned kWh.
        current_slot_planned_load_kwh: Load allocated to the current slot.
        data_quality: Structured diagnostics dict.
    """

    state: str = "unavailable"
    ev_connected: bool = False
    base_load_includes_ev: bool = False
    current_soc_pct: float = 0.0
    target_soc_pct: float = 80.0
    battery_capacity_kwh: float = 0.0
    charger_power_kw: float = 0.0
    total_kwh_needed: float = 0.0
    deadline: datetime | None = None
    charging_slots: list[EVChargingSlot] = field(default_factory=list)
    planned_load_by_slot: dict[str, float] = field(default_factory=dict)
    current_slot_planned_load_kwh: float = 0.0
    data_quality: dict[str, Any] = field(default_factory=dict)

    def as_attributes(self) -> dict[str, Any]:
        """Serialise to HA sensor attributes dict."""
        return {
            "battery_capacity_kwh": round(self.battery_capacity_kwh, 2),
            "charge_power_kw": round(self.charger_power_kw, 2),
            "current_soc": round(self.current_soc_pct, 1),
            "target_soc": round(self.target_soc_pct, 1),
            "ev_connected": self.ev_connected,
            "base_load_includes_ev": self.base_load_includes_ev,
            "total_kwh_needed": round(self.total_kwh_needed, 3),
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "charging_slots": [s.as_dict() for s in self.charging_slots],
            "planned_load_by_slot": {
                k: round(v, 3) for k, v in self.planned_load_by_slot.items()
            },
            "current_slot_planned_load_kwh": round(
                self.current_slot_planned_load_kwh, 3
            ),
            "data_quality": self.data_quality,
        }


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
    eff = max(charger_efficiency_pct, 1.0) / 100.0
    return charger_power_kw * hours * eff


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
        plan.state = "unavailable"
        plan.data_quality = {
            "error": "battery_capacity_kwh or charger_power_kw is zero"
        }
        return plan

    energy_needed = compute_ev_energy_needed(
        inp.current_soc_pct, inp.target_soc_pct, inp.battery_capacity_kwh
    )
    plan.total_kwh_needed = round(energy_needed, 3)
    plan.deadline = inp.deadline

    if abs(energy_needed) < 1e-9:
        plan.state = "fully_charged"
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

        # ``allocated`` is battery-side kWh delivered to the EV.
        # AC load = battery-side / charger_efficiency (what grid/PV must supply).
        eff = max(inp.charger_efficiency_pct, 1.0) / 100.0
        ac_load = allocated / eff

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

    # --- Pass 3: charge past target on surplus PV only ---
    # When allow_charge_past_target_soc is enabled and the EV has reached
    # its target SoC (remaining_energy ≈ 0), continue charging from any
    # remaining PV-surplus slots.  These slots are free — the energy would
    # otherwise be curtailed when the battery is full and export prices are
    # negative.  Only slots that are NOT already in the plan are considered.
    if (
        inp.allow_charge_past_target_soc
        and remaining_energy < 1e-9
        and inp.current_soc_pct < 100
    ):
        used_starts = {s.start for s in selected}
        # Re-scan surplus slots, skipping those already allocated.
        for i in surplus_slots:
            s_start = slots_start[i]
            s_end = slots_end[i]
            if s_start in used_starts:
                continue
            if (
                max_charge_energy_for_slot(
                    slot_duration_minutes(s_start, s_end),
                    inp.charger_power_kw,
                    inp.charger_efficiency_pct,
                )
                < 1e-9
            ):
                continue

            # The EV already reached target — no strict energy budget.
            # Use as much surplus as possible up to the full slot capacity,
            # limited by the available surplus.
            avail_min = slot_duration_minutes(s_start, s_end)
            max_charge = max_charge_energy_for_slot(
                avail_min, inp.charger_power_kw, inp.charger_efficiency_pct
            )
            net_surplus = slot_net_surplus_kwh[i]
            # Allocate the minimum of max power and available surplus.
            # We charge only from surplus, never from grid in this pass.
            allocated = min(max_charge, net_surplus)
            if allocated < 1e-9:
                continue

            eff = max(inp.charger_efficiency_pct, 1.0) / 100.0
            ac_load = allocated / eff

            ev_slot = EVChargingSlot(
                start=s_start,
                end=s_end,
                estimated_charged_kwh=round(allocated, 3),
                ac_load_kwh=round(ac_load, 3),
                solar_surplus_kwh=round(allocated, 3),
                import_needed_kwh=0.0,
                import_price=slot_import_price[i],
                estimated_cost=0.0,
            )
            selected.append(ev_slot)

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

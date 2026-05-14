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
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------


@dataclass
class EVChargingSlot:
    """Per-slot EV charging plan entry.

    Two distinct energy quantities are tracked for each slot:

    - ``estimated_charged_kwh`` — energy *delivered* to the EV battery, the
      value that counts toward the SoC target.
    - ``ac_load_kwh`` — AC-side load drawn from PV or the grid, equal to
      ``estimated_charged_kwh / (charger_efficiency_pct / 100)``.  This is
      the value that enters the planner's energy balance and is used to
      derive ``solar_surplus_kwh`` and ``import_needed_kwh``.

    When ``charger_efficiency_pct == 100`` the two values are equal and
    behaviour matches the legacy single-value model.

    Attributes:
        start: Timezone-aware start of the slot.
        end: Timezone-aware end of the slot.
        estimated_charged_kwh: EV energy delivered toward the SoC target.
        ac_load_kwh: AC-side load drawn by the charger this slot.
        solar_surplus_kwh: Solar surplus consumed by this EV in this slot
            (AC domain, never exceeds the remaining shared surplus).
        import_needed_kwh: Grid import required for EV charging in this slot
            (AC domain).
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

    Args:
        current_soc_pct: Current EV battery SoC (0–100).
        target_soc_pct: Desired EV battery SoC (0–100).
        battery_capacity_kwh: EV battery nameplate capacity in kWh.

    Returns:
        kWh of charging energy needed (≥ 0).
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

    Args:
        slot_duration_min: Duration of the slot in minutes.
        charger_power_kw: Charger AC output power in kW.
        charger_efficiency_pct: Charger efficiency (0–100 %).

    Returns:
        kWh that can be delivered to the EV battery.
    """
    hours = slot_duration_min / 60.0
    eff = max(charger_efficiency_pct, 1.0) / 100.0
    return charger_power_kw * hours * eff


def remaining_minutes_in_slot(now: datetime, slot_end: datetime) -> float:
    """Return minutes remaining in the current slot (clamped to ≥ 0)."""
    return max((slot_end - now).total_seconds() / 60.0, 0.0)


def build_ev_charging_plan(
    inp: EVPlannerInput,
    slots_start: list[datetime],
    slots_end: list[datetime],
    slot_solar_surplus_kwh: list[float],
    slot_import_price: list[float],
) -> EVChargingPlan:
    """Build an EV charging plan and return per-slot planned loads.

    Selection order:
    1. Slots with solar surplus are prioritised (free energy for EV).
    2. Among remaining slots, cheapest import price first.
    3. Allocation stops once ``energy_needed_kwh`` is satisfied or the
       deadline is reached.

    The current slot is scaled by its remaining duration, not the full
    slot width, to avoid over-counting energy in the partially elapsed slot.

    Multi-EV solar allocation:
        ``slot_solar_surplus_kwh`` is treated as a shared, mutable budget.
        Whenever this EV consumes solar surplus in a slot, the matching
        entry is decremented in place so that a later EV (planned in the
        same pass) sees only what is left.  Callers wanting an
        independent allocation must pass a copy of the surplus list.

    Charger efficiency domain:
        ``estimated_charged_kwh`` is the energy delivered to the EV
        battery (counts toward the SoC target).  ``ac_load_kwh`` is the
        AC-side load drawn from PV or the grid and equals
        ``estimated_charged_kwh / (charger_efficiency_pct / 100)``.
        ``solar_surplus_kwh`` and ``import_needed_kwh`` are reported in
        the AC domain so the planner's energy balance and grid-import
        accounting stay consistent.

    Args:
        inp: EV planner inputs.
        slots_start: List of slot start datetimes (same length as other lists).
        slots_end: List of slot end datetimes.
        slot_solar_surplus_kwh: Solar surplus available per slot (kWh, ≥ 0).
            Mutated in place: surplus claimed by this EV is subtracted so
            subsequent EVs share the remaining budget.
        slot_import_price: Import electricity price per slot.

    Returns:
        An :class:`EVChargingPlan` with state, charging slots, and a
        ``planned_load_by_slot`` mapping (AC-domain load per slot).
    """
    plan = EVChargingPlan(
        ev_connected=inp.ev_connected,
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

    # --- Candidate slot filtering (before deadline) ---
    # Find the current slot index (first slot that contains now or is in future)
    now_tz = inp.now
    deadline = inp.deadline

    candidate_indices: list[int] = []
    for i, (s_start, s_end) in enumerate(zip(slots_start, slots_end)):
        # Skip past slots
        if s_end <= now_tz:
            continue
        # Skip slots beyond deadline
        if deadline is not None and s_start >= deadline:
            break
        candidate_indices.append(i)

    if not candidate_indices:
        plan.state = "waiting"
        plan.data_quality = {"warning": "No candidate slots before deadline"}
        return plan

    # --- Two-pass slot selection ---
    # Pass 1: solar surplus slots (sorted by descending surplus → free energy first)
    # Pass 2: remaining slots sorted by ascending import price
    solar_slots = sorted(
        [i for i in candidate_indices if slot_solar_surplus_kwh[i] > 1e-9],
        key=lambda i: -slot_solar_surplus_kwh[i],
    )
    non_solar_slots = sorted(
        [i for i in candidate_indices if i not in set(solar_slots)],
        key=lambda i: slot_import_price[i],
    )
    ordered = solar_slots + non_solar_slots

    remaining_energy = energy_needed
    selected: list[EVChargingSlot] = []

    # Charger efficiency factor; clamp to >=1% so we never divide by zero.
    eff_fraction = max(inp.charger_efficiency_pct, 1.0) / 100.0

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

        # Clamp to deadline
        if deadline is not None and s_end > deadline:
            avail_min = min(
                avail_min,
                max((deadline - max(s_start, now_tz)).total_seconds() / 60.0, 0.0),
            )

        max_charge = max_charge_energy_for_slot(
            avail_min, inp.charger_power_kw, inp.charger_efficiency_pct
        )
        delivered = min(max_charge, remaining_energy)
        if delivered < 1e-9:
            continue

        # AC-side load drawn from PV/grid for this delivered energy.
        ac_load = delivered / eff_fraction

        # Solar surplus is a shared, AC-domain budget across EVs in this pass.
        surplus = max(slot_solar_surplus_kwh[i], 0.0)
        solar_used = min(ac_load, surplus)
        import_needed = max(ac_load - solar_used, 0.0)
        # Decrement the shared surplus so the next EV sees what is left.
        slot_solar_surplus_kwh[i] = max(surplus - solar_used, 0.0)

        cost = import_needed * slot_import_price[i]

        ev_slot = EVChargingSlot(
            start=s_start,
            end=s_end,
            estimated_charged_kwh=round(delivered, 3),
            ac_load_kwh=round(ac_load, 3),
            solar_surplus_kwh=round(solar_used, 3),
            import_needed_kwh=round(import_needed, 3),
            import_price=slot_import_price[i],
            estimated_cost=round(cost, 4),
        )
        selected.append(ev_slot)
        remaining_energy -= delivered

    # Build output
    plan.charging_slots = selected
    # AC-domain load is what the planner injects into slot.ev_planned_load_kwh.
    plan.planned_load_by_slot = {s.start.isoformat(): s.ac_load_kwh for s in selected}

    # Identify current slot load (AC-domain, matches planner injection)
    for s in selected:
        if s.start <= now_tz < s.end:
            plan.current_slot_planned_load_kwh = s.ac_load_kwh
            break

    if selected:
        plan.state = (
            "charging" if plan.current_slot_planned_load_kwh > 1e-9 else "waiting"
        )
    else:
        plan.state = "waiting"

    return plan


def apply_ev_planned_load_to_slots(
    slot_starts: list[datetime],
    slot_ev_planned_load_kwh: list[float],
    ev_plan: EVChargingPlan,
    base_load_includes_ev: bool,
) -> None:
    """Inject EV planned load into the per-slot EV load list (in-place).

    When ``base_load_includes_ev`` is True the function is a no-op because
    the EV load is already counted in the house consumption baseline.

    Args:
        slot_starts: Slot start datetimes aligned with the planner slot list.
        slot_ev_planned_load_kwh: Mutable list to update (same length as slot_starts).
        ev_plan: Computed EV charging plan.
        base_load_includes_ev: If True, skip injection to avoid double-counting.
    """
    if base_load_includes_ev:
        return
    for ev_slot in ev_plan.charging_slots:
        key = ev_slot.start.isoformat()
        for i, s in enumerate(slot_starts):
            if s.isoformat() == key:
                # Inject AC-domain load: this is what the house actually draws
                # from PV/grid for this EV charging.  At 100 % efficiency this
                # equals estimated_charged_kwh; below 100 % it is larger.
                slot_ev_planned_load_kwh[i] = ev_slot.ac_load_kwh
                break

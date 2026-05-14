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

    Args:
        inp: EV planner inputs.
        slots_start: List of slot start datetimes (same length as other lists).
        slots_end: List of slot end datetimes.
        slot_solar_surplus_kwh: Solar surplus available per slot (kWh, ≥ 0).
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
        allocated = min(max_charge, remaining_energy)
        if allocated < 1e-9:
            continue

        # ``allocated`` is battery-side kWh delivered to the EV.
        # AC load = battery-side / charger_efficiency (what grid/PV must supply).
        eff = max(inp.charger_efficiency_pct, 1.0) / 100.0
        ac_load = allocated / eff

        surplus = slot_solar_surplus_kwh[i]
        # solar_used / import_needed are expressed as battery-side kWh for
        # the EV plan display; ac_load_kwh is the grid/PV draw used for net
        # consumption and SoC simulation.
        solar_used = min(allocated, surplus)
        import_needed = max(allocated - solar_used, 0.0)
        cost = (ac_load - min(ac_load, surplus / eff * eff)) * slot_import_price[i]
        # Simpler: cost = grid AC draw × price = import_needed / eff × price
        cost = round((import_needed / eff) * slot_import_price[i], 4)

        ev_slot = EVChargingSlot(
            start=s_start,
            end=s_end,
            estimated_charged_kwh=round(allocated, 3),
            ac_load_kwh=round(ac_load, 3),
            solar_surplus_kwh=round(solar_used, 3),
            import_needed_kwh=round(import_needed, 3),
            import_price=slot_import_price[i],
            estimated_cost=cost,
        )
        selected.append(ev_slot)
        remaining_energy -= allocated

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
    for ev_slot in ev_plan.charging_slots:
        key = ev_slot.start.isoformat()
        for i, s in enumerate(slot_starts):
            if s.isoformat() == key:
                # Accumulate AC-side load (grid/PV draw), not battery-side
                # delivered energy.  With charger_efficiency < 100 %, the AC
                # load is larger than the kWh arriving in the EV battery.
                # The += operator ensures multiple EVs sharing the same slot
                # are summed rather than one overwriting the other.
                slot_ev_planned_load_kwh[i] += ev_slot.ac_load_kwh
                break

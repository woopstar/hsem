"""Full battery SoC simulation for the HSEM planner.

This module implements a forward-pass SoC simulator that:

- Respects the minimum SoC floor (``end_of_discharge_soc_pct``).
- Respects the maximum SoC ceiling (``battery_max_soc_pct``).
- Clamps charge energy to the remaining capacity up to max SoC and to the
  per-slot charge power limit.
- Clamps discharge energy to the available capacity above min SoC and to
  the per-slot discharge power limit (when set).
- Tracks per-slot PV, load, charge, discharge, grid import and export.

Design principles
-----------------
- Pure function — no I/O, no Home Assistant imports.
- Mutates the ``PlannedSlot`` list in place (same pattern as other
  ``populate_*`` helpers in :mod:`slot_population`).
- ``batteries_charged`` on each slot is the *pre-simulation* scheduled
  charge energy (set by :func:`~charge_scheduler.apply_charge_schedules`).
  The simulation may reduce it if the battery would exceed ``max_soc``.
- ``batteries_discharged``, ``grid_import_kwh``, and ``grid_export_kwh``
  are written by this function.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.planner_outputs import PlannedSlot


def simulate_soc(
    slots: list[PlannedSlot],
    now: datetime,
    current_kwh: float,
    usable_kwh: float,
    max_capacity_kwh: float,
    max_charge_per_slot: float,
    max_discharge_per_slot: float | None,
    rated_kwh: float = 0.0,
    end_of_discharge_soc_pct: float = 0.0,
) -> None:
    """Forward-simulate battery SoC through all planning slots.

    Writes the following fields on every future/current slot:

    - ``estimated_battery_capacity`` — remaining usable kWh *above* the
      end-of-discharge floor, at the *end* of the slot.
    - ``estimated_battery_soc`` — absolute SoC percentage (0-100) relative
      to the *rated* capacity.  When ``rated_kwh`` is not provided the value
      falls back to the relative usable-range percentage.
    - ``batteries_charged`` — may be *reduced* from its pre-set value if the
      battery would exceed ``max_capacity_kwh``.
    - ``batteries_discharged`` — energy drawn from the battery, clamped to
      the discharge power limit and available capacity.
    - ``grid_import_kwh`` — energy imported from the grid this slot.
    - ``grid_export_kwh`` — energy exported to the grid this slot.

    The simulation is DST-safe: all ``slot.start`` / ``slot.end`` are
    normalised to ``now.tzinfo`` before comparison.

    Args:
        slots: Mutable list of planned slots in chronological order.
        now: Timezone-aware current datetime.
        current_kwh: Energy currently stored *above* the discharge floor (kWh).
        usable_kwh: Maximum usable energy above the discharge floor (kWh).
            This is the *range* of the battery (max_soc − min_soc), not
            the absolute nameplate kWh.
        max_capacity_kwh: Absolute ceiling imposed by ``battery_max_soc_pct``
            expressed in usable kWh.  Must be ≤ ``usable_kwh``.
        max_charge_per_slot: Maximum energy (kWh) that can be *stored* per slot
            after conversion losses.
        max_discharge_per_slot: Maximum energy (kWh) that can be *drawn* from
            the battery per slot.  ``None`` means unlimited (inverter default).
        rated_kwh: Nameplate capacity in kWh.  When > 0 the SoC percentage is
            reported as an absolute value (0-100) relative to the rated
            capacity, consistent with what the inverter reports.
        end_of_discharge_soc_pct: End-of-discharge floor as a percentage.
            Used together with ``rated_kwh`` to convert kWh-above-floor to
            absolute SoC.
    """
    cap = current_kwh  # working state — kWh above discharge floor

    for slot in slots:
        slot_start = slot.start.astimezone(now.tzinfo)
        slot_end = slot.end.astimezone(now.tzinfo)

        # Past slots: zero out SoC fields and move on.
        if slot_end <= now:
            slot.estimated_battery_capacity = 0.0
            slot.estimated_battery_soc = 0.0
            slot.batteries_discharged = 0.0
            slot.grid_import_kwh = 0.0
            slot.grid_export_kwh = 0.0
            continue

        # For the current in-progress slot use current_kwh as the starting
        # state; for all future slots chain from the previous slot's end.
        if slot_start <= now < slot_end:
            cap = current_kwh

        pv = slot.solcast_pv_estimate  # kWh produced by PV this slot
        load = slot.avg_house_consumption  # kWh consumed by house this slot

        # --- Enforce charge ceiling on pre-scheduled charge ---
        # The charge scheduler may have set batteries_charged without knowing
        # the current SoC at this point in the simulation.  Reduce if the
        # battery would exceed max_capacity_kwh or the per-slot power limit.
        headroom = max(max_capacity_kwh - cap, 0.0)
        scheduled_charge = min(slot.batteries_charged, headroom, max_charge_per_slot)
        scheduled_charge = max(scheduled_charge, 0.0)
        slot.batteries_charged = round(scheduled_charge, 3)

        # --- Net demand after PV covers house load ---
        net_demand = load - pv  # positive = demand > PV; negative = PV surplus

        if net_demand > 0:
            # House demand exceeds PV.  Battery discharges to cover the gap;
            # anything the battery cannot supply is imported from the grid.
            # Scheduled charge (from grid or pre-planned solar) is in addition
            # to the discharge.
            max_discharge = cap  # can't discharge beyond available capacity
            if max_discharge_per_slot is not None:
                max_discharge = min(max_discharge, max_discharge_per_slot)
            discharge = min(net_demand, max_discharge)
            discharge = max(discharge, 0.0)
            grid_import = max(net_demand - discharge + scheduled_charge, 0.0)
            grid_export = 0.0
        else:
            # PV surplus beyond house load.
            # The pre-scheduled charge is already accounted for in batteries_charged.
            # Any additional PV surplus that cannot be stored is exported.
            discharge = 0.0
            pv_surplus = abs(net_demand)  # kWh of PV beyond house load
            # Additional PV that can still be absorbed by the battery
            # (beyond what the scheduler already planned):
            remaining_headroom = max(headroom - scheduled_charge, 0.0)
            remaining_power = max(max_charge_per_slot - scheduled_charge, 0.0)
            additional_pv_charge = min(pv_surplus, remaining_headroom, remaining_power)
            additional_pv_charge = max(additional_pv_charge, 0.0)
            # Total energy entering the battery this slot:
            total_charge = scheduled_charge + additional_pv_charge
            grid_import = 0.0
            grid_export = max(pv_surplus - additional_pv_charge, 0.0)
            # Override scheduled_charge for state update (include PV capture).
            # batteries_charged keeps the originally-scheduled value (no change).
            scheduled_charge = total_charge

        # --- Update battery state ---
        cap = cap + scheduled_charge - discharge
        # Enforce hard bounds (floating-point safety).
        cap = min(max(cap, 0.0), usable_kwh)

        # --- Write slot fields ---
        slot.estimated_battery_capacity = round(cap, 3)
        if rated_kwh > 1e-9:
            # Absolute SoC: convert kWh-above-floor back to percentage of rated
            # capacity so it matches what the physical inverter reports.
            absolute_kwh = cap + rated_kwh * end_of_discharge_soc_pct / 100
            slot.estimated_battery_soc = round(absolute_kwh / rated_kwh * 100, 2)
        elif usable_kwh > 1e-9:
            slot.estimated_battery_soc = round(cap / usable_kwh * 100, 2)
        else:
            slot.estimated_battery_soc = 0.0
        slot.batteries_discharged = round(discharge, 3)
        slot.grid_import_kwh = round(grid_import, 3)
        slot.grid_export_kwh = round(grid_export, 3)

"""Full battery SoC simulation for the HSEM planner.

This module implements a forward-pass SoC simulator that:

- Respects the minimum SoC floor (``end_of_discharge_soc_pct``).
- Respects the maximum SoC ceiling (``battery_max_soc_pct``).
- Clamps charge energy to the remaining capacity up to max SoC and to the
  per-slot charge power limit.
- Clamps discharge energy to the available capacity above min SoC and to
  the per-slot discharge power limit (when set).
- Tracks per-slot PV, load, charge, discharge, grid import and export.
- Applies separate charge and discharge efficiency so that:
    battery_stored = input_energy × charge_efficiency
    house_delivered = battery_removed × discharge_efficiency

Design principles
-----------------
- Pure function — no I/O, no Home Assistant imports.
- Mutates the ``PlannedSlot`` list in place (same pattern as other
  ``populate_*`` helpers in :mod:`slot_population`).
- ``batteries_charged`` on each slot is the *pre-simulation* scheduled
  charge energy expressed as energy *entering the battery* (post charge-loss).
  The simulation may reduce it if the battery would exceed ``max_soc``.
- ``batteries_discharged`` is the energy *removed from the battery* (pre
  discharge-loss).  The house actually receives
  ``batteries_discharged × discharge_efficiency``.
- ``grid_import_kwh`` and ``grid_export_kwh`` are written by this function.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner


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
    charge_efficiency_pct: float = 100.0,
    discharge_efficiency_pct: float = 100.0,
) -> None:
    """Forward-simulate battery SoC through all planning slots.

    Writes the following fields on every future/current slot:

    - ``estimated_battery_capacity`` — remaining usable kWh *above* the
      end-of-discharge floor, at the *end* of the slot.
    - ``estimated_battery_soc`` — absolute SoC percentage (0-100) relative
      to the *rated* capacity.  When ``rated_kwh`` is not provided the value
      falls back to the relative usable-range percentage.
    - ``batteries_charged`` — may be *reduced* from its pre-set value if the
      battery would exceed ``max_capacity_kwh``.  Represents energy *entering
      the battery* (post charge-side loss).
    - ``batteries_discharged`` — energy *removed from the battery* (pre
      discharge-side loss), clamped to the discharge power limit and available
      capacity.  The house actually receives
      ``batteries_discharged × (discharge_efficiency_pct / 100)``.
    - ``grid_import_kwh`` — energy imported from the grid this slot.
    - ``grid_export_kwh`` — energy exported to the grid this slot.

    Efficiency model
    ----------------
    - **Charge side**: ``charge_efficiency_pct`` (0-100) determines how much
      of the commanded input energy is actually stored.  The battery SoC
      increases by ``charge_commanded × (charge_efficiency_pct / 100)``.  Grid
      import for charging is ``charge_stored / (charge_efficiency_pct / 100)``.
    - **Discharge side**: ``discharge_efficiency_pct`` (0-100) determines how
      much of the removed battery energy reaches the house.  The battery SoC
      decreases by the full ``batteries_discharged`` value; the house receives
      ``batteries_discharged × (discharge_efficiency_pct / 100)``.

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
            (battery-side, already accounts for charge-side loss in the caller).
        max_discharge_per_slot: Maximum energy (kWh) that can be *drawn* from
            the battery per slot.  ``None`` means unlimited (inverter default).
        rated_kwh: Nameplate capacity in kWh.  When > 0 the SoC percentage is
            reported as an absolute value (0-100) relative to the rated
            capacity, consistent with what the inverter reports.
        end_of_discharge_soc_pct: End-of-discharge floor as a percentage.
            Used together with ``rated_kwh`` to convert kWh-above-floor to
            absolute SoC.
        charge_efficiency_pct: Charge-side efficiency as a percentage (0-100).
            Energy stored = charge_commanded × (charge_efficiency_pct / 100).
            Defaults to 100 % (no charge-side loss) for backward compatibility.
        discharge_efficiency_pct: Discharge-side efficiency as a percentage
            (0-100).  Energy to house = battery_removed × (discharge_efficiency_pct / 100).
            Defaults to 100 % (no discharge-side loss) for backward compatibility.
    """
    # Clamp efficiencies to a valid range to avoid division by zero or nonsense.
    charge_eff = max(min(charge_efficiency_pct, 100.0), 1.0) / 100.0
    discharge_eff = max(min(discharge_efficiency_pct, 100.0), 1.0) / 100.0
    cap = current_kwh  # working state — kWh above discharge floor

    log_planner(
        "debug",
        "[soc_sim] START  current=%.3f kWh  usable=%.3f kWh  "
        "max_cap=%.3f kWh  charge_eff=%.2f  discharge_eff=%.2f  "
        "max_charge/slot=%.3f  max_discharge/slot=%s",
        current_kwh,
        usable_kwh,
        max_capacity_kwh,
        charge_eff,
        discharge_eff,
        max_charge_per_slot,
        f"{max_discharge_per_slot:.3f}" if max_discharge_per_slot is not None else "∞",
    )

    for slot in slots:
        slot_start = as_tz(slot.start, now.tzinfo)
        slot_end = as_tz(slot.end, now.tzinfo)

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

        # ev_planned_load_kwh  — extra EV AC load NOT yet in avg_house_consumption
        #                        (base_load_includes_ev=False case)
        # ev_accounted_load_kwh — EV AC load already baked into avg_house_consumption
        #                        (base_load_includes_ev=True case)
        #
        # The EV charger and the house loads share the same AC bus with the
        # battery inverter.  When the battery discharges, the AC power feeds
        # EVERYTHING on the bus — you cannot selectively skip the EV charger.
        # Therefore, when ev_load > 0, battery discharge is suppressed and
        # ALL demand is covered from the grid to avoid DC→AC→DC conversion
        # losses through the EV charger.
        #
        # For base_load_includes_ev=False:
        #   house_load = avg_house_consumption (pure house, no EV)
        #   ev_load    = ev_planned_load_kwh   (extra EV draw, goes to grid)
        #
        # For base_load_includes_ev=True:
        #   avg_house_consumption includes the EV AC draw.
        #   We must strip it out so the battery's net demand is pure house only.
        #   house_load = avg_house_consumption - ev_accounted_load_kwh
        #   ev_load    = ev_planned_load_kwh (0.0) + ev_accounted_load_kwh
        ev_accounted = slot.ev_accounted_load_kwh  # already in avg_house_consumption
        ev_injected = (
            slot.ev_planned_load_kwh
        )  # extra, not yet in avg_house_consumption
        ev_load = ev_injected + ev_accounted  # total AC EV draw → grid/PV only
        house_load = slot.avg_house_consumption - ev_accounted  # pure house load

        # --- Enforce charge ceiling on pre-scheduled charge ---
        # The charge scheduler may have set batteries_charged without knowing
        # the current SoC at this point in the simulation.  Reduce if the
        # battery would exceed max_capacity_kwh or the per-slot power limit.
        headroom = max(max_capacity_kwh - cap, 0.0)
        scheduled_charge = min(slot.batteries_charged, headroom, max_charge_per_slot)
        scheduled_charge = max(scheduled_charge, 0.0)
        slot.batteries_charged = round(scheduled_charge, 3)

        # --- Net demand on the shared AC bus ---
        # The battery, house loads, and EV charger all share one AC bus.
        # When the battery discharges, the AC power flows to everything on
        # the bus — you cannot selectively skip the EV.  Therefore when the
        # EV is charging we MUST NOT discharge the battery; doing so would
        # create DC→AC→DC conversion losses through the EV charger for no
        # benefit.
        #
        # PV covers house load first.  The remainder (positive = deficit,
        # negative = surplus) determines whether the battery charges or
        # discharges.
        net_demand = (
            house_load - pv
        )  # positive = house needs energy; negative = surplus

        if net_demand > 0:
            # House demand exceeds PV on the shared AC bus.
            #
            # When the EV is charging, the battery MUST NOT discharge.
            # The battery, house loads, and EV charger all share one AC bus —
            # you cannot selectively route discharge power to the house while
            # excluding the EV.  Any DC→AC discharge would feed both, creating
            # needless DC→AC→DC conversion losses through the EV charger.
            # Instead, cover everything (house + EV + scheduled charge) from
            # the grid when EV is active.
            #
            # Discharge efficiency: to deliver `net_demand` kWh to the house
            # the battery must release `net_demand / discharge_eff` kWh.
            # We cap to available capacity and per-slot limit then compute
            # what the house actually receives from that draw.
            if ev_load > 1e-9:
                # EV is charging — no battery discharge.  Everything from grid.
                discharge = 0.0
                house_grid_import = net_demand
                grid_import = (
                    house_grid_import + scheduled_charge / charge_eff + ev_load
                )
                grid_export = 0.0
            else:
                max_discharge_cap = cap  # can't discharge beyond available capacity
                if max_discharge_per_slot is not None:
                    max_discharge_cap = min(max_discharge_cap, max_discharge_per_slot)
                # Battery energy to remove: enough to cover demand via discharge_eff
                discharge_needed = net_demand / discharge_eff
                discharge = min(discharge_needed, max_discharge_cap)
                discharge = max(discharge, 0.0)
                # Energy actually delivered to the house from battery (post loss)
                house_from_battery = discharge * discharge_eff
                # Remaining house demand from grid + grid energy for scheduled charge.
                # EV load is added on top: the EV draws from grid/PV directly,
                # independently of what the battery does.
                house_grid_import = max(net_demand - house_from_battery, 0.0)
                grid_import = (
                    house_grid_import
                    + scheduled_charge / charge_eff
                    + ev_load  # EV draws from grid (PV already consumed by house above)
                )
                grid_export = 0.0
        else:
            # PV surplus (or balanced: net_demand == 0) beyond house load.
            # The pre-scheduled charge (batteries_charged) is expressed as battery-side
            # stored kWh.  It can be sourced from PV or from the grid.
            # We attribute as much of the scheduled charge as possible to PV surplus;
            # any remainder must be imported from the grid.
            discharge = 0.0
            pv_surplus = abs(net_demand)  # kWh of PV beyond house load (≥ 0)

            # The EV charger draws from PV surplus first (free energy before
            # exporting to grid or charging the house battery).  Whatever PV
            # remains after serving the EV feeds the battery or is exported.
            pv_for_ev = min(ev_load, pv_surplus)
            ev_grid_import = max(ev_load - pv_for_ev, 0.0)  # EV residual from grid
            pv_surplus_after_ev = max(pv_surplus - pv_for_ev, 0.0)

            # How much PV input is needed to store scheduled_charge in the battery?
            scheduled_charge_pv_input = scheduled_charge / charge_eff
            # How much of that PV input is available (after EV consumption)?
            pv_for_scheduled = min(scheduled_charge_pv_input, pv_surplus_after_ev)
            # Battery-side energy sourced from PV for the scheduled charge:
            pv_battery_charge = pv_for_scheduled * charge_eff
            # Any shortfall in the scheduled charge must come from the grid:
            grid_charge_battery = scheduled_charge - pv_battery_charge  # battery-side
            grid_charge_input = (
                grid_charge_battery / charge_eff if grid_charge_battery > 1e-9 else 0.0
            )

            # Remaining PV surplus after serving EV and the scheduled charge:
            pv_remaining = max(pv_surplus_after_ev - pv_for_scheduled, 0.0)

            # Additional PV that can still be absorbed by the battery
            # (beyond what the scheduler already planned):
            remaining_headroom = max(headroom - scheduled_charge, 0.0)
            remaining_power = max(max_charge_per_slot - scheduled_charge, 0.0)
            max_additional_pv_input = min(
                pv_remaining,
                remaining_headroom / charge_eff,
                remaining_power / charge_eff,
            )
            max_additional_pv_input = max(max_additional_pv_input, 0.0)
            additional_pv_charge = max_additional_pv_input * charge_eff
            additional_pv_charge = max(additional_pv_charge, 0.0)

            # Total battery-side energy stored this slot:
            total_charge = scheduled_charge + additional_pv_charge

            # Grid import: grid-sourced battery charge + EV residual not covered by PV
            grid_import = grid_charge_input + ev_grid_import
            # PV exported = remaining PV not absorbed by battery or EV
            grid_export = max(pv_remaining - max_additional_pv_input, 0.0)

            # Override scheduled_charge for state update (include PV capture).
            # batteries_charged keeps the originally-scheduled value (no change).
            scheduled_charge = total_charge

        # --- Update battery state ---
        # The battery stores/releases actual kWh (post charge-eff, pre discharge-eff).
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

        log_planner(
            "debug",
            "[soc_sim] %s→%s  rec=%-28s  "
            "pv=%.3f  house=%.3f  ev_inj=%.3f  ev_acc=%.3f  net_demand=%+.3f  "
            "sched_chg=%.3f  discharge=%.3f  "
            "grid_in=%.3f  grid_out=%.3f  "
            "cap_after=%.3f  soc=%.1f%%",
            slot.start.strftime("%d %H:%M"),
            slot.end.strftime("%H:%M"),
            slot.recommendation if slot.recommendation is not None else "(none)",
            pv,
            house_load,
            ev_injected,
            ev_accounted,
            net_demand,
            scheduled_charge,
            discharge,
            grid_import,
            grid_export,
            cap,
            slot.estimated_battery_soc,
        )

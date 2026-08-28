"""Write MILP decision variables back into output slots.

Extracted from ``solve_milp`` so the orchestrator remains under 30 KB.
"""

from __future__ import annotations

import copy
import math
from datetime import datetime

import numpy as np

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.cost_helpers import slot_grid_cash_flow_cost
from custom_components.hsem.planner.milp._ev_power_writeout import (
    _write_ev_power_fields_to_slots,
)
from custom_components.hsem.utils.units import ev_dc_to_ac_kwh, slot_duration_hours

#: Minimum kWh a slot's EV allocation must clear to be considered "occupied"
#: rather than solver noise, mirroring ``_write_milp_results_to_slots``'s
#: ``_min_action_kwh`` default.
_MIN_ACTION_KWH = 1e-4


def _write_milp_results_to_slots(
    slots: list[PlannedSlot],
    future_idx: list[int],
    now: datetime,
    ec_sol: np.ndarray,  # type: ignore[name-defined]
    ed_sol: np.ndarray,  # type: ignore[name-defined]
    result_x: np.ndarray,  # type: ignore[name-defined]
    m: int,
    battery_export_off: int,
    active_evs: list[EVConfig],
    ev_var_offsets: list[int],
    pv_avail: np.ndarray,  # type: ignore[name-defined]
    base_load: np.ndarray,  # type: ignore[name-defined]
    charge_eff: float,
    discharge_eff: float,
    p_exp: np.ndarray,  # type: ignore[name-defined]
    min_export_price: float,
    _has_session_demand: bool,
    session_slots_set: set[int],
    current_kwh: float,
    usable_kwh: float,
    curt_sol_full: np.ndarray,  # type: ignore[name-defined]
    *,
    ev_writeback_diagnostics: dict[str, dict[str, object]] | None = None,
    _min_action_kwh: float = 1e-4,
) -> list[PlannedSlot]:
    """Write MILP solution into a deep-copied slot list.

    Args:
        slots: Original slot list (will be deep-copied).
        future_idx: Indices of future (LP-variable) slots.
        now: Current datetime for time-aware power calculation.
        ec_sol: Solved charge energy per LP slot (kWh).
        ed_sol: Solved discharge energy per LP slot (kWh).
        result_x: Full LP solution vector.
        m: Number of active LP slots (``len(future_idx)``).
        battery_export_off: Offset of battery-side export variables.
        active_evs: List of active EV configs for EV write-out.
        ev_var_offsets: Start offset of each EV's ``ev_c[t]`` block.
        pv_avail: Per-slot PV surplus (positive kWh).
        base_load: Per-slot house demand after PV (positive kWh).
        charge_eff: Charge-side efficiency fraction (0-1).
        discharge_eff: Discharge-side efficiency fraction (0-1).
        p_exp: Per-slot export price array.
        min_export_price: Minimum export price threshold.
        _has_session_demand: Whether any EV has active session demand.
        session_slots_set: Aggregate (any-EV) session slot indices, used
            where the check is genuinely site-wide (battery grid-charge
            guard) rather than per-charger.
        current_kwh: Battery energy at horizon start (above floor, kWh).
        usable_kwh: Maximum usable energy (kWh).
        curt_sol_full: Solved curtailment per LP slot (kWh).
        _min_action_kwh: Minimum kWh threshold for action slots.
        Recommendations: The canonical Recommendations enum.

    Returns:
        A list of ``PlannedSlot`` copies with MILP-derived fields populated.
    """

    from custom_components.hsem.utils.recommendations import Recommendations

    out_slots: list[PlannedSlot] = [copy.copy(s) for s in slots]
    executable_x = result_x.copy()

    full_slot_hours = (
        slot_duration_hours(slots[future_idx[0]].start, slots[future_idx[0]].end)
        if future_idx
        else 0.0
    )
    # Managed EV charge is already a validated whole-amp MILP decision
    # (issue #797): the solver-native amp lattice (planner/milp/
    # _ev_amp_lattice.py) links ev_c[t] to an executable amp command by
    # equality, so it is never moved or quantised after solving here.
    # Concentration/redistribution/quantization (below) remain available to
    # direct compatibility callers that supply a legacy continuous
    # allocation, but production write-out never calls them.
    for ev_idx, ev in enumerate(active_evs):
        if ev.fixed_session_only:
            continue
        ev_off = ev_var_offsets[ev_idx]
        values = executable_x[ev_off : ev_off + m]
        if ev_writeback_diagnostics is not None:
            delivered_by_deadline = float(np.sum(values))
            if ev.deadline_slot is not None:
                delivered_by_deadline = float(
                    np.sum(values[: max(0, min(ev.deadline_slot, m - 1)) + 1])
                )
            deadline_penalty = max(
                ev.effective_deadline_target_kwh
                - ev.initial_soc_kwh
                - delivered_by_deadline,
                0.0,
            )
            ev_writeback_diagnostics[f"ev{ev_idx}"] = {
                "total_dc_kwh": round(float(np.sum(values)), 4),
                "deadline_penalty_kwh": round(deadline_penalty, 4),
                "deadline_met": deadline_penalty < 1e-6,
                "unplaceable_dc_kwh": 0.0,
                "reportioned_slots": 0,
            }

    if ev_writeback_diagnostics is not None:
        for ev_idx, ev in enumerate(active_evs):
            key = f"ev{ev_idx}"
            if key in ev_writeback_diagnostics:
                continue
            ev_off = ev_var_offsets[ev_idx]
            values = executable_x[ev_off : ev_off + m]
            delivered_by_deadline = float(np.sum(values))
            if ev.deadline_slot is not None:
                delivered_by_deadline = float(
                    np.sum(values[: max(0, min(ev.deadline_slot, m - 1)) + 1])
                )
            deadline_penalty = max(
                ev.effective_deadline_target_kwh
                - ev.initial_soc_kwh
                - delivered_by_deadline,
                0.0,
            )
            ev_writeback_diagnostics[key] = {
                "total_dc_kwh": round(float(np.sum(values)), 4),
                "deadline_penalty_kwh": round(deadline_penalty, 4),
                "deadline_met": deadline_penalty < 1e-6,
                "unplaceable_dc_kwh": 0.0,
                "reportioned_slots": 0,
            }

    # Reset charge/discharge, energy-flow, and EV fields on all future slots;
    # past slots keep TimePassed.
    for i in future_idx:
        out_slots[i].recommendation = None
        out_slots[i].batteries_charged_kwh = 0.0
        out_slots[i].batteries_discharged_kwh = 0.0
        out_slots[i].grid_import_kwh = 0.0
        out_slots[i].grid_export_kwh = 0.0
        out_slots[i].primary_battery_export_kwh = 0.0
        out_slots[i].pv_export_kwh = 0.0
        out_slots[i].ev_planned_load_kwh = 0.0
        out_slots[i].ev_accounted_load_kwh = 0.0
        out_slots[i].ev_total_planned_load_kwh = 0.0
        out_slots[i].ev_charger_calculated_power = 0.0
        out_slots[i].ev_second_charger_calculated_power = 0.0

    # Write MILP-derived charge/discharge actions
    # Pre-compute which slots have EV charging — when both battery and
    # EV charge in the same slot, the battery must use BatteriesChargeGrid
    # (not BatteriesChargeSolar) because the EV will consume the solar
    # surplus, leaving nothing for the battery.
    ev_charging_slots: set[int] = set()
    if active_evs:
        for ev_idx in range(len(active_evs)):
            ev_off = ev_var_offsets[ev_idx]
            ev_c_sol = executable_x[ev_off : ev_off + m]
            for lp_t in range(m):
                if float(ev_c_sol[lp_t]) >= _min_action_kwh:
                    ev_charging_slots.add(lp_t)

    # Pre-compute per-slot total EV AC load from the LP solution.
    # Needed for deriving grid import/export from the energy balance
    # equation when mutex resolution alters ec/ed (issue #659):
    #   gi + pv + ed·η_dis = base_load + ec/η_chg + ge + curt + Σ ev_c/eff
    ev_ac_load_by_slot: dict[int, float] = {}
    if active_evs:
        for ev_idx, ev in enumerate(active_evs):
            ev_off = ev_var_offsets[ev_idx]
            ev_c_sol = executable_x[ev_off : ev_off + m]
            for lp_t in range(m):
                ev_dc = float(ev_c_sol[lp_t])
                if ev_dc >= _min_action_kwh:
                    ev_ac_load_by_slot[lp_t] = ev_ac_load_by_slot.get(
                        lp_t, 0.0
                    ) + ev_dc_to_ac_kwh(ev_dc, ev.charger_efficiency)

    # ------------------------------------------------------------------
    # Single merged energy-flow write-out pass (issue #659).
    #
    # Resolves degenerate LP vertices (simultaneous charge+discharge),
    # sets recommendation, and populates ALL per-slot energy-flow fields
    # (charge, discharge, grid import, grid export) consistently from
    # the SAME resolved ec/ed decision.  Grid import/export are derived
    # from the slot's energy balance equation rather than read from the
    # raw LP arrays, so they remain correct even when ec/ed are adjusted
    # by the mutex resolution.
    #
    # The SoC simulation (simulate_soc) must use these verbatim when
    # milp_prepopulated=True — never re-derive a different (greedy)
    # value from the recommendation label and net_demand.
    # ------------------------------------------------------------------
    running_soc = current_kwh
    for lp_t, slot_i in enumerate(future_idx):
        ec_kwh = float(ec_sol[lp_t])
        ed_kwh = float(ed_sol[lp_t])
        battery_export_dc = float(result_x[battery_export_off + lp_t])

        if ec_kwh > _min_action_kwh and ed_kwh > _min_action_kwh:
            # Degenerate LP vertex (simultaneous charge+discharge).
            # The LP is indifferent among cost-equivalent ec/ed
            # combinations.  Check actual SoC headroom at this point
            # in the resolved trajectory to distinguish a genuine
            # economic signal from solver noise near a SoC bound
            # (issue #662).
            #
            # net_charge_profit = p_imp·(η_dis − 1/η_chg) − 2·cycle_cost
            # is structurally always ≤ 0 for realistic efficiencies
            # and costs, so it cannot discriminate.  The LP's
            # s_max_pen/s_min_pen variables are a per-slot
            # hard-bound-violation signal, not a horizon-wide
            # degeneracy signal — they miss degenerate vertices
            # where SoC is merely near (not at) a bound.
            # Use actual resolved SoC headroom instead.
            net = ec_kwh - ed_kwh
            if net > _min_action_kwh:
                # Net charge candidate: clamp to remaining ceiling
                # headroom.  usable_kwh is the energy available
                # between end_of_discharge_soc and max_soc;
                # running_soc is measured from the same floor.
                headroom = usable_kwh - running_soc
                if headroom <= _min_action_kwh:
                    ec_kwh = 0.0
                    ed_kwh = 0.0
                else:
                    chosen = min(net, headroom)
                    ec_kwh = chosen
                    ed_kwh = 0.0
            elif net < -_min_action_kwh:
                # Net discharge candidate: clamp to remaining floor
                # headroom.  The discharge floor is already baked
                # into current_kwh/usable_kwh (see usable_capacity),
                # so 0.0 is the floor reference for running_soc.
                floor_headroom = running_soc
                if floor_headroom <= _min_action_kwh:
                    ec_kwh = 0.0
                    ed_kwh = 0.0
                else:
                    chosen = min(-net, floor_headroom)
                    ec_kwh = 0.0
                    ed_kwh = chosen
            else:
                # Net ~0 (both within _min_action_kwh of each other):
                # pure wash vertex — zero both.
                ec_kwh = 0.0
                ed_kwh = 0.0

        battery_export_dc = min(battery_export_dc, max(ed_kwh, 0.0))

        if ec_kwh > _min_action_kwh:
            # Use BatteriesChargeSolar when PV surplus is available,
            # BatteriesChargeGrid otherwise.  When EV is also charging
            # in this slot, always use BatteriesChargeGrid — the EV
            # will consume the solar surplus, so the battery must draw
            # from grid to actually receive the energy the MILP allocated.
            if pv_avail[lp_t] > _min_action_kwh and lp_t not in ev_charging_slots:
                out_slots[
                    slot_i
                ].recommendation = Recommendations.BatteriesChargeSolar.value
            else:
                # Session-slot guard: do NOT assign BatteriesChargeGrid
                # during session EV demand slots (issue #615).  The LP
                # constraints already prevent ec[t] > 0 here, but this
                # guard protects against any edge case.
                is_session_slot = _has_session_demand and lp_t in session_slots_set
                if is_session_slot and pv_avail[lp_t] > _min_action_kwh:
                    out_slots[
                        slot_i
                    ].recommendation = Recommendations.BatteriesChargeSolar.value
                elif not is_session_slot:
                    out_slots[
                        slot_i
                    ].recommendation = Recommendations.BatteriesChargeGrid.value
        elif ed_kwh > _min_action_kwh:
            # If the LP is exporting (ge > 0) in this slot, use
            # ForceBatteriesDischarge to signal that the battery should
            # cover house load AND export excess to grid.
            if battery_export_dc > _min_action_kwh and p_exp[lp_t] >= min_export_price:
                out_slots[
                    slot_i
                ].recommendation = Recommendations.ForceBatteriesDischarge.value
            else:
                out_slots[
                    slot_i
                ].recommendation = Recommendations.BatteriesDischargeMode.value

        # Write resolved charge/discharge kWh fields consistently.
        resolved_charge = round(max(ec_kwh, 0.0), 3)
        resolved_discharge = round(max(ed_kwh, 0.0), 3)
        if resolved_charge > 0.0:
            headroom = max(usable_kwh - running_soc, 0.0)
            resolved_charge = min(
                resolved_charge, math.floor(headroom * 1000.0) / 1000.0
            )
        if resolved_discharge > 0.0:
            headroom = max(running_soc, 0.0)
            resolved_discharge = min(
                resolved_discharge,
                math.floor(headroom * 1000.0) / 1000.0,
            )
        out_slots[slot_i].batteries_charged_kwh = resolved_charge
        out_slots[slot_i].batteries_discharged_kwh = resolved_discharge

        # Derive grid import/export from the slot's energy balance using
        # the SAME resolved (rounded) charge/discharge values stored in
        # the slot fields.  This guarantees the equality
        #   gi + pv + ed·η_dis = house_load + ec/η_chg + ge
        # holds exactly at 3-decimal precision.
        #
        # LP energy balance:  gi + pv + ed·η_dis
        #     = base_load + ec/η_chg + ge + curt + Σ ev_c/eff
        # ⇒ gi − ge = base_load + ec/η_chg − ed·η_dis + curt + Σ ev_c/eff − pv
        curt_kwh = float(curt_sol_full[lp_t])
        ev_ac_kwh = ev_ac_load_by_slot.get(lp_t, 0.0)
        net_flow = (
            base_load[lp_t]
            + resolved_charge / charge_eff
            - resolved_discharge * discharge_eff
            + curt_kwh
            + ev_ac_kwh
            - pv_avail[lp_t]
        )
        if net_flow > 0:
            out_slots[slot_i].grid_import_kwh = round(net_flow, 3)
            out_slots[slot_i].grid_export_kwh = 0.0
        else:
            out_slots[slot_i].grid_import_kwh = 0.0
            out_slots[slot_i].grid_export_kwh = round(-net_flow, 3)

        grid_export = out_slots[slot_i].grid_export_kwh
        battery_export_ac = round(
            min(max(battery_export_dc * discharge_eff, 0.0), grid_export),
            3,
        )
        out_slots[slot_i].primary_battery_export_kwh = battery_export_ac
        out_slots[slot_i].pv_export_kwh = round(
            max(grid_export - battery_export_ac, 0.0), 3
        )

        # Advance resolved SoC for headroom-based degenerate-vertex
        # resolution in subsequent slots (issue #662).
        running_soc += resolved_charge - resolved_discharge

    # ------------------------------------------------------------------
    # Write MILP-derived EV charging decisions to output slots
    # ------------------------------------------------------------------
    if active_evs:
        # Pre-compute full slot hours for power calculation (same for all slots
        # when interval is uniform).
        first_future_slot = out_slots[future_idx[0]]
        full_slot_hours = slot_duration_hours(
            first_future_slot.start, first_future_slot.end
        )
        _write_ev_power_fields_to_slots(
            out_slots,
            future_idx,
            now,
            executable_x,
            m,
            full_slot_hours,
            active_evs,
            ev_var_offsets,
            _min_action_kwh,
        )

    for i in future_idx:
        out_slots[i].estimated_cost_currency = round(
            slot_grid_cash_flow_cost(
                out_slots[i],
                export_min_price=min_export_price,
            ),
            4,
        )

    return out_slots

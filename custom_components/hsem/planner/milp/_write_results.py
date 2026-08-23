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
from custom_components.hsem.planner.milp._ev_quantize import (
    _quantize_ev_allocation_to_whole_amps,
    _redistribute_below_minimum_power,
)
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.phase_power import (
    charger_current_to_power_w,
    charger_max_power_to_current_a,
    charger_min_power_to_current_a,
    charger_power_to_current_a,
    ev_phase_share_for_slot,
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
    gi_off: int | None = None,
    grid_import_cap_per_slot_kwh: np.ndarray | None = None,  # type: ignore[name-defined]
    max_phase_import_per_slot_kwh: float | None = None,
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
        session_slots_set: Session slot indices where grid-charge is blocked.
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

    # Concentrate flexible EV fragments below startup power into already
    # allocated slots without exceeding per-slot EV or grid-import headroom.
    for ev_idx, ev in enumerate(active_evs):
        if ev.fixed_session_only or ev.charger_min_power_w <= 1e-9:
            continue
        ev_off = ev_var_offsets[ev_idx]
        values = executable_x[ev_off : ev_off + m].copy()
        original_total_dc = float(np.sum(values))
        original_values_snapshot = values.copy()
        full_slot_hours = slot_duration_hours(
            slots[future_idx[0]].start, slots[future_idx[0]].end
        )
        minimum_dc = (
            ev.charger_min_power_w / 1000.0 * full_slot_hours * ev.charger_efficiency
        )
        donor_energy = 0.0
        for t, value in enumerate(values):
            if _min_action_kwh < value < minimum_dc - 1e-9:
                donor_energy += float(value)
                values[t] = 0.0

        def _current_gi_kwh(t: int) -> float:
            """Return slot ``t``'s grid import, adjusted for this EV's shift.

            ``executable_x[gi_off + t]`` is the solved value from before
            concentration/quantization moved any of this EV's own energy
            between slots; it is never re-solved afterwards.  Assuming
            everything else in the slot's energy balance is unchanged, a
            shift of this EV's own allocation moves grid import by the same
            delta, so the live grid import is approximated by applying that
            delta to the stale solved value (issue #789).  Only called when
            ``gi_off is not None`` (guarded by the caller).
            """
            assert gi_off is not None
            return float(executable_x[gi_off + t]) + (
                float(values[t]) - float(original_values_snapshot[t])
            )

        def _room_dc(t: int) -> float:
            """Headroom slot ``t`` can accept without breaking its own caps."""
            room_dc = max(ev.max_charge_per_slot - float(values[t]), 0.0)
            if grid_import_cap_per_slot_kwh is not None and gi_off is not None:
                grid_room_ac = max(
                    float(grid_import_cap_per_slot_kwh[t]) - _current_gi_kwh(t),
                    0.0,
                )
                room_dc = min(room_dc, grid_room_ac * ev.charger_efficiency)
            if max_phase_import_per_slot_kwh is not None:
                # Per-phase fuse headroom (EV charger phase topology): the
                # same envelope as the hard constraint rows — gi/3 +
                # (share - 1/3)·E_ac ≤ cap — so concentration cannot merge
                # fragments into a slot whose phase envelope would exceed
                # the single-phase fuse.  Each AC kW of added EV draw
                # raises the envelope by exactly ``share``.
                share = ev_phase_share_for_slot(active_evs=active_evs)[
                    1 if ev.is_second else 0
                ]
                gi_kwh = (
                    _current_gi_kwh(t)
                    if grid_import_cap_per_slot_kwh is not None and gi_off is not None
                    else 0.0
                )
                envelope_now = (
                    gi_kwh / 3.0
                    + (share - 1.0 / 3.0) * float(values[t]) / ev.charger_efficiency
                )
                phase_room_ac = max(
                    (max_phase_import_per_slot_kwh - envelope_now) / max(share, 1e-9),
                    0.0,
                )
                room_dc = min(room_dc, phase_room_ac * ev.charger_efficiency)
            return room_dc

        recipients = sorted(
            (t for t in range(m) if t not in session_slots_set),
            key=lambda t: float(values[t]),
            reverse=True,
        )
        for t in recipients:
            if donor_energy <= 1e-9:
                break
            added = min(_room_dc(t), donor_energy)
            values[t] += added
            donor_energy -= added

        # Only deadline-driven charging may open a further slot for a
        # stranded residue — past-target charging is opportunistic
        # surplus-only demand with no target to protect.
        reportioned_slot_i: int | None = None
        if ev.deadline_slot is not None and not ev.charge_past_target:
            deadline_lp_limit = max(0, min(int(ev.deadline_slot), m - 1))
            values_by_slot = {t: float(values[t]) for t in range(m)}
            values_by_slot, donor_energy, reportioned_lp_t = (
                _redistribute_below_minimum_power(
                    values_by_slot,
                    minimum_dc=minimum_dc,
                    deadline_lp_limit=deadline_lp_limit,
                    session_slots_set=session_slots_set,
                    room_dc=_room_dc,
                    donor_energy=donor_energy,
                )
            )
            if reportioned_lp_t is not None:
                for t, v in values_by_slot.items():
                    values[t] = v
                reportioned_slot_i = future_idx[reportioned_lp_t]

        # ------------------------------------------------------------
        # Whole-amp quantization (issue #789): the continuous LP result
        # is only a budget.  An external current controller can only
        # command whole amps, so the published energy must be what that
        # controller can actually deliver — not an idealised continuous
        # value the ceiling sensor then floors on display, silently
        # diverging from the cost/energy accounting.
        # ------------------------------------------------------------
        rated_ac_power_w = charger_current_to_power_w(
            charger_max_power_to_current_a(
                (
                    ev_dc_to_ac_kwh(ev.max_charge_per_slot, ev.charger_efficiency)
                    / full_slot_hours
                )
                * 1000.0,
                ev.charger_phase_topology,
            ),
            ev.charger_phase_topology,
        )
        effective_min_power_w = charger_current_to_power_w(
            charger_min_power_to_current_a(
                ev.charger_min_power_w, ev.charger_phase_topology
            ),
            ev.charger_phase_topology,
        )

        # A managed live session still receives an HSEM current ceiling.
        # Its fixed measured LP energy must therefore be reduced to the
        # same whole-amp command that will be published; the fractional
        # residue is handed to the flexible slots below to recover.
        session_quantization_residue_dc = 0.0
        min_current_a = max(
            charger_min_power_to_current_a(
                effective_min_power_w, ev.charger_phase_topology
            ),
            1,
        )
        for t in range(m):
            if t not in session_slots_set:
                continue
            measured_dc = float(values[t])
            if measured_dc <= _min_action_kwh:
                continue
            measured_power_w = (
                ev_dc_to_ac_kwh(measured_dc, ev.charger_efficiency)
                / full_slot_hours
                * 1000.0
            )
            command_current_a = min(
                charger_power_to_current_a(measured_power_w, ev.charger_phase_topology),
                charger_max_power_to_current_a(
                    rated_ac_power_w, ev.charger_phase_topology
                ),
            )
            if command_current_a < min_current_a:
                command_current_a = 0
            executable_dc = (
                charger_current_to_power_w(command_current_a, ev.charger_phase_topology)
                * full_slot_hours
                * ev.charger_efficiency
                / 1000.0
            )
            session_quantization_residue_dc += max(measured_dc - executable_dc, 0.0)
            values[t] = executable_dc

        # Flexible (non-session) allocation: floor every slot down to what a
        # whole-amp command can deliver, then try to recover the pooled
        # fractional residue (including the session's own residue above) by
        # bumping other occupied slots up by whole amp-steps, or opening one
        # further deadline-eligible slot at the charger minimum.
        dc_by_slot = {
            t: float(values[t])
            for t in range(m)
            if t not in session_slots_set and float(values[t]) > _min_action_kwh
        }
        unplaceable_quant_dc = 0.0
        reportioned_quant_slots: list[int] = []
        if dc_by_slot or session_quantization_residue_dc > _min_action_kwh:
            rated_dc_per_slot = (
                rated_ac_power_w * full_slot_hours * ev.charger_efficiency / 1000.0
            )
            slot_ceiling_dc: dict[int, float] = {
                t: min(rated_dc_per_slot, dc + max(_room_dc(t), 0.0))
                for t, dc in dc_by_slot.items()
            }
            candidate_dc: dict[int, float] = {}
            if ev.deadline_slot is not None and not ev.charge_past_target:
                deadline_lp_limit = max(0, min(int(ev.deadline_slot), m - 1))
                for t in range(deadline_lp_limit + 1):
                    if t in session_slots_set or t in dc_by_slot:
                        continue
                    ceiling = min(rated_dc_per_slot, max(_room_dc(t), 0.0))
                    if ceiling > _min_action_kwh:
                        candidate_dc[t] = ceiling
                        slot_ceiling_dc[t] = ceiling
            quantized_dc, unplaceable_quant_dc = _quantize_ev_allocation_to_whole_amps(
                dc_by_slot,
                target_dc_kwh=sum(dc_by_slot.values())
                + session_quantization_residue_dc,
                slot_hours=dict.fromkeys(
                    set(dc_by_slot) | set(candidate_dc), full_slot_hours
                ),
                charger_efficiency=ev.charger_efficiency,
                charger_min_power_w=effective_min_power_w,
                rated_ac_power_w=rated_ac_power_w,
                charger_phase_topology=ev.charger_phase_topology,
                slot_ceiling_dc=slot_ceiling_dc,
                candidate_dc=candidate_dc,
            )
            for t in dc_by_slot:
                values[t] = 0.0
            for t, dc in quantized_dc.items():
                values[t] = dc
            reportioned_quant_slots = sorted(set(quantized_dc) - set(dc_by_slot))

        executable_x[ev_off : ev_off + m] = values
        if reportioned_slot_i is not None:
            log_planner(
                "debug",
                "[milp] EV%s: re-portioned into slot %d to keep the deadline "
                "target reachable at the %.0f W minimum",
                "2" if ev.is_second else "1",
                reportioned_slot_i,
                ev.charger_min_power_w,
            )
        if reportioned_quant_slots:
            log_planner(
                "debug",
                "[milp] EV%s: re-portioned into %d additional slot(s) %s for "
                "whole-amp quantization at the %.0f W minimum",
                "2" if ev.is_second else "1",
                len(reportioned_quant_slots),
                reportioned_quant_slots,
                effective_min_power_w,
            )
        total_unplaceable_quant_dc = (
            unplaceable_quant_dc + session_quantization_residue_dc
        )
        if total_unplaceable_quant_dc > 1e-6:
            log_planner(
                "debug",
                "[milp] EV%s: %.3f kWh could not be placed at or above the "
                "charger minimum of %.0f W after whole-amp quantization",
                "2" if ev.is_second else "1",
                total_unplaceable_quant_dc,
                effective_min_power_w,
            )
        if ev_writeback_diagnostics is not None:
            delivered_by_deadline = float(np.sum(values))
            if ev.deadline_slot is not None:
                delivered_by_deadline = float(
                    np.sum(values[: max(0, min(ev.deadline_slot, m - 1)) + 1])
                )
            deadline_penalty = max(
                ev.target_kwh - ev.initial_soc_kwh - delivered_by_deadline,
                0.0,
            )
            ev_writeback_diagnostics[f"ev{ev_idx}"] = {
                "total_dc_kwh": round(float(np.sum(values)), 4),
                "deadline_penalty_kwh": round(deadline_penalty, 4),
                "deadline_met": deadline_penalty < 1e-6,
                "unplaceable_dc_kwh": round(
                    max(original_total_dc - float(np.sum(values)), 0.0),
                    4,
                ),
                "reportioned_slots": (
                    (1 if reportioned_slot_i is not None else 0)
                    + len(reportioned_quant_slots)
                ),
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
                ev.target_kwh - ev.initial_soc_kwh - delivered_by_deadline,
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

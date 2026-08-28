"""Write MILP-derived per-slot EV charger power fields onto output slots.

Extracted from ``_write_results.py`` so it stays under the 30 KB file limit.
Pure move: no behaviour change.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.utils.units import ev_dc_to_ac_kwh, timedelta_to_hours


def _write_ev_power_fields_to_slots(
    out_slots: list[PlannedSlot],
    future_idx: list[int],
    now: datetime,
    executable_x: np.ndarray,  # type: ignore[name-defined]
    m: int,
    full_slot_hours: float,
    active_evs: list[EVConfig],
    ev_var_offsets: list[int],
    _min_action_kwh: float,
) -> None:
    """Write the executable per-slot EV charger power fields in place.

    Args:
        out_slots: Output slot list, mutated in place.
        future_idx: Indices of future (LP-variable) slots.
        now: Current datetime for remaining-slot-time power calculation.
        executable_x: Full LP solution vector after EV write-back adjustments
            (concentration, minimum-power redistribution, whole-amp
            quantization).
        m: Number of active LP slots (``len(future_idx)``).
        full_slot_hours: Duration of one full future slot, in hours.
        active_evs: List of active EV configs for EV write-out.
        ev_var_offsets: Start offset of each EV's ``ev_c[t]`` block.
        _min_action_kwh: Minimum kWh threshold for action slots.
    """
    for ev_idx, ev in enumerate(active_evs):
        ev_off = ev_var_offsets[ev_idx]
        ev_c_sol = executable_x[ev_off : ev_off + m]
        # Redistribution target window (issue #845): a deadline-driven EV
        # may move a below-minimum-power slot's energy forward instead of
        # discarding it.  Charge-past-target EVs have no deadline to
        # protect, so they're excluded.
        deadline_lp_limit = (
            max(0, min(ev.deadline_slot, m - 1))
            if ev.deadline_slot is not None and not ev.charge_past_target
            else None
        )
        for lp_t, slot_i in enumerate(future_idx):
            ev_dc_kwh = float(ev_c_sol[lp_t])
            if ev_dc_kwh < _min_action_kwh:
                continue
            # AC load = DC / charger_eff (grid/PV draw)
            ac_load = round(ev_dc_to_ac_kwh(ev_dc_kwh, ev.charger_efficiency), 3)
            # Accumulate into slot EV fields (additive for multiple EVs)
            if ev.base_load_includes_ev:
                out_slots[slot_i].ev_accounted_load_kwh += ac_load
            else:
                out_slots[slot_i].ev_planned_load_kwh += ac_load
            out_slots[slot_i].ev_total_planned_load_kwh += ac_load

            # Compute AC charger target power (W) for this EV in this slot.
            # For the current (partially elapsed) slot, use remaining time
            # instead of the full slot width so the charger ramps to meet
            # the MILP's energy target within the available minutes.
            #
            # Cap at the charger's rated AC power — the MILP treats all
            # slots as full-width, so it may allocate max_charge_per_slot
            # to a slot with only a few minutes remaining.  The charger
            # physically cannot exceed its nameplate rating.
            max_ac_power_w = round(
                (
                    ev_dc_to_ac_kwh(ev.max_charge_per_slot, ev.charger_efficiency)
                    / full_slot_hours
                )
                * 1000
            )
            slot_start = out_slots[slot_i].start
            slot_end = out_slots[slot_i].end
            if slot_start <= now < slot_end:
                remaining_hours = max(
                    timedelta_to_hours(slot_end - now),
                    1.0 / 3600.0,  # 1 s minimum guard
                )
                ac_power_w = round(
                    (
                        ev_dc_to_ac_kwh(ev_dc_kwh, ev.charger_efficiency)
                        / remaining_hours
                    )
                    * 1000
                )
            else:
                ac_power_w = round(
                    (
                        ev_dc_to_ac_kwh(ev_dc_kwh, ev.charger_efficiency)
                        / full_slot_hours
                    )
                    * 1000
                )
            ac_power_w = min(ac_power_w, max_ac_power_w)

            if ev.fixed_session_only:
                # Keep measured energy in site accounting without emitting
                # an HSEM command for an unmanaged session.
                continue

            # Floor at the charger's minimum operating power — if the
            # target power is below the minimum the charger needs to
            # start, it will never deliver any energy.  Zero out the
            # field so the applier does not attempt to throttle below
            # the minimum.
            if ev.charger_min_power_w > 1e-9 and ac_power_w < ev.charger_min_power_w:
                # Before discarding, try to move this slot's energy forward
                # onto a later pre-deadline slot with headroom (issue #845)
                # instead of silently losing it.  ``ev_c_sol`` is mutated in
                # place, so the loop's own later iteration over the target
                # slot picks up the boosted value, re-derives its charger
                # command from it, and re-subjects it to this same floor
                # check.  The grid-balance correction below mirrors the
                # discard formula (net_grid = import - export ± ac_load),
                # since the extra draw at the target slot wasn't accounted
                # for by the original MILP-level grid-balance write-out.
                remaining_dc = ev_dc_kwh
                if deadline_lp_limit is not None:
                    for lp_t2 in range(lp_t + 1, deadline_lp_limit + 1):
                        if remaining_dc <= 1e-9:
                            break
                        headroom_dc = max(
                            ev.max_charge_per_slot - float(ev_c_sol[lp_t2]), 0.0
                        )
                        if headroom_dc <= 1e-9:
                            continue
                        take_dc = min(headroom_dc, remaining_dc)
                        ev_c_sol[lp_t2] += take_dc
                        take_ac = ev_dc_to_ac_kwh(take_dc, ev.charger_efficiency)
                        target_slot_i = future_idx[lp_t2]
                        target_net_grid = (
                            out_slots[target_slot_i].grid_import_kwh
                            - out_slots[target_slot_i].grid_export_kwh
                            + take_ac
                        )
                        out_slots[target_slot_i].grid_import_kwh = round(
                            max(target_net_grid, 0.0), 3
                        )
                        out_slots[target_slot_i].grid_export_kwh = round(
                            max(-target_net_grid, 0.0), 3
                        )
                        remaining_dc -= take_dc
                # The charger cannot execute this fragment. Remove its
                # energy and grid flow instead of publishing energy with a
                # zero command.  (Whatever was placed forward above is now
                # counted at its new slot instead, so this slot's own
                # contribution must still go to zero either way.)
                if ev.base_load_includes_ev:
                    out_slots[slot_i].ev_accounted_load_kwh = round(
                        max(out_slots[slot_i].ev_accounted_load_kwh - ac_load, 0.0),
                        3,
                    )
                else:
                    out_slots[slot_i].ev_planned_load_kwh = round(
                        max(out_slots[slot_i].ev_planned_load_kwh - ac_load, 0.0),
                        3,
                    )
                out_slots[slot_i].ev_total_planned_load_kwh = round(
                    max(out_slots[slot_i].ev_total_planned_load_kwh - ac_load, 0.0),
                    3,
                )
                net_grid = (
                    out_slots[slot_i].grid_import_kwh
                    - out_slots[slot_i].grid_export_kwh
                    - ac_load
                )
                out_slots[slot_i].grid_import_kwh = round(max(net_grid, 0.0), 3)
                out_slots[slot_i].grid_export_kwh = round(max(-net_grid, 0.0), 3)
                continue

            # Write to the correct charger power field by EV identity
            # (is_second), NOT by list position (ev_idx).  When the
            # primary EV is disabled, active_evs[0] IS the second EV,
            # and ev_idx==0 would incorrectly route its power to
            # ev_charger_calculated_power instead of
            # ev_second_charger_calculated_power (issue #646).
            if ev.is_second:
                out_slots[slot_i].ev_second_charger_calculated_power = max(
                    ac_power_w,
                    out_slots[slot_i].ev_second_charger_calculated_power,
                )
            else:
                out_slots[slot_i].ev_charger_calculated_power = max(
                    ac_power_w, out_slots[slot_i].ev_charger_calculated_power
                )
    # Recompute estimated net consumption to reflect executable EV loads.
    for i in future_idx:
        s = out_slots[i]
        s.estimated_net_consumption_kwh = (
            s.avg_house_consumption_kwh
            + s.ev_planned_load_kwh
            - s.solcast_pv_estimate_kwh
        )

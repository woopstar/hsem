"""Compute MILP diagnostics and violations after the LP solves.

Extracted from ``solve_milp`` so the orchestrator remains under 30 KB.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot


def _compute_milp_diagnostics(
    result: Any,  # scipy.optimize.OptimizeResult
    out_slots: list[PlannedSlot],
    slots: list[PlannedSlot],
    future_idx: list[int],
    m: int,
    s_max_off: int,
    s_min_off: int,
    curt_off: int,
    gi_off: int,
    ge_off: int,
    gi_pen_off: int,
    current_kwh: float,
    usable_kwh: float,
    replacement_price_per_kwh: float | None,
    min_export_price: float,
    p_imp_obj: np.ndarray,  # type: ignore[name-defined]
    discharge_loss: float,
    fuse_active: bool,
    max_grid_import_per_slot_kwh: float,
    active_evs: list[EVConfig],
    ev_var_offsets: list[int],
    ev_pen_offsets: list[int],
    terminal_soc_credit: float,
    *,
    _min_action_kwh: float = 1e-4,
) -> dict:
    """Build the diagnostics dict from the solved LP.

    Args:
        result: The ``scipy.optimize.OptimizeResult`` from ``linprog``.
        out_slots: Already-written output slots (from
            :func:`_write_milp_results_to_slots`).
        slots: Original (unmodified) slot list.
        future_idx: Indices of future (LP-variable) slots.
        m: Number of active LP slots.
        s_max_off, s_min_off, curt_off, gi_off, ge_off, gi_pen_off:
            Variable offsets into ``result.x``.
        current_kwh: Battery energy at horizon start (above floor, kWh).
        usable_kwh: Maximum usable energy (kWh).
        replacement_price_per_kwh: Terminal-SoC replacement price.
        min_export_price: Minimum export price threshold.
        p_imp_obj: Sanitised import-price array (objective coefficients).
        discharge_loss: Discharge-side loss fraction (0-1).
        fuse_active: Whether the main-fuse constraint is active.
        max_grid_import_per_slot_kwh: Max grid import per slot (kWh).
        active_evs: List of active EV configs.
        ev_var_offsets: Start offset of each EV's ``ev_c[t]`` block.
        ev_pen_offsets: Index of deadline penalty per EV.
        terminal_soc_credit: Pre-computed terminal-SoC credit (currency).
        _min_action_kwh: Minimum kWh threshold for action slots.

    Returns:
        Diagnostics dict with keys ``s_max_pen``, ``s_min_pen``,
        ``has_violations``, ``total_violation_kwh``,
        ``total_fuse_violation_kwh``, ``terminal_soc_credit``,
        ``total_curtailment_kwh``, ``discharge_loss_cost_destination_aware``,
        and optionally ``ev``.
    """
    import numpy as np

    from custom_components.hsem.utils.logger import log_planner
    from custom_components.hsem.utils.recommendations import Recommendations

    # ------------------------------------------------------------------
    # Compute violation diagnostics from the penalty variables
    # ------------------------------------------------------------------
    s_max_pen_vals = [float(v) for v in result.x[s_max_off : s_max_off + m]]
    s_min_pen_vals = [float(v) for v in result.x[s_min_off : s_min_off + m]]

    s_max_pen_list = list(s_max_pen_vals)
    s_min_pen_list = list(s_min_pen_vals)
    total_violation = sum(s_max_pen_list) + sum(s_min_pen_list)
    has_violations = total_violation > 1e-6

    if has_violations:
        violating_slots: list[dict] = []
        for t in range(m):
            slot_i = future_idx[t]
            s_start = slots[slot_i].start.isoformat()
            if s_max_pen_list[t] > 1e-6:
                violating_slots.append(
                    {
                        "slot": t,
                        "time": s_start,
                        "type": "s_max_pen",
                        "kwh": round(s_max_pen_list[t], 4),
                    }
                )
            if s_min_pen_list[t] > 1e-6:
                violating_slots.append(
                    {
                        "slot": t,
                        "time": s_start,
                        "type": "s_min_pen",
                        "kwh": round(s_min_pen_list[t], 4),
                    }
                )
        log_planner(
            "warning",
            "[milp] SoC penalty violations detected: total=%.4f kWh, %d violating slots",
            total_violation,
            len(violating_slots),
        )
        for v in violating_slots:
            log_planner(
                "warning",
                "[milp] Penalty slot %d (%s) %s: %.4f kWh",
                v["slot"],
                v["time"],
                v["type"],
                v["kwh"],
            )

    # --- Extract fuse penalty values ---
    total_fuse_violation_kwh = 0.0
    if fuse_active:
        gi_pen_sol = result.x[gi_pen_off : gi_pen_off + m]
        gi_pen_list = [float(v) for v in gi_pen_sol]
        total_fuse_violation_kwh = sum(gi_pen_list)
        if total_fuse_violation_kwh > 1e-6:
            has_violations = True
            for t in range(m):
                if gi_pen_list[t] > 1e-6:
                    slot_i = future_idx[t]
                    s_start = slots[slot_i].start.isoformat()
                    gi_val = float(result.x[gi_off + t])
                    log_planner(
                        "warning",
                        "[milp] Fuse violation slot %d (%s): "
                        "grid_import=%.3f kWh  limit=%.3f kWh  excess=%.3f kWh",
                        t,
                        s_start,
                        gi_val,
                        max_grid_import_per_slot_kwh,
                        gi_pen_list[t],
                    )
            log_planner(
                "warning",
                "[milp] Main fuse violations detected: total=%.4f kWh excess",
                total_fuse_violation_kwh,
            )

    curt_sol_full = result.x[curt_off : curt_off + m]
    total_curtailment_kwh = float(np.sum(curt_sol_full))

    log_planner(
        "debug",
        "[milp] LP solved: objective=%.4f  charge_slots=%d  discharge_slots=%d"
        "  replacement_price=%s  penalty_total=%.4f  has_violations=%s"
        "  ev_slots=%d  terminal_soc_credit=%.4f  curtailment=%.4f",
        float(result.fun),
        sum(
            1
            for i in future_idx
            if out_slots[i].recommendation
            in (
                Recommendations.BatteriesChargeGrid.value,
                Recommendations.BatteriesChargeSolar.value,
            )
        ),
        sum(
            1
            for i in future_idx
            if out_slots[i].recommendation
            == Recommendations.BatteriesDischargeMode.value
        ),
        (
            f"{replacement_price_per_kwh:.4f}"
            if replacement_price_per_kwh is not None
            else "(none)"
        ),
        total_violation,
        has_violations,
        sum(1 for s in out_slots if abs(s.ev_total_planned_load_kwh) > _min_action_kwh),
        terminal_soc_credit,
        total_curtailment_kwh,
    )

    # ------------------------------------------------------------------
    # Post-hoc destination-aware discharge loss cost (issue #641).
    #
    # The LP's pre-solve objective uses p_imp_obj for ed[t] as a
    # conservative approximation (the LP cannot know the discharge
    # destination before solving).  Once the LP has solved and the
    # per-slot grid_export_kwh / grid_import_kwh fields are written,
    # we can compute the economically accurate cost using the actual
    # destination of each discharged kWh.
    #
    # This value should match cost_function.py::score_plan()'s
    # conversion_loss_cost for the discharge-side portion.
    # ------------------------------------------------------------------
    discharge_loss_dest_aware = 0.0
    for t in range(m):
        slot_i = future_idx[t]
        s = out_slots[slot_i]
        if s.batteries_discharged_kwh <= 1e-9:
            continue
        lost_kwh = s.batteries_discharged_kwh * discharge_loss
        if s.grid_export_kwh > 1e-9:
            # Export-destined discharge: price at export price.
            exp_p = slots[slot_i].price.export_price
            # Apply same sanitisation as cost_function.py
            if min_export_price > 1e-9 and exp_p < min_export_price:
                exp_p = 0.0
            p_loss = max(exp_p, 0.0)
        else:
            # House-load-covering discharge: price at import price.
            p_loss = p_imp_obj[t]
        discharge_loss_dest_aware += lost_kwh * p_loss

    diagnostics: dict = {
        "s_max_pen": s_max_pen_list,
        "s_min_pen": s_min_pen_list,
        "has_violations": has_violations,
        "total_violation_kwh": round(total_violation, 4),
        "total_fuse_violation_kwh": round(total_fuse_violation_kwh, 4),
        "terminal_soc_credit": round(terminal_soc_credit, 4),
        "total_curtailment_kwh": round(total_curtailment_kwh, 4),
        "discharge_loss_cost_destination_aware": round(discharge_loss_dest_aware, 6),
    }

    # --- EV diagnostics ---
    if active_evs:
        ev_diag: dict = {}
        for ev_idx, _ev in enumerate(active_evs):
            ev_off = ev_var_offsets[ev_idx]
            ev_c_sol = result.x[ev_off : ev_off + m]
            ev_total_dc = float(np.sum(ev_c_sol))
            ev_pen_val = float(result.x[ev_pen_offsets[ev_idx]])
            ev_diag[f"ev{ev_idx}"] = {
                "total_dc_kwh": round(ev_total_dc, 4),
                "deadline_penalty_kwh": round(ev_pen_val, 4),
                "deadline_met": ev_pen_val < 1e-6,
            }
        diagnostics["ev"] = ev_diag

    return diagnostics

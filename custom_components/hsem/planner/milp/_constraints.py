"""Build MILP constraints and variable bounds.

Extracted from ``solve_milp`` so the orchestrator remains under 30 KB.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from custom_components.hsem.models.ev_config import EVConfig


def _build_constraints(
    m: int,
    n_vars: int,
    ec_off: int,
    ed_off: int,
    gi_off: int,
    ge_off: int,
    pv_off: int,
    m_off: int,
    curt_off: int,
    gi_pen_off: int,
    s_max_off: int,
    s_min_off: int,
    ev_var_offsets: list[int],
    ev_pen_offsets: list[int],
    active_evs: list[EVConfig],
    pv_avail: np.ndarray,  # type: ignore[name-defined]
    base_load: np.ndarray,  # type: ignore[name-defined]
    ev_accounted: np.ndarray,  # type: ignore[name-defined]
    charge_eff: float,
    discharge_eff: float,
    current_kwh: float,
    usable_kwh: float,
    max_charge_per_slot: float,
    max_dis: float,
    max_grid_import_per_slot_kwh: float,
    fuse_active: bool,
    session_slots_set: set[int],
    session_ev_indices: list[int],
    session_slots: int,
    slot_hours: float,
    _has_session_demand: bool,
) -> dict:
    """Build all LP constraint matrices and variable bounds.

    Returns a dict with keys:
        ``A_eq``, ``b_eq``, ``A_ub``, ``b_ub``, ``bounds``,
        ``ev_discharge_guard_active``, ``ed_ub_per_slot``.
    """
    import numpy as np

    # ------------------------------------------------------------------
    # Equality constraints: energy balance per slot
    # gi[t] + pv[t] + ed[t]*discharge_eff
    #     = base_load[t] + ec[t]/charge_eff + ge[t] + curt[t] + Σ ev_c/eff
    # ->  gi - ec/η_chg + ed·η_dis + pv - ge - curt - Σ ev_c/eff = base_load
    #
    # EV charge energy ev_c[t] is DC-side (delivered to EV battery).
    # The AC grid/PV draw is ev_c[t] / charger_efficiency — that is the
    # load the house must supply.  base_load already EXCLUDES EV load
    # when ev_configs is active (net_load was rebuilt without EV).
    #
    # curt[t] allows the LP to explicitly curtail PV when battery is full
    # and export prices are low/negative.
    # ------------------------------------------------------------------
    A_eq = np.zeros((m, n_vars))  # NOSONAR
    for t in range(m):
        A_eq[t, ec_off + t] = -1.0 / charge_eff  # -ec[t]/charge_eff
        A_eq[t, ed_off + t] = 1.0 * discharge_eff  # +ed[t]*discharge_eff
        A_eq[t, gi_off + t] = 1.0  # +gi[t]
        A_eq[t, ge_off + t] = -1.0  # -ge[t]
        A_eq[t, pv_off + t] = 1.0  # +pv[t] (fixed to pv_avail[t])
        A_eq[t, curt_off + t] = -1.0  # -curt[t] (curtailment reduces available PV)
        # EV AC load: -ev_c[t] / charger_eff per active EV
        for ev_idx, ev in enumerate(active_evs):
            A_eq[t, ev_var_offsets[ev_idx] + t] = -1.0 / ev.charger_efficiency
    b_eq = base_load.copy()  # always non-negative — pv[t] covers surplus

    # ------------------------------------------------------------------
    # Inequality constraints:
    #   1. SoC recurrence: soc[t] = soc[0] + Σ_{k≤t} (ec[k] − ed[k])
    #      Upper (soft): Σ_{k≤t}(ec[k]−ed[k]) − s_max_pen[t] ≤ usable−soc0
    #      Lower (soft): −Σ_{k≤t}(ec[k]−ed[k]) − s_min_pen[t] ≤ soc0
    #      Penalty variables s_max_pen[t] and s_min_pen[t] absorb violations
    #      at high cost, preventing infeasibility from out-of-bounds initial SoC.
    #   2. Mutual exclusion: ec[t]/max_charge + ed[t]/max_dis ≤ 1
    #   3. ec[t] ≤ max_charge_per_slot  (via bounds)
    #   4. ed[t] ≤ max_dis              (via bounds)
    # ------------------------------------------------------------------
    # We encode SoC bounds as inequality rows:
    #   upper: cumsum(ec−ed)[t] − s_max_pen[t] ≤ (usable_kwh − current_kwh)
    #   lower: −cumsum(ec−ed)[t] − s_min_pen[t] ≤ current_kwh
    soc_rows = 2 * m
    # Mutual exclusion rows: ec[t]/max_charge + ed[t]/max_dis <= 1
    mutex_rows = m
    # Cycle cost auxiliary rows: m[t] >= ec[t] and m[t] >= ed[t]
    #   → -m[t] + ec[t] <= 0  and  -m[t] + ed[t] <= 0
    cycle_rows = 2 * m
    A_ub = np.zeros((soc_rows + mutex_rows + cycle_rows, n_vars))  # NOSONAR
    b_ub = np.zeros(soc_rows + mutex_rows + cycle_rows)

    for t in range(m):
        for k in range(t + 1):
            # Upper SoC bound row (soft)
            A_ub[t, ec_off + k] = 1.0
            A_ub[t, ed_off + k] = -1.0
            # Lower SoC bound row (soft)
            A_ub[m + t, ec_off + k] = -1.0
            A_ub[m + t, ed_off + k] = 1.0
        # Penalty variable absorbs violation in upper bound
        A_ub[t, s_max_off + t] = -1.0
        # Penalty variable absorbs violation in lower bound
        A_ub[m + t, s_min_off + t] = -1.0
        b_ub[t] = usable_kwh - current_kwh  # upper SoC headroom
        b_ub[m + t] = current_kwh  # lower SoC headroom

        # Mutual exclusion: ec[t]/max_charge + ed[t]/max_dis <= 1
        A_ub[2 * m + t, ec_off + t] = 1.0 / max_charge_per_slot
        A_ub[2 * m + t, ed_off + t] = 1.0 / max_dis
        b_ub[2 * m + t] = 1.0

    # Cycle cost auxiliary: m[t] >= ec[t]  →  -m[t] + ec[t] <= 0
    #                     m[t] >= ed[t]  →  -m[t] + ed[t] <= 0
    cycle_row_start = soc_rows + mutex_rows  # = 3m
    for t in range(m):
        A_ub[cycle_row_start + t, ec_off + t] = 1.0
        A_ub[cycle_row_start + t, m_off + t] = -1.0
        b_ub[cycle_row_start + t] = 0.0
        A_ub[cycle_row_start + m + t, ed_off + t] = 1.0
        A_ub[cycle_row_start + m + t, m_off + t] = -1.0
        b_ub[cycle_row_start + m + t] = 0.0

    # ------------------------------------------------------------------
    # EV discharge guard: when base_load_includes_ev=True and EV
    # co-optimisation is NOT active, the EV load is already captured in
    # avg_house_consumption_kwh via ev_accounted_load_kwh.  The battery
    # must not discharge to cover this portion of base_load — the EV is
    # served by grid (or PV).
    #
    # When co-optimisation IS active, the EV has its own decision
    # variables and base_load already excludes EV load, so the guard is
    # skipped.
    #
    # Without this cap, the live-injected current-slot house consumption
    # (which includes EV power when the CT clamp is upstream of the
    # charger) causes the MILP to discharge the home battery into the EV
    # (issue #592).
    #
    # Per-slot upper bound on ed: ed[t] ≤ max(0, base_load[t] - ev_acct[t]) / η_dis
    # Only slots where ev_accounted > 0 are capped; uncapped slots use max_dis.
    # This does NOT affect export — when base_load=0 (PV surplus), ev_acct is
    # already in avg_house_consumption which is covered by PV, and the battery
    # can still export freely.
    # ------------------------------------------------------------------
    ev_discharge_guard_active = (not active_evs) and bool(np.any(ev_accounted > 1e-9))
    ed_ub_per_slot: list[float] = []
    for t in range(m):
        if ev_discharge_guard_active and ev_accounted[t] > 1e-9:
            cap = max(base_load[t] - ev_accounted[t], 0.0) / discharge_eff
            ed_ub_per_slot.append(min(cap, max_dis))
        else:
            ed_ub_per_slot.append(max_dis)

    # ------------------------------------------------------------------
    # EV constraints (only when active_evs is non-empty)
    # ------------------------------------------------------------------
    # Row counts for EV constraints
    num_evs = len(active_evs)
    ev_soc_rows = num_evs * m  # cumulative SOC upper bound per EV
    ev_deadline_rows = sum(
        1
        for ev in active_evs
        if ev.deadline_slot is not None and ev.target_kwh > ev.initial_soc_kwh + 1e-9
    )
    # Post-deadline zero-charge rows: for EVs with a deadline and no
    # charge-past-target, ev_c[t] = 0 for all t > deadline_slot.
    ev_post_deadline_rows = sum(
        m - 1 - max(0, min(ev.deadline_slot, m - 1))
        for ev in active_evs
        if ev.deadline_slot is not None
        and ev.target_kwh > ev.initial_soc_kwh + 1e-9
        and not ev.charge_past_target
    )
    # Target-cap rows: for EVs with a deadline and no charge-past-target,
    # Σ_{k≤D} ev_c[k] ≤ target_kwh - initial_soc_kwh
    # Caps EV charging at the economic target for pre-deadline slots,
    # preventing overcharge to full capacity_kwh.
    ev_target_rows = sum(
        1
        for ev in active_evs
        if ev.deadline_slot is not None
        and ev.target_kwh > ev.initial_soc_kwh + 1e-9
        and not ev.charge_past_target
    )
    # Surplus-only rows: for charge-past-target EVs, ev_c[t]/eff ≤ max(0, pv[t] - base_load[t])
    ev_surplus_rows = sum(1 for ev in active_evs if ev.charge_past_target) * m
    ev_total_rows = (
        ev_soc_rows
        + ev_deadline_rows
        + ev_target_rows
        + ev_post_deadline_rows
        + ev_surplus_rows
    )

    if ev_total_rows > 0:
        # Extend A_ub and b_ub to accommodate EV rows
        existing_rows = soc_rows + mutex_rows + cycle_rows
        A_ub_old = A_ub
        b_ub_old = b_ub
        A_ub = np.zeros((existing_rows + ev_total_rows, n_vars))
        b_ub = np.zeros(existing_rows + ev_total_rows)
        A_ub[:existing_rows, :] = A_ub_old
        b_ub[:existing_rows] = b_ub_old

        ev_row = existing_rows
        for ev_idx, ev in enumerate(active_evs):
            ev_off = ev_var_offsets[ev_idx]
            # EV SOC upper bound per slot: Σ_{k≤t} ev_c[k] ≤ cap − init
            #   For each t in 0..m-1:
            #   Σ_{k=0..t} ev_c[k] ≤ ev.capacity_kwh - ev.initial_soc_kwh
            headroom = max(ev.capacity_kwh - ev.initial_soc_kwh, 0.0)
            for t in range(m):
                for k in range(t + 1):
                    A_ub[ev_row + t, ev_off + k] = 1.0
                b_ub[ev_row + t] = headroom
            ev_row += m

            # EV deadline soft constraint:
            # initial_soc + Σ_{k≤D} ev_c[k] + penalty ≥ target
            # → -Σ_{k≤D} ev_c[k] - penalty ≤ initial_soc - target
            if (
                ev.deadline_slot is not None
                and ev.target_kwh > ev.initial_soc_kwh + 1e-9
            ):
                d = ev.deadline_slot
                # Clamp deadline to valid range
                d = max(0, min(d, m - 1))
                for k in range(d + 1):
                    A_ub[ev_row, ev_off + k] = -1.0
                A_ub[ev_row, ev_pen_offsets[ev_idx]] = -1.0
                b_ub[ev_row] = ev.initial_soc_kwh - ev.target_kwh
                ev_row += 1

            # EV target-cap constraint:
            # Σ_{k≤D} ev_c[k] ≤ target_kwh - initial_soc_kwh
            # Caps EV charging at the economic target for pre-deadline
            # slots.  Without this, the benefit coefficient on ev_c[t]
            # would drive charging all the way to capacity_kwh
            # regardless of the actual shortfall.
            # Does NOT apply when charge_past_target is enabled — that
            # mode intentionally allows charging beyond target_kwh via
            # a separate surplus-only mechanism.
            if (
                ev.deadline_slot is not None
                and ev.target_kwh > ev.initial_soc_kwh + 1e-9
                and not ev.charge_past_target
            ):
                shortfall = ev.target_kwh - ev.initial_soc_kwh
                d = ev.deadline_slot
                d = max(0, min(d, m - 1))
                for k in range(d + 1):
                    A_ub[ev_row, ev_off + k] = 1.0
                b_ub[ev_row] = shortfall
                ev_row += 1

            # Post-deadline zero-charge constraint:
            # For EVs with a deadline and no charge-past-target,
            # ev_c[t] = 0 for all t > deadline_slot.
            # This prevents the MILP from charging after the deadline
            # unless charge_past_target is enabled (which uses surplus PV).
            if (
                ev.deadline_slot is not None
                and ev.target_kwh > ev.initial_soc_kwh + 1e-9
                and not ev.charge_past_target
            ):
                d = ev.deadline_slot
                d = max(0, min(d, m - 1))
                for t in range(d + 1, m):
                    A_ub[ev_row, ev_off + t] = 1.0
                    b_ub[ev_row] = 0.0
                    ev_row += 1

            # Surplus-only constraint for charge-past-target EVs:
            # ev_c[t] / charger_eff ≤ max(0, pv[t] - base_load[t])
            # This ensures past-target charging ONLY uses genuine PV
            # surplus — never battery discharge or grid import.
            if ev.charge_past_target:
                for t in range(m):
                    surplus_kwh = max(pv_avail[t] - base_load[t], 0.0)
                    A_ub[ev_row + t, ev_off + t] = 1.0 / ev.charger_efficiency
                    b_ub[ev_row + t] = surplus_kwh
                ev_row += m

    # ------------------------------------------------------------------
    # Session EV grid-charge prevention (issue #615).
    # For session slots, battery grid-charging is blocked: the battery
    # may only charge from PV surplus remaining after the fixed EV
    # session load is met.
    #   ec[t] / charge_eff  ≤ max(0, pv_avail[t] - total_session_ac[t])
    # ------------------------------------------------------------------
    session_rows = len(session_slots_set) if _has_session_demand else 0
    if session_rows > 0:
        # Compute per-slot total AC-side session EV load
        session_ac_by_slot: dict[int, float] = {}
        for ev_idx in session_ev_indices:
            ev = active_evs[ev_idx]
            skw = ev.session_charge_kw
            assert skw is not None
            session_dc = skw * slot_hours * ev.charger_efficiency
            session_ac = session_dc / ev.charger_efficiency
            for t in session_slots_set:
                session_ac_by_slot[t] = session_ac_by_slot.get(t, 0.0) + session_ac

        session_t_list = sorted(session_slots_set)
        existing_rows = soc_rows + mutex_rows + cycle_rows + ev_total_rows
        A_ub_old = A_ub
        b_ub_old = b_ub
        A_ub = np.zeros((existing_rows + session_rows, n_vars))
        b_ub = np.zeros(existing_rows + session_rows)
        A_ub[:existing_rows, :] = A_ub_old
        b_ub[:existing_rows] = b_ub_old
        for row, t in enumerate(session_t_list):
            A_ub[existing_rows + row, ec_off + t] = 1.0 / charge_eff
            b_ub[existing_rows + row] = max(
                pv_avail[t] - session_ac_by_slot.get(t, 0.0), 0.0
            )
        ev_total_rows += session_rows

    # ------------------------------------------------------------------
    # Fuse constraint (soft): gi[t] - gi_pen[t] ≤ max_grid_import_per_slot_kwh
    # The penalty variable gi_pen[t] absorbs any excess at high cost,
    # preventing infeasibility when house base load alone exceeds the fuse.
    # ------------------------------------------------------------------
    fuse_rows = m if fuse_active else 0
    if fuse_active:
        existing_rows = soc_rows + mutex_rows + cycle_rows + ev_total_rows
        A_ub_old = A_ub
        b_ub_old = b_ub
        A_ub = np.zeros((existing_rows + fuse_rows, n_vars))
        b_ub = np.zeros(existing_rows + fuse_rows)
        A_ub[:existing_rows, :] = A_ub_old
        b_ub[:existing_rows] = b_ub_old
        for t in range(m):
            A_ub[existing_rows + t, gi_off + t] = 1.0
            A_ub[existing_rows + t, gi_pen_off + t] = -1.0
            b_ub[existing_rows + t] = max_grid_import_per_slot_kwh

    # ------------------------------------------------------------------
    # Variable bounds: all ≥ 0, charge/discharge capped by power limits.
    # Penalty variables are unbounded above (can absorb arbitrary
    # violations) and non-negative (violations cannot be negative).
    # ------------------------------------------------------------------
    bounds: list[tuple[float, float | None]] = (
        [(0.0, max_charge_per_slot)] * m  # ec[t]
        + [(0.0, float(ed_ub_per_slot[t])) for t in range(m)]  # ed[t]
        + [(0.0, None)] * m  # gi[t] (unbounded above)
        + [(0.0, None)] * m  # ge[t] (unbounded above)
        + [
            (pv_avail[t], pv_avail[t]) for t in range(m)
        ]  # pv[t] fixed to actual surplus
        + [(0.0, None)] * m  # m[t] (auxiliary, unbounded above, ≥ 0)
        + [(0.0, None)] * m  # s_max_pen[t] (penalty, ≥ 0)
        + [(0.0, None)] * m  # s_min_pen[t] (penalty, ≥ 0)
        + [(0.0, None)] * m  # curt[t] (curtailment, ≥ 0)
    )
    # --- EV bounds ---
    for ev_idx, ev in enumerate(active_evs):
        is_session_ev = ev_idx in session_ev_indices
        for t in range(m):
            if is_session_ev and t < session_slots and ev.session_charge_kw is not None:
                # Fixed bound: session demand (DC-side kWh per slot)
                session_dc = ev.session_charge_kw * slot_hours * ev.charger_efficiency
                session_dc = min(session_dc, ev.max_charge_per_slot)
                bounds.append((session_dc, session_dc))
            else:
                bounds.append((0.0, ev.max_charge_per_slot))
        # ev deadline penalty: [0, unbounded)
        bounds.append((0.0, None))
    # --- Fuse penalty bounds ---
    if fuse_active:
        bounds += [(0.0, None)] * m  # gi_pen[t] (penalty, ≥ 0)

    return {
        "A_eq": A_eq,
        "b_eq": b_eq,
        "A_ub": A_ub,
        "b_ub": b_ub,
        "bounds": bounds,
        "ev_discharge_guard_active": ev_discharge_guard_active,
        "ed_ub_per_slot": ed_ub_per_slot,
    }

"""Build MILP constraints and variable bounds.

Extracted from ``solve_milp`` so the orchestrator remains under 30 KB.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from custom_components.hsem.planner.milp._bounds import build_bounds
from custom_components.hsem.planner.milp._layout import (
    MilpColumnLayout,
    build_milp_column_layout,
)

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
    no_export: bool,
    session_slots_set: set[int],
    session_ev_indices: list[int],
    session_slots: int,
    slot_hours: float,
    _has_session_demand: bool,
    max_grid_export_per_slot_kwh: float = 0.0,
    export_limit_active: bool = False,
    battery_export_blocked: np.ndarray | None = None,  # type: ignore[name-defined]
    battery_export_off: int = 0,
    export_mode_off: int = 0,
    excess_export_discharge_buffer_pct: float = 0.0,
    grid_flow_mode_off: int = 0,
    grid_import_ub_per_slot: np.ndarray | None = None,  # type: ignore[name-defined]
    grid_export_ub_per_slot: np.ndarray | None = None,  # type: ignore[name-defined]
    session_slot_hours: np.ndarray | None = None,  # type: ignore[name-defined]
    available_slot_hours: np.ndarray | None = None,  # type: ignore[name-defined]
    column_layout: MilpColumnLayout | None = None,
    phase_fuse_active: bool = False,
    max_phase_import_per_slot_kwh: float = 0.0,
) -> dict:
    """Build all LP constraint matrices and variable bounds.

    Returns a dict with keys:
        ``A_eq``, ``b_eq``, ``A_ub``, ``b_ub``, ``bounds``,
        ``ev_discharge_guard_active``, ``ed_ub_per_slot``.
    """
    import numpy as np

    if session_slot_hours is None:
        session_slot_hours = np.full(m, slot_hours)
    if available_slot_hours is None:
        available_slot_hours = np.full(m, slot_hours)
    if column_layout is None:
        column_layout = build_milp_column_layout(
            m,
            len(active_evs),
            fuse_active=fuse_active,
        )

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
        if max_dis > 1e-9:
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
    # EV discharge guard (issue #592): when base_load_includes_ev=True and
    # EV co-optimisation is NOT active, the EV load is already captured in
    # avg_house_consumption_kwh.  The battery must not discharge to cover
    # it — without this cap the live-injected house consumption makes the
    # MILP discharge the home battery into the EV.
    #   ed[t] ≤ max(0, base_load[t] − ev_accounted[t]) / η_dis
    # The formula is exact, not over-conservative: with H = gross house
    # consumption (incl. EV) and P = PV, base_load = max(H−P, 0), and
    # max(base_load − ev, 0) == max(H − ev − P, 0) in all cases.
    # With no_export=True the cap extends to ALL slots (battery never
    # exports).  battery_export_blocked[t] (issue #752) applies the same
    # cap only to slots whose RAW export price is strictly below the
    # user's ``battery_export_min_price``, so only intentional
    # battery-to-grid export is blocked; house-load service continues.
    # ------------------------------------------------------------------
    ev_discharge_guard_active = (not active_evs) and bool(np.any(ev_accounted > 1e-9))
    if battery_export_blocked is None:
        battery_export_blocked = np.zeros(len(base_load), dtype=bool)
    ed_ub_per_slot: list[float] = []
    for t in range(m):
        # Per-slot cap: battery must not discharge more than what's needed
        # to cover house load (minus EV-accounted load when applicable).
        # When no_export=True, all slots get this cap.  Otherwise, only
        # EV-accounted slots and battery_export_blocked slots get it.
        cap_house_load = base_load[t] / discharge_eff
        if ev_discharge_guard_active and ev_accounted[t] > 1e-9:
            cap_house_load = max(base_load[t] - ev_accounted[t], 0.0) / discharge_eff
        if (
            no_export
            or bool(battery_export_blocked[t])
            or (ev_discharge_guard_active and ev_accounted[t] > 1e-9)
        ):
            ed_ub_per_slot.append(min(cap_house_load, max_dis))
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
    # Battery-first rows (issue #775): for charge-past-target EVs, the EV may
    # only absorb PV surplus that the house battery cannot take.  One shared
    # row per slot: ec[t] + Σ ev_c[t]/eff ≤ max(0, pv[t] - base_load[t]).
    ev_battery_first_rows = sum(1 for ev in active_evs if ev.charge_past_target) * m
    ev_total_rows = (
        ev_soc_rows
        + ev_deadline_rows
        + ev_target_rows
        + ev_post_deadline_rows
        + ev_surplus_rows
        + ev_battery_first_rows
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
        # Index of the first charge-past-target EV (into active_evs).  The
        # battery-first row is shared across all such EVs, so it is emitted
        # once, by this EV (issue #775).
        first_past_target_ev = next(
            (i for i, e in enumerate(active_evs) if e.charge_past_target), None
        )
        session_evs = set(session_ev_indices)

        def _pinned_session_dc(ev_idx: int, ev: EVConfig, t: int) -> float | None:
            """Return DC energy a live session pins into slot *t*, else ``None``.

            Mirrors the session branch of the ``ev_*_charge`` bounds exactly,
            including its ``max_charge_per_slot`` clamp, so rows and bounds
            always agree on how much energy is already committed.
            """
            if (
                ev_idx not in session_evs
                or t >= session_slots
                or ev.session_charge_kw is None
            ):
                return None
            return min(
                max(ev.session_charge_kw, 0.0)
                * float(session_slot_hours[t])
                * ev.charger_efficiency,
                ev.max_charge_per_slot,
            )

        for ev_idx, ev in enumerate(active_evs):
            ev_off = ev_var_offsets[ev_idx]
            # EV SOC upper bound per slot: Σ_{k≤t} ev_c[k] ≤ cap − init
            #   For each t in 0..m-1:
            #   Σ_{k=0..t} ev_c[k] ≤ ev.capacity_kwh - ev.initial_soc_kwh
            # Slots already pinned by a live session are fixed by their bounds,
            # so they are moved to the right-hand side instead of staying as
            # free columns.  The clamp at zero keeps the model feasible when a
            # session already commits more than the remaining headroom (an EV
            # near full that is still drawing power); leaving the row negative
            # would make the whole solve infeasible.
            headroom = max(ev.capacity_kwh - ev.initial_soc_kwh, 0.0)
            for t in range(m):
                fixed_session_dc = 0.0
                for k in range(t + 1):
                    pinned = _pinned_session_dc(ev_idx, ev, k)
                    if pinned is None:
                        A_ub[ev_row + t, ev_off + k] = 1.0
                    else:
                        fixed_session_dc += pinned
                b_ub[ev_row + t] = max(headroom - fixed_session_dc, 0.0)
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
                fixed_session_dc = 0.0
                for k in range(d + 1):
                    pinned = _pinned_session_dc(ev_idx, ev, k)
                    if pinned is None:
                        A_ub[ev_row, ev_off + k] = 1.0
                    else:
                        fixed_session_dc += pinned
                b_ub[ev_row] = max(shortfall - fixed_session_dc, 0.0)
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
                    # A session-pinned slot cannot be forced to zero; its
                    # energy is already committed by the bounds.  Skipping the
                    # coefficient keeps the row count stable while avoiding an
                    # infeasible 0 ≤ 0 conflict against a fixed positive value.
                    if _pinned_session_dc(ev_idx, ev, t) is None:
                        A_ub[ev_row, ev_off + t] = 1.0
                    b_ub[ev_row] = 0.0
                    ev_row += 1

            # Surplus-only constraint for charge-past-target EVs:
            # ev_c[t] / charger_eff ≤ max(0, pv[t] - base_load[t])
            # This ensures past-target charging ONLY uses genuine PV
            # surplus — never battery discharge or grid import.
            if ev.charge_past_target:
                for t in range(m):
                    # Session-pinned slots are uncontrollable demand, not
                    # past-target charging, so the surplus-only rule does not
                    # apply to them and would otherwise be infeasible whenever
                    # a live session runs without forecast PV surplus.
                    if _pinned_session_dc(ev_idx, ev, t) is None:
                        surplus_kwh = max(pv_avail[t] - base_load[t], 0.0)
                        A_ub[ev_row + t, ev_off + t] = 1.0 / ev.charger_efficiency
                        b_ub[ev_row + t] = surplus_kwh
                ev_row += m

            # Battery-first constraint for charge-past-target EVs (issue #775):
            #   ec[t] + Σ_ev ev_c[t] / charger_eff ≤ max(0, pv[t] - base_load[t])
            # The house battery must take its share of the slot's PV surplus
            # BEFORE the EV absorbs any.  Without this, a charge-past-target
            # EV valued at its avoided-future-import cost (issue #630) can
            # outrank the battery's charge credit and divert surplus PV that
            # the battery needs for its scheduled discharge window — the EV
            # and battery then oscillate for the same surplus across replans.
            #
            # The row is shared across all charge-past-target EVs (the battery
            # is a single resource), so it is only emitted for the first such
            # EV; every charge-past-target EV's ev_c[t] contributes to it.
            # Pre-deadline (below-target) EVs are deliberately excluded — they
            # keep their deadline benefit and may charge ahead of the battery.
            if ev.charge_past_target and ev_idx == first_past_target_ev:
                for t in range(m):
                    surplus_kwh = max(pv_avail[t] - base_load[t], 0.0)
                    A_ub[ev_row + t, ec_off + t] = 1.0
                    for other_idx, other in enumerate(active_evs):
                        if other.charge_past_target:
                            A_ub[
                                ev_row + t,
                                ev_var_offsets[other_idx] + t,
                            ] = 1.0 / other.charger_efficiency
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
            # AC-side session load per slot (kW × hours).  The DC/AC
            # efficiency conversion cancels out by definition, so this is
            # simply the AC power multiplied by the slot duration.
            for t in session_slots_set:
                session_ac = skw * float(session_slot_hours[t])
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
    hard_grid_import_cap_per_slot_kwh: np.ndarray | None = None
    fuse_rows = 2 * m if fuse_active else 0
    if fuse_active:
        existing_rows = soc_rows + mutex_rows + cycle_rows + ev_total_rows
        A_ub_old = A_ub
        b_ub_old = b_ub
        A_ub = np.zeros((existing_rows + fuse_rows, n_vars))
        b_ub = np.zeros(existing_rows + fuse_rows)
        A_ub[:existing_rows, :] = A_ub_old
        b_ub[:existing_rows] = b_ub_old
        session_evs = set(session_ev_indices)
        hard_grid_import_cap_per_slot_kwh = np.zeros(m)
        for t in range(m):
            # Soft diagnostic row.
            A_ub[existing_rows + t, gi_off + t] = 1.0
            A_ub[existing_rows + t, gi_pen_off + t] = -1.0
            b_ub[existing_rows + t] = max_grid_import_per_slot_kwh

            # Hard no-worsening row: controllable charging may not increase
            # an unavoidable house/live-session overload.  The baseline is
            # net of forecast PV — omitting PV would inflate the cap on
            # exactly the sunny slots where surplus-charging pressure is
            # highest (see planner-spec.md, grid import power limit).
            fixed_session_ac = sum(
                max(ev.session_charge_kw or 0.0, 0.0) * float(session_slot_hours[t])
                for ev_idx, ev in enumerate(active_evs)
                if ev_idx in session_evs and t in session_slots_set
            )
            fixed_site_import = max(
                float(base_load[t]) - float(pv_avail[t]) + fixed_session_ac, 0.0
            )
            A_ub[existing_rows + m + t, gi_off + t] = 1.0
            hard_cap = max(max_grid_import_per_slot_kwh, fixed_site_import)
            b_ub[existing_rows + m + t] = hard_cap
            hard_grid_import_cap_per_slot_kwh[t] = hard_cap

    # Hard per-phase fuse rows (EV charger phase topology): see
    # _phase_fuse.extend_with_phase_fuse_rows for the row model.
    if phase_fuse_active and active_evs:
        from custom_components.hsem.planner.milp._phase_fuse import (
            extend_with_phase_fuse_rows,
        )

        A_ub, b_ub = extend_with_phase_fuse_rows(
            A_ub=A_ub,
            b_ub=b_ub,
            m=m,
            column_layout=column_layout,
            active_evs=active_evs,
            session_slots_set=session_slots_set,
            session_slot_hours=session_slot_hours,
            slot_hours=slot_hours,
            available_slot_hours=available_slot_hours,
            max_phase_import_per_slot_kwh=max_phase_import_per_slot_kwh,
        )

    # ------------------------------------------------------------------
    # Exact grid direction under signed prices.
    # ------------------------------------------------------------------
    if grid_import_ub_per_slot is None:
        ev_import_capacity = sum(
            ev.max_charge_per_slot / max(ev.charger_efficiency, 0.01)
            for ev in active_evs
        )
        grid_import_ub_per_slot = (
            base_load + max_charge_per_slot / charge_eff + ev_import_capacity
        )
    if grid_export_ub_per_slot is None:
        grid_export_ub_per_slot = pv_avail + max_dis * discharge_eff
    old_rows = A_ub.shape[0]
    direction_a = np.zeros((old_rows + 2 * m, n_vars))
    direction_b = np.zeros(old_rows + 2 * m)
    direction_a[:old_rows, :] = A_ub
    direction_b[:old_rows] = b_ub
    for t in range(m):
        gi_cap = max(float(grid_import_ub_per_slot[t]), 0.0)
        ge_cap = max(float(grid_export_ub_per_slot[t]), 0.0)
        # z=1 permits import; z=0 permits export.
        direction_a[old_rows + t, gi_off + t] = 1.0
        direction_a[old_rows + t, grid_flow_mode_off + t] = -gi_cap
        direction_a[old_rows + m + t, ge_off + t] = 1.0
        direction_a[old_rows + m + t, grid_flow_mode_off + t] = ge_cap
        direction_b[old_rows + m + t] = ge_cap
    A_ub = direction_a
    b_ub = direction_b

    # ------------------------------------------------------------------
    # Battery-origin export source split and conditional reserve.
    # bx[t] is battery-side DC export; z_export[t] is binary activation.
    # Direct PV export remains available independently through ge[t].
    # ------------------------------------------------------------------
    from custom_components.hsem.planner.milp._export_reserve import (
        _next_solar_refill_checkpoints,
    )

    reserve_pct = max(min(excess_export_discharge_buffer_pct, 100.0), 0.0)
    reserve_active = not no_export and reserve_pct > 1e-9 and usable_kwh > 1e-9
    checkpoints = _next_solar_refill_checkpoints(pv_avail)
    source_rows = 3 * m + (m if reserve_active else 0)
    reserve_rows = m if reserve_active else 0
    old_rows = A_ub.shape[0]
    extended_a = np.zeros((old_rows + source_rows + reserve_rows, n_vars))
    extended_b = np.zeros(old_rows + source_rows + reserve_rows)
    extended_a[:old_rows, :] = A_ub
    extended_b[:old_rows] = b_ub
    A_ub = extended_a
    b_ub = extended_b

    row = old_rows
    for t in range(m):
        # Battery export cannot exceed total battery discharge.
        A_ub[row, battery_export_off + t] = 1.0
        A_ub[row, ed_off + t] = -1.0
        row += 1
        # Aggregate export cannot exceed direct PV surplus plus battery AC export.
        A_ub[row, ge_off + t] = 1.0
        A_ub[row, battery_export_off + t] = -discharge_eff
        b_ub[row] = pv_avail[t]
        row += 1
        # Every primary discharge must serve local load or declared battery
        # export; it may not manufacture room by curtailing PV.
        A_ub[row, ed_off + t] = discharge_eff
        A_ub[row, battery_export_off + t] = -discharge_eff
        b_ub[row] = base_load[t]
        row += 1
        if reserve_active:
            # Material battery export activates the binary mode.
            A_ub[row, battery_export_off + t] = 1.0
            A_ub[row, export_mode_off + t] = -max_dis
            row += 1

    if reserve_active:
        buffer_kwh = usable_kwh * reserve_pct / 100.0
        for t in range(m):
            checkpoint = int(checkpoints[t])
            # SoC[checkpoint] >= buffer - usable*(1-z[t])
            # -> -Σec + Σed + usable*z <= current + usable - buffer
            for k in range(checkpoint + 1):
                A_ub[row, ec_off + k] = -1.0
                A_ub[row, ed_off + k] = 1.0
            A_ub[row, export_mode_off + t] = usable_kwh
            b_ub[row] = current_kwh + usable_kwh - buffer_kwh
            row += 1

    # ------------------------------------------------------------------
    # Variable bounds: all ≥ 0, charge/discharge capped by power limits.
    # Penalty variables are unbounded above (can absorb arbitrary
    # violations) and non-negative (violations cannot be negative).
    # ------------------------------------------------------------------
    bounds = build_bounds(
        m=m,
        column_layout=column_layout,
        active_evs=active_evs,
        session_ev_indices=session_ev_indices,
        session_slots=session_slots,
        session_slot_hours=session_slot_hours,
        available_slot_hours=available_slot_hours,
        slot_hours=slot_hours,
        pv_avail=pv_avail,
        max_charge_per_slot=max_charge_per_slot,
        max_dis=max_dis,
        ed_ub_per_slot=ed_ub_per_slot,
        grid_import_ub_per_slot=grid_import_ub_per_slot,
        grid_export_ub_per_slot=grid_export_ub_per_slot,
        current_kwh=current_kwh,
        usable_kwh=usable_kwh,
        no_export=no_export,
        reserve_active=reserve_active,
        fuse_active=fuse_active,
    )

    return {
        "A_eq": A_eq,
        "b_eq": b_eq,
        "A_ub": A_ub,
        "b_ub": b_ub,
        "bounds": bounds,
        "bounds_blocks": column_layout.blocks,
        "ev_discharge_guard_active": ev_discharge_guard_active,
        "ed_ub_per_slot": ed_ub_per_slot,
        "hard_grid_import_cap_per_slot_kwh": hard_grid_import_cap_per_slot_kwh,
        "battery_export_reserve_active": reserve_active,
        "export_reserve_checkpoints": checkpoints,
    }

"""EV and EV-session MILP constraint rows.

Extracted from ``_constraints.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: row math and ordering are unchanged, only
the ``existing_rows`` bookkeeping now reads ``A_ub.shape[0]`` instead of a
separately-threaded ``soc_rows + mutex_rows + cycle_rows`` sum — the two are
always equal at the call site, since ``A_ub`` is initialised with exactly
that many rows before this function runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from custom_components.hsem.planner.milp._ev_amp_lattice import (
    target_cap_activation_quantum_dc,
)

if TYPE_CHECKING:
    from custom_components.hsem.models.ev_config import EVConfig


def add_ev_and_session_constraint_rows(
    *,
    A_ub: np.ndarray,  # type: ignore[name-defined]
    b_ub: np.ndarray,  # type: ignore[name-defined]
    m: int,
    ec_off: int,
    ev_var_offsets: list[int],
    ev_pen_offsets: list[int],
    active_evs: list[EVConfig],
    pv_avail: np.ndarray,  # type: ignore[name-defined]
    base_load: np.ndarray,  # type: ignore[name-defined]
    available_slot_hours: np.ndarray,  # type: ignore[name-defined]
    session_dc_by_ev: dict[int, dict[int, float]],
    session_ev_indices: list[int],
    session_slots_set: set[int],
    charge_eff: float,
    _has_session_demand: bool,
) -> tuple[np.ndarray, np.ndarray, int]:  # type: ignore[name-defined]
    """Append EV charging rows and session grid-charge-prevention rows.

    Returns the (possibly row-extended) ``A_ub``, ``b_ub``, and the total
    number of rows appended by this function (EV rows + session rows).
    """
    n_vars = A_ub.shape[1]

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
        existing_rows = A_ub.shape[0]
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

        def _pinned_session_dc(ev_idx: int, ev: EVConfig, t: int) -> float | None:
            """Return DC energy a live session pins into slot *t*, else ``None``.

            ``session_dc_by_ev`` is computed once per EV in ``milp_optimizer.py``
            (bounded by control authority — issue #789), so rows and bounds
            always agree on how much energy is already committed.
            """
            return session_dc_by_ev.get(ev_idx, {}).get(t)

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
            # initial_soc + Σ_{k≤D} ev_c[k] + penalty ≥ effective_target
            # → -Σ_{k≤D} ev_c[k] - penalty ≤ initial_soc - effective_target
            # ``effective_deadline_target_kwh`` is target_kwh plus a
            # configured safety margin (issue #845), so the plan aims past
            # the bare target and has slack to absorb execution-layer
            # friction (anti-flap windows, min-power floors, phase-headroom
            # throttling) without silently missing the deadline.
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
                b_ub[ev_row] = ev.initial_soc_kwh - ev.effective_deadline_target_kwh
                ev_row += 1

            # EV target-cap constraint:
            # Σ_{k≤D} ev_c[k] ≤ cap_target - initial_soc_kwh + activation_quantum
            # Caps EV charging near the economic target (plus safety margin)
            # for pre-deadline slots.  Without this, the benefit coefficient
            # on ev_c[t] would drive charging all the way to capacity_kwh
            # regardless of the actual shortfall.  One activation quantum
            # (the smallest executable whole-amp energy) is permitted above
            # the exact target: whole-amp hardware may have no executable
            # point exactly at the remaining need, and a strict cap would
            # report an avoidable deadline miss (issue #797).
            # Does NOT apply when charge_past_target is enabled — that
            # mode intentionally allows charging beyond target_kwh via
            # a separate surplus-only mechanism.
            # When ``deadline_escalated`` is True — even max-power charging
            # for every remaining pre-deadline slot can't reach the margined
            # target — the cap is lifted all the way to capacity_kwh
            # (issue #845), so the solver isn't artificially blocked from
            # charging as much as physically possible once the safety
            # margin itself is no longer achievable.
            if (
                ev.deadline_slot is not None
                and ev.target_kwh > ev.initial_soc_kwh + 1e-9
                and not ev.charge_past_target
            ):
                cap_target = (
                    ev.capacity_kwh
                    if ev.deadline_escalated
                    else ev.effective_deadline_target_kwh
                )
                shortfall = cap_target - ev.initial_soc_kwh
                d = ev.deadline_slot
                d = max(0, min(d, m - 1))
                activation_quantum_dc = target_cap_activation_quantum_dc(
                    ev, d=d, available_slot_hours=available_slot_hours
                )
                fixed_session_dc = 0.0
                for k in range(d + 1):
                    pinned = _pinned_session_dc(ev_idx, ev, k)
                    if pinned is None:
                        A_ub[ev_row, ev_off + k] = 1.0
                    else:
                        fixed_session_dc += pinned
                b_ub[ev_row] = max(
                    shortfall - fixed_session_dc + activation_quantum_dc,
                    0.0,
                )
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
            fixed_dc = session_dc_by_ev.get(ev_idx)
            if fixed_dc is None:
                continue
            # AC-side session load: the fixed DC energy divided by charger
            # efficiency (bounded by control authority — issue #789).
            for t, dc in fixed_dc.items():
                session_ac = float(dc) / max(ev.charger_efficiency, 0.01)
                session_ac_by_slot[t] = session_ac_by_slot.get(t, 0.0) + session_ac

        session_t_list = sorted(session_slots_set)
        existing_rows = A_ub.shape[0]
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

    return A_ub, b_ub, ev_total_rows

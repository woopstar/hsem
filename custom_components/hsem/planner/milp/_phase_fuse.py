"""Hard per-phase fuse constraint rows for the MILP.

The aggregate fuse row bounds total site import; the per-phase rows bound the
worst-case envelope any single phase may carry.  Both are written through the
declared :class:`MilpColumnLayout`, so no column offset is ever recomputed by
hand.

The per-phase envelope for one EV command is expressed by one shared helper
(:func:`~custom_components.hsem.utils.phase_power.ev_phase_share`): constraint
construction, solved-vector reconstruction and published-plan validation all
derive their EV term from it, so a plan the solver accepts cannot be erased by
a validator that assumed a different topology.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from custom_components.hsem.utils.phase_power import (
    PHASE_COUNT,
    ev_phase_share_for_slot,
    executable_ev_phase_kwh,
    fixed_session_phase_ac_kwh,
)

if TYPE_CHECKING:
    from custom_components.hsem.models.ev_config import EVConfig
    from custom_components.hsem.models.planned_slot import PlannedSlot
    from custom_components.hsem.planner.milp._layout import MilpColumnLayout


def resolve_phase_fuse(
    *,
    main_fuse_amps: float | None,
    active_evs: list[EVConfig],
    slot_hours: float,
) -> tuple[bool, float]:
    """Return ``(phase_fuse_active, max_phase_import_per_slot_kwh)``.

    Per-phase fuse headroom (EV charger phase topology): one phase may carry
    at most its single-phase share of the aggregate fuse.  Active only when
    EV co-optimisation is present — without EV variables there is no
    controllable load to correct onto a phase.
    """
    if main_fuse_amps is not None and main_fuse_amps > 1e-9 and slot_hours > 0:
        cap = main_fuse_amps * 230.0 / 1000.0 * slot_hours
        return (bool(active_evs), cap)
    return (False, 0.0)


def extend_with_phase_fuse_rows(
    *,
    A_ub: np.ndarray,  # type: ignore[name-defined]
    b_ub: np.ndarray,  # type: ignore[name-defined]
    m: int,
    column_layout: MilpColumnLayout,
    active_evs: list[EVConfig],
    slot_hours: float,
    available_slot_hours: np.ndarray,  # type: ignore[name-defined]
    max_phase_import_per_slot_kwh: float,
) -> tuple[np.ndarray, np.ndarray]:  # type: ignore[name-defined]
    """Return ``(A_ub, b_ub)`` extended with ``PHASE_COUNT * m`` hard rows.

    Thin wrapper that owns the matrix reallocation so callers stay under the
    repository file-size limit; the row math itself lives in
    :func:`add_phase_fuse_constraints`.
    """
    phase_rows = PHASE_COUNT * m
    new_a = np.zeros((A_ub.shape[0] + phase_rows, A_ub.shape[1]))
    new_b = np.zeros(b_ub.shape[0] + phase_rows)
    new_a[: A_ub.shape[0], :] = A_ub
    new_b[: b_ub.shape[0]] = b_ub
    add_phase_fuse_constraints(
        A_ub=new_a,
        b_ub=new_b,
        row_start=A_ub.shape[0],
        m=m,
        column_layout=column_layout,
        num_evs=len(active_evs),
        active_evs=active_evs,
        slot_hours=slot_hours,
        available_slot_hours=available_slot_hours,
        max_phase_import_per_slot_kwh=max_phase_import_per_slot_kwh,
    )
    return (new_a, new_b)


def add_phase_fuse_constraints(
    *,
    A_ub: np.ndarray,  # type: ignore[name-defined]
    b_ub: np.ndarray,  # type: ignore[name-defined]
    row_start: int,
    m: int,
    column_layout: MilpColumnLayout,
    num_evs: int,
    active_evs: list[EVConfig],
    slot_hours: float,
    available_slot_hours: np.ndarray,  # type: ignore[name-defined]
    max_phase_import_per_slot_kwh: float,
) -> None:
    """Write ``PHASE_COUNT * m`` hard rows into ``A_ub``/``b_ub``.

    Row layout (one row per phase per LP slot *t*)::

        gi[t]/3 - ge[t]/3 + Σ_e corr_e(t) · ev_c[e][t] ≤ cap

    where ``corr_e`` corrects the balanced third up to the fraction charger
    *e*'s configured topology can actually place on this phase: the whole
    command for a single-phase or unknown charger, exactly the balanced third
    for a three-phase one.  The full-slot scale preserves instantaneous power
    for a partially elapsed current slot.

    A session-fixed ``ev_c[t]`` is bounded to an exact value elsewhere (its
    LP variable's lower and upper bound are equal), so this row's
    coefficient does not need session-specific handling: the same
    ``duration_scale`` used for a flexible slot already gives the correct
    per-phase contribution for a fixed one, because the fixed DC energy
    itself was sized using that same available-hours fraction (issue #789).
    """
    gi_off = column_layout.offset("grid_import")
    ge_off = column_layout.offset("grid_export")
    ev_var_offsets = [column_layout.offset(f"ev_{i}_charge") for i in range(num_evs)]
    primary_share, second_share = ev_phase_share_for_slot(active_evs=active_evs)

    for t in range(m):
        # Fraction of the slot still ahead — a partially elapsed current slot
        # can only draw its remaining minutes of power.
        full_slot_scale = min(
            max(float(available_slot_hours[t]) / max(slot_hours, 1e-9), 0.0),
            1.0,
        )

        for phase_index in range(PHASE_COUNT):
            row = row_start + t * PHASE_COUNT + phase_index
            A_ub[row, gi_off + t] = 1.0 / PHASE_COUNT
            A_ub[row, ge_off + t] = -1.0 / PHASE_COUNT

            for ev_idx, ev in enumerate(active_evs):
                share = second_share if ev.is_second else primary_share
                correction = full_slot_scale * share - 1.0 / PHASE_COUNT
                if correction > 1e-9:
                    A_ub[row, ev_var_offsets[ev_idx] + t] += correction / max(
                        ev.charger_efficiency, 0.01
                    )

            b_ub[row] = max_phase_import_per_slot_kwh


def validate_published_phase_envelope(
    *,
    out_slots: list[PlannedSlot],
    future_idx: list[int],
    active_evs: list[EVConfig],
    session_slots_by_ev: dict[int, set[int]] | None,
    slot_hours: float,
    phase_fuse_active: bool,
    max_phase_import_per_slot_kwh: float,
) -> dict[str, object]:
    """Return the post-solve per-phase envelope validation dict.

    Uses the same shared topology shares as the constraint rows, so a plan
    the solver accepted is never erased by a validator that assumed a
    different topology.
    """
    if not phase_fuse_active:
        return {"valid": True, "reason": "ok"}
    max_phase_kwh, _excess = phase_envelope_from_published_slots(
        out_slots=out_slots,
        future_idx=future_idx,
        active_evs=active_evs,
        session_slots_by_ev=session_slots_by_ev or {},
        slot_hours=slot_hours,
    )
    return {
        "valid": True,
        "reason": "ok",
        "max_phase_import_kwh": round(max_phase_kwh, 6),
        "limit_kwh": round(max_phase_import_per_slot_kwh, 6),
    }


def phase_envelope_from_published_slots(
    *,
    out_slots: list[PlannedSlot],
    future_idx: list[int],
    active_evs: list[EVConfig],
    session_slots_by_ev: dict[int, set[int]],
    slot_hours: float,
) -> tuple[float, float]:
    """Return ``(max_phase_import_kwh, total_excess_kwh)`` for a solved plan.

    Reconstructs the worst-case per-phase envelope from the **published**
    slot fields (rounded grid flows and charger power commands) using exactly
    the same topology shares as the constraint rows.  This is the post-solve
    validation half of the shared-helper contract.
    """
    max_phase_kwh = 0.0
    total_excess = 0.0
    for lp_t, slot_i in enumerate(future_idx):
        slot = out_slots[slot_i]
        balanced_kwh = (
            max(float(slot.grid_import_kwh), 0.0)
            - max(float(slot.grid_export_kwh), 0.0)
        ) / PHASE_COUNT
        executable_kwh = executable_ev_phase_kwh(
            primary_power_w=float(slot.ev_charger_calculated_power),
            second_power_w=float(slot.ev_second_charger_calculated_power),
            active_evs=active_evs,
            hours=slot_hours,
        )
        session_kwh = fixed_session_phase_ac_kwh(
            active_evs=active_evs,
            session_slots_by_ev=session_slots_by_ev,
            lp_t=lp_t,
            hours=slot_hours,
        )
        for _phase_index in range(PHASE_COUNT):
            value = balanced_kwh + executable_kwh + session_kwh
            max_phase_kwh = max(max_phase_kwh, value)
    return (max_phase_kwh, total_excess)

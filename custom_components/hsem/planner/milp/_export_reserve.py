"""Conditional primary-battery export reserve checkpoint helpers."""

from __future__ import annotations

from typing import Any

import numpy as np

_EPSILON_KWH = 1e-9


def _next_solar_refill_checkpoints(pv_avail: np.ndarray) -> np.ndarray:
    """Return one later reserve checkpoint per slot.

    The checkpoint is immediately before the next distinct forecast PV-surplus
    run, or the horizon end. Every slot in one contiguous surplus run shares
    the checkpoint derived from that run's final slot.
    """
    m = len(pv_avail)
    checkpoints = np.zeros(m, dtype=int)
    next_surplus: int | None = None
    for t in range(m - 1, -1, -1):
        # Resolve against a strictly later surplus slot; the current slot is
        # registered only after its checkpoint has been assigned.
        checkpoints[t] = m - 1 if next_surplus is None else max(t, next_surplus - 1)
        if float(pv_avail[t]) > _EPSILON_KWH:
            next_surplus = t

    run_start: int | None = None
    for t in range(m + 1):
        is_surplus = t < m and float(pv_avail[t]) > _EPSILON_KWH
        if is_surplus and run_start is None:
            run_start = t
        elif not is_surplus and run_start is not None:
            run_end = t - 1
            checkpoints[run_start:t] = checkpoints[run_end]
            run_start = None

    return checkpoints


def _add_battery_export_reserve_constraints(
    constraints: dict[str, Any],
    m: int,
    n_vars: int,
    ec_off: int,
    ed_off: int,
    export_mode_off: int,
    usable_kwh: float,
    current_kwh: float,
    discharge_eff: float,
    checkpoints: np.ndarray | None,
    reserve_kwh: float,
    immediate_reserve_kwh: float = 0.0,
) -> dict[str, Any]:
    """Append conditional battery-export reserve constraints.

    The export-mode binary is forced to one whenever battery-origin grid
    export is positive. The checkpoint reserve may be restored before the
    following demand-window checkpoint; the immediate reserve must remain
    at the end of the exporting slot itself.

    Both reserves are conditional on intentional battery export. Ordinary
    self-consumption may use the full battery, and direct PV export does
    not activate either reserve.
    """
    old_a_ub = constraints["A_ub"]
    old_b_ub = constraints["b_ub"]
    old_rows = old_a_ub.shape[0]

    checkpoint_active = reserve_kwh > _EPSILON_KWH
    immediate_active = immediate_reserve_kwh > _EPSILON_KWH
    reserve_blocks = int(checkpoint_active) + int(immediate_active)
    added_rows = reserve_blocks * m
    a_ub = np.zeros((old_rows + added_rows, n_vars))
    b_ub = np.zeros(old_rows + added_rows)
    a_ub[:old_rows, : old_a_ub.shape[1]] = old_a_ub
    b_ub[:old_rows] = old_b_ub

    # Big-M values are physical battery bounds, not arbitrary constants.
    soc_big_m_kwh = max(usable_kwh, _EPSILON_KWH)
    if checkpoint_active:
        assert checkpoints is not None

    row = old_rows
    for t in range(m):
        if checkpoint_active:
            # A later refill may restore the checkpoint buffer.
            assert checkpoints is not None
            checkpoint = int(checkpoints[t])
            for k in range(checkpoint + 1):
                a_ub[row, ec_off + k] = -1.0
                a_ub[row, ed_off + k] = 1.0
            a_ub[row, export_mode_off + t] = soc_big_m_kwh
            b_ub[row] = current_kwh + soc_big_m_kwh - reserve_kwh
            row += 1

        if immediate_active:
            # A later PV/grid refill cannot justify spending this reserve now.
            for k in range(t + 1):
                a_ub[row, ec_off + k] = -1.0
                a_ub[row, ed_off + k] = 1.0
            a_ub[row, export_mode_off + t] = soc_big_m_kwh
            b_ub[row] = current_kwh + soc_big_m_kwh - immediate_reserve_kwh
            row += 1

    constraints["A_ub"] = a_ub
    constraints["b_ub"] = b_ub
    return constraints

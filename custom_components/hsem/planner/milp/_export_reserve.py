"""Conditional primary-battery export reserve checkpoint helpers."""

from __future__ import annotations

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

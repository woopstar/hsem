"""Battery-export reserve diagnostics for the MILP result.

Extracted from ``milp_optimizer.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: no behaviour change.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def attach_export_reserve_diagnostics(
    diagnostics: dict[str, Any],
    constraints: dict[str, Any],
    *,
    m: int,
    export_mode_off: int,
    solution: np.ndarray,  # type: ignore[name-defined]
    ec_sol: np.ndarray,  # type: ignore[name-defined]
    ed_sol: np.ndarray,  # type: ignore[name-defined]
    current_kwh: float,
) -> None:
    """Record whether the export reserve was active and its tightest checkpoint.

    The reserve is conditional, so the reported minimum is taken only over slots
    where the solver actually activated battery-origin export.
    """
    diagnostics["battery_export_reserve_active"] = bool(
        constraints.get("battery_export_reserve_active", False)
    )
    checkpoints = constraints.get("export_reserve_checkpoints")
    if not diagnostics["battery_export_reserve_active"] or checkpoints is None:
        return

    active_soc: list[float] = []
    for t in range(m):
        if solution[export_mode_off + t] < 0.5:
            continue
        checkpoint = int(checkpoints[t])
        active_soc.append(
            current_kwh
            + float(np.sum(ec_sol[: checkpoint + 1]) - np.sum(ed_sol[: checkpoint + 1]))
        )
    diagnostics["battery_export_reserve_min_checkpoint_soc_kwh"] = (
        round(min(active_soc), 6) if active_soc else None
    )

"""Battery-export reserve diagnostics for the MILP result.

Extracted from ``milp_optimizer.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: no behaviour change.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from custom_components.hsem.utils.logger import log_planner


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
    export_reserve_active = bool(
        constraints.get("battery_export_reserve_active", False)
    )
    export_forecast_reserve_active = bool(
        constraints.get("battery_export_forecast_reserve_active", False)
    )
    forecast_reserve_kwh = float(
        constraints.get("battery_export_forecast_reserve_kwh", 0.0)
    )

    diagnostics["battery_export_reserve_active"] = export_reserve_active
    diagnostics["battery_export_reserve_slots"] = 0
    diagnostics["battery_export_reserve_min_checkpoint_soc_kwh"] = None
    diagnostics["battery_export_forecast_reserve_active"] = (
        export_forecast_reserve_active
    )
    diagnostics["battery_export_forecast_reserve_kwh"] = round(forecast_reserve_kwh, 6)
    diagnostics["battery_export_forecast_reserve_slots"] = 0
    diagnostics["battery_export_forecast_reserve_min_post_export_soc_kwh"] = None

    checkpoints = constraints.get("export_reserve_checkpoints")

    export_slots: list[int] = []
    soc_after = current_kwh + np.cumsum(ec_sol - ed_sol)
    if export_mode_off is not None:
        export_mode_sol = solution[export_mode_off : export_mode_off + m]
        export_slots = [t for t, value in enumerate(export_mode_sol) if value > 0.5]

    if export_reserve_active:
        assert checkpoints is not None
        active_soc: list[float] = []
        for t in range(m):
            if solution[export_mode_off + t] < 0.5:
                continue
            checkpoint = int(checkpoints[t])
            active_soc.append(
                current_kwh
                + float(
                    np.sum(ec_sol[: checkpoint + 1]) - np.sum(ed_sol[: checkpoint + 1])
                )
            )
        min_checkpoint_soc = min(active_soc) if active_soc else None
        diagnostics["battery_export_reserve_min_checkpoint_soc_kwh"] = (
            round(min_checkpoint_soc, 6) if min_checkpoint_soc is not None else None
        )
        diagnostics["battery_export_reserve_slots"] = len(export_slots)
        log_planner(
            "debug",
            "[milp] battery_export_reserve  buffer=%.3f  checkpoints=%s  "
            "export_slots=%d  min_checkpoint_soc=%s",
            constraints.get("export_reserve_kwh", 0.0),
            checkpoints.tolist() if checkpoints is not None else [],
            len(export_slots),
            (f"{min_checkpoint_soc:.3f}" if min_checkpoint_soc is not None else "n/a"),
        )

    if export_forecast_reserve_active:
        post_export_soc = [float(soc_after[t]) for t in export_slots]
        min_post_export_soc = min(post_export_soc) if post_export_soc else None
        diagnostics["battery_export_forecast_reserve_slots"] = len(export_slots)
        diagnostics["battery_export_forecast_reserve_min_post_export_soc_kwh"] = (
            round(min_post_export_soc, 6) if min_post_export_soc is not None else None
        )
        log_planner(
            "debug",
            "[milp] battery_export_forecast_reserve  reserve=%.3f  "
            "export_slots=%d  min_post_export_soc=%s",
            forecast_reserve_kwh,
            len(export_slots),
            (
                f"{min_post_export_soc:.3f}"
                if min_post_export_soc is not None
                else "n/a"
            ),
        )

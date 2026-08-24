"""Variable-bounds assembly for the MILP model.

Extracted from ``_constraints.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: every bound is written through the declared
:class:`MilpColumnLayout` exactly as before.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import numpy as np

from custom_components.hsem.planner.milp._layout import (
    Bound,
    MilpBoundsBuilder,
    MilpColumnLayout,
)

if TYPE_CHECKING:
    from custom_components.hsem.models.ev_config import EVConfig
    from custom_components.hsem.planner.milp._ev_amp_lattice import EvAmpPlan


def build_bounds(
    *,
    m: int,
    column_layout: MilpColumnLayout,
    active_evs: list[EVConfig],
    session_ev_indices: list[int],
    session_dc_by_ev: dict[int, dict[int, float]],
    available_slot_hours: np.ndarray,  # type: ignore[name-defined]
    slot_hours: float,
    pv_avail: np.ndarray,  # type: ignore[name-defined]
    max_charge_per_slot: float,
    max_dis: float,
    ed_ub_per_slot: Sequence[float] | np.ndarray,  # type: ignore[name-defined]
    grid_import_ub_per_slot: np.ndarray,  # type: ignore[name-defined]
    grid_export_ub_per_slot: np.ndarray,  # type: ignore[name-defined]
    current_kwh: float,
    usable_kwh: float,
    no_export: bool,
    reserve_active: bool,
    fuse_active: bool,
    ev_amp_plan: EvAmpPlan | None = None,
) -> list[Bound]:
    """Return the complete, validated solver bounds vector."""
    unbounded: tuple[float, float | None] = (0.0, None)
    bounds_builder = MilpBoundsBuilder(column_layout)
    bounds_builder.fill("battery_charge", (0.0, max_charge_per_slot))
    bounds_builder.set(
        "battery_discharge",
        [(0.0, float(ed_ub_per_slot[t])) for t in range(m)],
    )
    bounds_builder.set(
        "grid_import",
        [(0.0, max(float(grid_import_ub_per_slot[t]), 0.0)) for t in range(m)],
    )
    bounds_builder.set(
        "grid_export",
        [(0.0, max(float(grid_export_ub_per_slot[t]), 0.0)) for t in range(m)],
    )
    bounds_builder.set(
        "pv",
        [(float(pv_avail[t]), float(pv_avail[t])) for t in range(m)],
    )
    bounds_builder.fill("primary_throughput", unbounded)
    bounds_builder.fill(
        "soc_max_penalty",
        (0.0, max(float(current_kwh - usable_kwh), 0.0)),
    )
    bounds_builder.fill(
        "soc_min_penalty",
        (0.0, max(float(-current_kwh), 0.0)),
    )
    bounds_builder.set(
        "curtailment",
        [(0.0, float(pv_avail[t])) for t in range(m)],
    )
    bounds_builder.set(
        "primary_battery_export",
        [((0.0, 0.0) if no_export else (0.0, max_dis)) for _t in range(m)],
    )
    bounds_builder.set(
        "battery_export_mode",
        [((0.0, 1.0) if reserve_active else (0.0, 0.0)) for _t in range(m)],
    )
    bounds_builder.set(
        "grid_flow_mode",
        [(0.0, 1.0) for _t in range(m)],
    )

    for ev_idx, ev in enumerate(active_evs):
        is_session_ev = ev_idx in session_ev_indices
        fixed_dc = session_dc_by_ev.get(ev_idx)
        ev_bounds: list[tuple[float, float | None]] = []
        for t in range(m):
            if is_session_ev and fixed_dc is not None and t in fixed_dc:
                session_dc = float(fixed_dc[t])
                ev_bounds.append((session_dc, session_dc))
            elif ev.fixed_session_only:
                ev_bounds.append((0.0, 0.0))
            else:
                # A partly elapsed current slot can only deliver its remaining
                # minutes of charge, so the flexible ceiling is scaled down
                # accordingly.  Without this the optimiser reserves a full
                # slot's energy that the charger cannot physically deliver.
                duration_scale = min(
                    max(float(available_slot_hours[t]) / max(slot_hours, 1e-9), 0.0),
                    1.0,
                )
                ev_bounds.append((0.0, ev.max_charge_per_slot * duration_scale))
        bounds_builder.set(f"ev_{ev_idx}_charge", ev_bounds)
        bounds_builder.fill(
            f"ev_{ev_idx}_target_penalty",
            unbounded,
        )
    if fuse_active:
        bounds_builder.fill("grid_import_penalty", unbounded)

    if ev_amp_plan is not None:
        from custom_components.hsem.planner.milp._ev_amp_lattice import (
            write_ev_amp_bounds,
        )

        write_ev_amp_bounds(bounds_builder, ev_amp_plan, m=m)

    return bounds_builder.finalize()

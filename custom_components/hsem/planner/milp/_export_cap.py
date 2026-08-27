"""Resolve the grid export power cap for the MILP (issue #726).

Extracted from ``solve_milp`` so the orchestrator remains under 30 KB.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.units import (
    export_max_energy_per_slot_kwh,
    timedelta_to_hours,
)

if TYPE_CHECKING:
    from custom_components.hsem.models.ev_config import EVConfig
    from custom_components.hsem.models.planned_slot import PlannedSlot


def _resolve_export_cap(
    max_grid_export_power_kw: float | None,
    slots: list[PlannedSlot],
    future_idx: list[int],
) -> tuple[bool, float]:
    """Resolve the per-slot grid export cap for the MILP.

    Unlike the main fuse (soft, penalty-based), the export cap is a hard
    physical limit enforced by the inverter/DNO — the LP must never plan
    export above it.  Battery export and PV export compete for the same cap
    via the energy-balance equality, so the optimum front-loads battery
    export into low-PV slots and tapers it as PV ramps.

    Args:
        max_grid_export_power_kw: Configured export cap in kW (None/0 =
            disabled).
        slots: Full slot list passed to ``solve_milp``.
        future_idx: Indices of future (active LP) slots into *slots*.

    Returns:
        ``(active, max_kwh_per_slot)`` — *active* is True when the cap is
        configured, in which case *max_kwh_per_slot* is the hard upper bound
        for the ``ge[t]`` variable (0.0 when disabled).
    """
    if max_grid_export_power_kw is None or max_grid_export_power_kw <= 1e-9:
        return False, 0.0

    first_slot = slots[future_idx[0]]
    slot_hours = timedelta_to_hours(first_slot.end - first_slot.start)
    max_kwh = export_max_energy_per_slot_kwh(max_grid_export_power_kw, slot_hours)
    log_planner(
        "debug",
        "[milp] Grid export cap active: %.2f kW → max %.3f kWh/slot "
        "(interval=%.0f min)",
        max_grid_export_power_kw,
        max_kwh,
        slot_hours * 60.0,
    )
    return True, max_kwh


def resolve_grid_bounds(
    *,
    active_evs: list[EVConfig],
    base_load: np.ndarray,  # type: ignore[name-defined]
    pv_avail: np.ndarray,  # type: ignore[name-defined]
    max_charge_per_slot: float,
    charge_eff: float,
    max_dis: float,
    discharge_eff: float,
    max_grid_export_power_kw: float | None,
    slots: list[PlannedSlot],
    future_idx: list[int],
) -> tuple[np.ndarray, np.ndarray, bool, float]:  # type: ignore[name-defined]
    """Resolve finite grid import/export bounds that close both signed-price directions.

    Returns ``(grid_import_ub_per_slot, grid_export_ub_per_slot,
    export_limit_active, max_grid_export_per_slot_kwh)``.
    """
    ev_import_capacity = sum(
        ev.max_charge_per_slot / max(ev.charger_efficiency, 0.01) for ev in active_evs
    )
    grid_import_ub_per_slot = (
        base_load + max_charge_per_slot / charge_eff + ev_import_capacity
    )
    grid_export_ub_per_slot = pv_avail + max_dis * discharge_eff

    # Grid export power cap (issue #726): hard per-slot bound on ge[t].
    export_limit_active, max_grid_export_per_slot_kwh = _resolve_export_cap(
        max_grid_export_power_kw, slots, future_idx
    )
    if export_limit_active:
        grid_export_ub_per_slot = np.minimum(
            grid_export_ub_per_slot,
            max_grid_export_per_slot_kwh,
        )

    return (
        grid_import_ub_per_slot,
        grid_export_ub_per_slot,
        export_limit_active,
        max_grid_export_per_slot_kwh,
    )

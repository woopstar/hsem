"""Aggregate main-fuse limit resolution for the MILP.

Extracted from ``milp_optimizer.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: no behaviour change.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.units import (
    fuse_max_energy_per_slot_kwh,
    timedelta_to_hours,
)


def resolve_fuse_variables(
    *,
    fuse_active: bool,
    main_fuse_amps: float | None,
    main_fuse_phases: int,
    column_layout: object,
    active_evs: list,
    slot_hours: float,
    first_slot: object,
) -> tuple[int, float, bool, float]:
    """Resolve aggregate and per-phase fuse variables for one MILP model.

    Returns ``(gi_pen_off, max_grid_import_per_slot_kwh, phase_fuse_active,
    max_phase_import_per_slot_kwh)``.
    """
    from custom_components.hsem.planner.milp._phase_fuse import resolve_phase_fuse

    if fuse_active:
        gi_pen_off = column_layout.offset("grid_import_penalty")  # type: ignore[attr-defined]
        max_grid = resolve_fuse_import_limit_kwh(
            main_fuse_amps=main_fuse_amps,
            main_fuse_phases=main_fuse_phases,
            slot_start=first_slot.start,  # type: ignore[attr-defined]
            slot_end=first_slot.end,  # type: ignore[attr-defined]
        )
        phase_active, phase_cap = resolve_phase_fuse(
            main_fuse_amps=main_fuse_amps,
            active_evs=active_evs,
            slot_hours=slot_hours,
        )
        return (gi_pen_off, max_grid, phase_active, phase_cap)
    return (0, 0.0, False, 0.0)


def resolve_fuse_import_limit_kwh(
    *,
    main_fuse_amps: float | None,
    main_fuse_phases: int,
    slot_start: datetime,
    slot_end: datetime,
) -> float:
    """Return the max grid import per slot in kWh for the configured fuse.

    Single source of truth shared with the post-hoc EV/battery throttle in
    ``engine_core``. The slot interval is derived from the first future slot.
    """
    assert main_fuse_amps is not None  # guarded by the caller's fuse_active
    interval_minutes = timedelta_to_hours(slot_end - slot_start) * 60.0
    limit = fuse_max_energy_per_slot_kwh(
        main_fuse_amps,
        main_fuse_phases,
        interval_minutes / 60.0,
    )
    log_planner(
        "debug",
        "[milp] Main fuse constraint active: %d A × %d-phase → max %.3f kWh/slot "
        "(interval=%.0f min)",
        main_fuse_amps,
        main_fuse_phases,
        limit,
        interval_minutes,
    )
    return limit

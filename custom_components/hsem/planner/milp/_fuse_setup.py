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

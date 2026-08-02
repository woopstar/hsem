"""Dataclass for one charge/discharge schedule window configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time


@dataclass
class BatteryScheduleInput:
    """Configuration for one charge-into/discharge-from schedule window.

    Mirrors the user-visible battery schedule options from the config flow
    (``batteries_schedule_1/2/3``).
    """

    enabled: bool = False
    start: time = time(0, 0)
    end: time = time(1, 0)

    # Runtime attributes populated by the discharge scheduler and consumed by
    # the charge scheduler.  Declared here so type checkers see them instead of
    # requiring dynamic attribute workarounds.
    _occurrences: list[tuple[datetime, datetime, float, float]] = field(
        default_factory=list, repr=False
    )
    _needed_capacity: float = field(default=0.0, repr=False)
    _avg_import_price: float = field(default=0.0, repr=False)

"""Dataclass for one charge/discharge schedule window configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass
class BatteryScheduleInput:
    """Configuration for one charge-into/discharge-from schedule window.

    Mirrors the user-visible battery schedule options from the config flow
    (``batteries_schedule_1/2/3``).
    """

    enabled: bool = False
    start: time = time(0, 0)
    end: time = time(1, 0)

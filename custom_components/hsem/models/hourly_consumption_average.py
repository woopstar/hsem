"""Dataclass for historical consumption averages for one clock-hour.

All values are in kWh for the full hour.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HourlyConsumptionAverage:
    """Historical consumption averages for one clock-hour.

    All values are in kWh for the full hour.

    Attributes:
        hour:
            Wall-clock hour of the day (0-23).
        day_offset:
            Number of whole calendar days from the planning midnight (0 = today,
            1 = tomorrow, …).  Defaults to 0 for backward compatibility with
            callers that only pass 24 single-day entries.
    """

    hour: int  # 0-23
    avg_1d: float = 0.0
    avg_3d: float = 0.0
    avg_7d: float = 0.0
    avg_14d: float = 0.0
    day_offset: int = 0

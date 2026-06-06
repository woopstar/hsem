"""Dataclass for a forecast PV production estimate for a single time slot."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SolcastSlot:
    """Forecast PV production estimate for a single time slot.

    Attributes:
        hour:
            0-based calendar hour (0-23).
        pv_estimate:
            PV energy estimate in kWh for the full slot duration.
        day_offset:
            Number of whole calendar days from the planning midnight (0 = today,
            1 = tomorrow, …).  Defaults to 0 for backward compatibility with
            callers that only pass 24 single-day entries.
    """

    hour: int  # 0-23
    pv_estimate: float = 0.0
    day_offset: int = 0

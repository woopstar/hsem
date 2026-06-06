"""Dataclass for a contiguous block of slots assigned to battery discharging."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class DischargeWindow:
    """A contiguous block of slots assigned to battery discharging.

    Attributes:
        start:
            Start of the first discharging slot.
        end:
            End of the last discharging slot.
        avg_import_price:
            Mean import price across all slots (proxy for discharge value).
        recommendation:
            The recommendation value that marks these slots (typically
            ``"batteries_discharge_mode"`` or
            ``"force_batteries_discharge"``).
    """

    start: datetime
    end: datetime
    avg_import_price: float = 0.0
    recommendation: str = ""

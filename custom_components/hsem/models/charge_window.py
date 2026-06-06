"""Dataclass for a contiguous block of slots assigned to battery charging."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class ChargeWindow:
    """A contiguous block of slots assigned to battery charging.

    Groups consecutive :class:`PlannedSlot` entries that share the same
    charge recommendation so tests can reason about charge windows at a
    higher level of abstraction.

    Attributes:
        start:
            Start of the first charging slot.
        end:
            End of the last charging slot.
        total_energy_kwh:
            Total energy scheduled to be charged during this window.
        avg_import_price:
            Mean import price across all slots in the window.
        recommendation:
            The recommendation value that marks these slots (typically
            ``"batteries_charge_grid"`` or ``"batteries_charge_solar"``).
    """

    start: datetime
    end: datetime
    total_energy_kwh: float = 0.0
    avg_import_price: float = 0.0
    recommendation: str = ""

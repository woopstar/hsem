"""Dataclass for an import or export electricity price for a single time slot."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PricePoint:
    """An import or export electricity price for a single time slot.

    Attributes:
        hour:
            0-based calendar hour (0-23).
        import_price:
            Import price in local currency/kWh (e.g. DKK/kWh).
        export_price:
            Export price in local currency/kWh.
        day_offset:
            Number of whole calendar days from the planning midnight (0 = today,
            1 = tomorrow, …).  Defaults to 0 for backward compatibility with
            callers that only pass 24 single-day entries.
    """

    hour: int  # 0-23
    import_price: float = 0.0
    export_price: float = 0.0
    day_offset: int = 0

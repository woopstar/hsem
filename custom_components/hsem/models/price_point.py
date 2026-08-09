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
        slot_in_day:
            Optional 0-based index of the sub-hourly slot within its calendar
            day (0-95 for 15-min slots, 0-47 for 30-min, 0-23 for 60-min).
            ``None`` (default) means the point is hour-granular — existing
            hourly callers are unaffected.  When set, the planner keys the
            point by ``(day_offset, slot_in_day)`` so quarter-hourly prices
            (e.g. Nord Pool 15-min MTUs) survive to the MILP instead of being
            collapsed to one price per hour (issue #720).
    """

    hour: int  # 0-23
    import_price: float = 0.0
    export_price: float = 0.0
    day_offset: int = 0
    slot_in_day: int | None = None

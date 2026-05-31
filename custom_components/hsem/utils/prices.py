"""Hourly-to-slot price expansion utilities for HSEM (issue #287).

This module provides a pure-Python function that expands hourly import and
export price data into per-slot lists aligned to the shared
:class:`~custom_components.hsem.models.time_series.TimeSeriesIndex` grid.

Design goals
------------
- **No Home Assistant imports** — usable in plain unit tests.
- **Negative prices supported** — export prices may be negative (grid pays
  you to consume) and import prices may be negative during surplus periods.
- **Missing hours handled safely** — slots for absent hours receive
  ``float("nan")`` so callers can detect and handle gaps explicitly rather
  than silently defaulting to zero.
- **Currency / VAT transparent** — prices are passed through unchanged;
  this module applies no currency conversion or VAT transformation.
- **15-minute default resolution** — uses
  :data:`~custom_components.hsem.models.time_series.DEFAULT_SLOT_MINUTES`.

Public API
----------
``SlotPrice``
    Lightweight named-tuple carrying one slot's import and export price.
``expand_hourly_prices_to_slots``
    Expand ``{hour: price}`` dicts into a parallel ``list[SlotPrice]``.

Usage example
-------------
>>> from zoneinfo import ZoneInfo
>>> from datetime import datetime
>>> from custom_components.hsem.utils.prices import expand_hourly_prices_to_slots
>>>
>>> tz = ZoneInfo("Europe/Copenhagen")
>>> now = datetime(2024, 6, 15, 0, 0, tzinfo=tz)
>>> import_prices = {h: 0.10 + h * 0.01 for h in range(24)}
>>> export_prices = {h: max(import_prices[h] - 0.02, 0.0) for h in range(24)}
>>> slots = expand_hourly_prices_to_slots(now, import_prices, export_prices)
>>> len(slots)  # 24 * 4 = 96 slots for 15-min resolution over 24 h
96
>>> slots[0].import_price  # hour 0 import price
0.1
>>> import math; math.isnan(slots[0].import_price) if len(slots) > 0 else True
False
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import NamedTuple

from custom_components.hsem.models.time_series import (
    DEFAULT_SLOT_MINUTES,
    TimeSeriesIndex,
)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class SlotPrice(NamedTuple):
    """Import and export price for a single planning slot.

    Prices are in the caller's local currency per kWh (e.g. DKK/kWh).
    A value of :data:`~custom_components.hsem.models.time_series.MISSING_SENTINEL`
    (``float("nan")``) indicates that no price data was available for the
    slot's hour.

    Attributes:
        import_price:
            Price to import one kWh from the grid.  May be negative.
        export_price:
            Price received for exporting one kWh to the grid.  May be
            negative (curtailment penalty).
    """

    import_price: float
    export_price: float

    @property
    def is_missing_import(self) -> bool:
        """Return ``True`` if import price data is absent for this slot.

        Uses :func:`math.isnan` for an explicit IEEE 754 NaN check.
        """
        return math.isnan(self.import_price)

    @property
    def is_missing_export(self) -> bool:
        """Return ``True`` if export price data is absent for this slot.

        Uses :func:`math.isnan` for an explicit IEEE 754 NaN check.
        """
        return math.isnan(self.export_price)

    @property
    def has_any_missing(self) -> bool:
        """Return ``True`` if either import or export price is missing."""
        return self.is_missing_import or self.is_missing_export


# ---------------------------------------------------------------------------
# Core expansion function
# ---------------------------------------------------------------------------


def expand_hourly_prices_to_slots(
    now: datetime,
    import_prices: dict[int, float],
    export_prices: dict[int, float],
    *,
    interval_minutes: int = DEFAULT_SLOT_MINUTES,
    horizon_hours: int = 24,
) -> list[SlotPrice]:
    """Expand hourly import and export prices into per-slot price lists.

    Each price is broadcast to *all* slots that fall within the same
    wall-clock hour.  For 15-minute slots this means four consecutive slots
    share the same price.  Prices are **not** scaled or divided — electricity
    prices are a rate (currency/kWh), not an energy quantity, so every slot
    in the same hour receives exactly the same value.

    Missing hours are filled with
    :data:`~custom_components.hsem.models.time_series.MISSING_SENTINEL`
    (``float("nan")``) so callers can detect gaps explicitly.

    Args:
        now:
            Timezone-aware current datetime.  Slots start from midnight of
            *now*'s calendar day in *now*'s timezone.
        import_prices:
            Dict mapping wall-clock hour (0-23) to import price in local
            currency/kWh.  May contain negative values.  Hours absent from
            the dict produce :data:`MISSING_SENTINEL` slots.
        export_prices:
            Dict mapping wall-clock hour (0-23) to export price in local
            currency/kWh.  May contain negative values.  Hours absent from
            the dict produce :data:`MISSING_SENTINEL` slots.
        interval_minutes:
            Slot width in minutes.  Must be a positive divisor of 60
            (e.g. 15, 30, 60).  Defaults to
            :data:`~custom_components.hsem.models.time_series.DEFAULT_SLOT_MINUTES`
            (15).
        horizon_hours:
            Number of hours of slots to generate from midnight.  Defaults
            to 24.

    Returns:
        List of :class:`SlotPrice` objects in chronological order, one per
        slot, parallel to the :class:`TimeSeriesIndex` that was built
        internally.  The list length is
        ``(horizon_hours * 60) // interval_minutes``.

    Raises:
        ValueError:
            If *now* is naive, *interval_minutes* is not a positive divisor
            of 60, or *horizon_hours* ≤ 0.

    Examples:
        Expand 24 hours of prices to 96 15-minute slots::

            import_p = {h: 0.10 + h * 0.01 for h in range(24)}
            export_p = {h: import_p[h] - 0.02 for h in range(24)}
            slots = expand_hourly_prices_to_slots(now, import_p, export_p)
            # Hour 14 slots → indices 56-59 all have the same price
            assert slots[56].import_price == slots[59].import_price

        Negative export prices are passed through unchanged::

            export_p = {0: -0.05, 1: -0.03}
            slots = expand_hourly_prices_to_slots(now, {}, export_p)
            assert slots[0].export_price == -0.05

        Missing hours become NaN sentinels::

            import math
            slots = expand_hourly_prices_to_slots(now, {}, {})
            assert math.isnan(slots[0].import_price)
    """
    tsi = TimeSeriesIndex.from_now(
        now,
        interval_minutes=interval_minutes,
        horizon_hours=horizon_hours,
    )
    aligned_import, aligned_export = tsi.align_hourly_prices(
        import_prices, export_prices
    )

    return [
        SlotPrice(import_price=imp, export_price=exp)
        for imp, exp in zip(aligned_import, aligned_export)
    ]


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


def fill_missing_prices(
    slot_prices: list[SlotPrice],
    *,
    import_fallback: float = 0.0,
    export_fallback: float = 0.0,
) -> list[SlotPrice]:
    """Replace ``NaN`` sentinel prices with fallback values.

    This is a convenience function for callers that prefer a safe default
    over explicit ``NaN`` handling.  The original list is not mutated; a
    new list is returned.

    Args:
        slot_prices:
            List of :class:`SlotPrice` objects as returned by
            :func:`expand_hourly_prices_to_slots`.
        import_fallback:
            Value to substitute for missing import prices.  Defaults to
            ``0.0``.
        export_fallback:
            Value to substitute for missing export prices.  Defaults to
            ``0.0``.

    Returns:
        New list with all ``NaN`` values replaced by the supplied fallback.
    """
    return [
        SlotPrice(
            import_price=(
                import_fallback if math.isnan(sp.import_price) else sp.import_price
            ),
            export_price=(
                export_fallback if math.isnan(sp.export_price) else sp.export_price
            ),
        )
        for sp in slot_prices
    ]


def missing_price_hours(slot_prices: list[SlotPrice]) -> set[int]:
    """Return the set of wall-clock hours (0-23) that have missing price data.

    An hour is considered *missing* when **any** of its constituent slots
    carries a :data:`MISSING_SENTINEL` value for either import or export.

    Args:
        slot_prices:
            List of :class:`SlotPrice` objects.  The list length must be a
            multiple of ``slots_per_hour`` inferred from the list length and
            24 (i.e. the list should cover a whole number of hours starting
            at midnight).

    Returns:
        Set of integer hours (0-23) with incomplete price data.  Empty set
        if all prices are present.

    Note:
        This helper assumes the slot list starts at midnight and covers
        exactly ``N`` complete hours where ``N`` divides the list length
        evenly.  For sub-24-hour lists, only the covered hours are examined.
    """
    if not slot_prices:
        return set()

    # Infer slots_per_hour: try to find a divisor of len(slot_prices) that
    # also divides 60 evenly and is in the range [1, 4].
    n = len(slot_prices)
    slots_per_hour: int | None = None
    for candidate in (4, 2, 1):  # prefer finer granularity first (15, 30, 60 min)
        if n % candidate == 0:
            slots_per_hour = candidate
            break
    if slots_per_hour is None:
        # Fallback: treat every slot as its own "hour".
        slots_per_hour = 1

    missing: set[int] = set()
    for idx, sp in enumerate(slot_prices):
        if sp.has_any_missing:
            hour = (idx // slots_per_hour) % 24
            missing.add(hour)
    return missing

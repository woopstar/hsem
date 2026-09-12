"""``SlotPrice`` — per-slot import/export price pair for HSEM (issue #287).

Design goals
------------
- **No Home Assistant imports** — usable in plain unit tests.
- **Negative prices supported** — export prices may be negative (grid pays
  you to consume) and import prices may be negative during surplus periods.
- **Missing-price detection** — :data:`~custom_components.hsem.models.time_series.MISSING_SENTINEL`
  (``float("nan")``) indicates that no price data was available for the
  slot's hour, so callers can detect and handle gaps explicitly rather than
  silently defaulting to zero.

``expand_hourly_prices_to_slots``, ``fill_missing_prices``, and
``missing_price_hours`` — a convenience layer that expanded ``{hour: price}``
dicts into per-slot ``SlotPrice`` lists via a standalone
:class:`~custom_components.hsem.models.time_series.TimeSeriesIndex` — were
removed as dead code in issue #967. The actual planner integration
(``planner/slot_population.py::populate_prices``) calls
``TimeSeriesIndex.align_hourly_prices()`` directly on its own already-built
index instead, so this wrapper never had a production caller.
"""

from __future__ import annotations

from typing import NamedTuple

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

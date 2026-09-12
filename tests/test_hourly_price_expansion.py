"""Tests for the ``SlotPrice`` named-tuple (originally issue #287).

Acceptance criteria verified here
----------------------------------
- ``SlotPrice`` behaves as an immutable named-tuple of ``(import_price,
  export_price)``.

``expand_hourly_prices_to_slots``, ``fill_missing_prices``, and
``missing_price_hours`` (and their dedicated tests previously in this file)
were removed as dead code in issue #967: they were a convenience layer
around ``TimeSeriesIndex.align_hourly_prices()`` built alongside it in issue
#287, but the actual planner integration (``slot_population.py::populate_prices``)
ended up calling ``TimeSeriesIndex.align_hourly_prices()`` directly instead,
leaving this wrapper with zero production callers.
"""

from __future__ import annotations

import pytest

from custom_components.hsem.utils.prices import SlotPrice


class TestSlotPrice:
    """SlotPrice behaves as an immutable named-tuple."""

    def test_is_a_named_tuple(self):
        sp = SlotPrice(import_price=1.0, export_price=2.0)
        assert sp[0] == pytest.approx(1.0)
        assert sp[1] == pytest.approx(2.0)

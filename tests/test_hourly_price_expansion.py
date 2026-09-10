"""Tests for the ``SlotPrice`` named-tuple (originally issue #287).

Acceptance criteria verified here
----------------------------------
- ``SlotPrice`` named-tuple properties (``is_missing_import``, etc.) behave correctly.

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

from custom_components.hsem.models.time_series import MISSING_SENTINEL
from custom_components.hsem.utils.prices import SlotPrice


class TestSlotPrice:
    """SlotPrice behaves as an immutable named-tuple with helper properties."""

    def test_normal_slot_has_no_missing(self):
        sp = SlotPrice(import_price=0.25, export_price=0.10)
        assert not sp.is_missing_import
        assert not sp.is_missing_export
        assert not sp.has_any_missing

    def test_nan_import_is_missing(self):
        sp = SlotPrice(import_price=MISSING_SENTINEL, export_price=0.10)
        assert sp.is_missing_import
        assert not sp.is_missing_export
        assert sp.has_any_missing

    def test_nan_export_is_missing(self):
        sp = SlotPrice(import_price=0.25, export_price=MISSING_SENTINEL)
        assert not sp.is_missing_import
        assert sp.is_missing_export
        assert sp.has_any_missing

    def test_both_nan_is_missing(self):
        sp = SlotPrice(import_price=MISSING_SENTINEL, export_price=MISSING_SENTINEL)
        assert sp.is_missing_import
        assert sp.is_missing_export
        assert sp.has_any_missing

    def test_negative_prices_are_not_missing(self):
        sp = SlotPrice(import_price=-0.05, export_price=-0.10)
        assert not sp.is_missing_import
        assert not sp.is_missing_export
        assert not sp.has_any_missing

    def test_zero_prices_are_not_missing(self):
        sp = SlotPrice(import_price=0.0, export_price=0.0)
        assert not sp.has_any_missing

    def test_is_a_named_tuple(self):
        sp = SlotPrice(import_price=1.0, export_price=2.0)
        assert sp[0] == pytest.approx(1.0)
        assert sp[1] == pytest.approx(2.0)

"""Tests for :func:`ev_future_charge_value_per_kwh` (issue #630).

Verifies the avoided-future-import-cost valuation used to price EV
charge-past-target charging in the MILP objective, mirroring the pattern
already used for the house battery's terminal-SoC pricing
(:func:`replacement_price_from_next_discharge`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.candidate_selector import (
    ev_future_charge_value_per_kwh,
)
from custom_components.hsem.utils.prices import SlotPrice

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2024, 6, 15, 14, 0, tzinfo=_TZ)


def _slot(hours_from_now: float, import_price: float) -> PlannedSlot:
    start = _NOW + timedelta(hours=hours_from_now)
    return PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=import_price, export_price=0.0),
    )


class TestEvFutureChargeValuePerKwh:
    def test_averages_future_import_prices_within_lookahead(self):
        """Value equals confidence_factor * mean(import_price) over 24h."""
        slots = [_slot(h, 1.0) for h in range(1, 25)] + [_slot(30, 999.0)]

        value = ev_future_charge_value_per_kwh(
            slots, _NOW, lookahead_hours=24.0, confidence_factor=1.0
        )

        assert value == pytest.approx(1.0, rel=1e-6)

    def test_confidence_factor_discounts_the_estimate(self):
        """A confidence_factor < 1.0 scales the averaged price down."""
        slots = [_slot(h, 2.0) for h in range(1, 25)]

        value = ev_future_charge_value_per_kwh(
            slots, _NOW, lookahead_hours=24.0, confidence_factor=0.9
        )

        assert value == pytest.approx(1.8, rel=1e-6)

    def test_excludes_past_slots(self):
        """Slots at or before now must not contribute to the average."""
        slots = [_slot(-1, 100.0), _slot(0, 100.0), _slot(1, 2.0)]

        value = ev_future_charge_value_per_kwh(
            slots, _NOW, lookahead_hours=24.0, confidence_factor=1.0
        )

        assert value == pytest.approx(2.0, rel=1e-6)

    def test_excludes_slots_beyond_lookahead_window(self):
        """Slots starting after the lookahead cutoff are excluded."""
        slots = [_slot(1, 2.0), _slot(25, 999.0)]

        value = ev_future_charge_value_per_kwh(
            slots, _NOW, lookahead_hours=24.0, confidence_factor=1.0
        )

        assert value == pytest.approx(2.0, rel=1e-6)

    def test_excludes_nan_prices(self):
        """Slots with NaN import prices (missing data) are excluded."""
        slots = [_slot(1, float("nan")), _slot(2, 3.0)]

        value = ev_future_charge_value_per_kwh(
            slots, _NOW, lookahead_hours=24.0, confidence_factor=1.0
        )

        assert value == pytest.approx(3.0, rel=1e-6)

    def test_returns_none_when_no_future_price_data(self):
        """No future slots within the window returns None (caller falls back)."""
        slots = [_slot(-1, 1.0)]

        value = ev_future_charge_value_per_kwh(slots, _NOW)

        assert value is None

    def test_returns_none_for_empty_slot_list(self):
        assert ev_future_charge_value_per_kwh([], _NOW) is None

    def test_default_lookahead_and_confidence_factor(self):
        """Defaults are 24h lookahead and 0.9 confidence factor."""
        slots = [_slot(h, 1.0) for h in range(1, 25)]

        value = ev_future_charge_value_per_kwh(slots, _NOW)

        assert value == pytest.approx(0.9, rel=1e-6)

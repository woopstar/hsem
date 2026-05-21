"""Tests for concentrate_discharge_on_expensive_slots() in discharge_scheduler.py.

Regression test for #446: the original code used ``break`` when a slot
could not be fully served, causing all subsequent (cheaper) slots to be
skipped even if they had small enough demand to fit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.planner.discharge_scheduler import (
    concentrate_discharge_on_expensive_slots,
)
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import DISCHARGE_RECS, Recommendations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TZ = UTC
_NOW = datetime(2026, 5, 20, 10, 0, 0, tzinfo=TZ)


def _make_discharge_slot(
    demand_kwh: float,
    price: float,
    recommendation: str | None = Recommendations.BatteriesDischargeMode.value,
) -> PlannedSlot:
    """Create a single discharge-mode PlannedSlot."""
    start = _NOW + timedelta(hours=1)
    end = start + timedelta(hours=1)
    return PlannedSlot(
        start=start,
        end=end,
        price=SlotPrice(import_price=price, export_price=0.0),
        recommendation=recommendation,
        estimated_net_consumption_kwh=demand_kwh,
    )


def _count_discharge_slots(slots: list[PlannedSlot]) -> int:
    """Return how many slots still have a discharge recommendation."""
    return sum(1 for s in slots if s.recommendation in DISCHARGE_RECS)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConcentrateDischargeExpensiveSlots:
    """Unit tests for concentrate_discharge_on_expensive_slots()."""

    def test_slots_are_sorted_by_price_descending(self) -> None:
        """Slots with higher import price are preferred for discharge."""
        slots = [
            _make_discharge_slot(demand_kwh=1.0, price=0.20),
            _make_discharge_slot(demand_kwh=1.0, price=2.50),
            _make_discharge_slot(demand_kwh=1.0, price=0.10),
        ]
        concentrate_discharge_on_expensive_slots(
            slots,
            _NOW,
            current_kwh=0.0,
            usable_kwh=2.0,
            max_discharge_per_slot=None,
        )

        # The two most expensive slots (2.50, 0.20) should be kept
        kept = [s for s in slots if s.recommendation in DISCHARGE_RECS]
        # All three slots have same demand (1.0 kWh) and usable_kwh=2.0,
        # so only 2 of 3 fit. The two most expensive should be kept.
        assert len(kept) == 2
        kept_prices_sorted = sorted(s.price.import_price for s in kept)
        assert kept_prices_sorted[0] == pytest.approx(0.20)
        assert kept_prices_sorted[1] == pytest.approx(2.50)

    def test_no_discharge_slots_returns_early(self) -> None:
        """No discharge slots means no work done."""
        slots = [
            _make_discharge_slot(
                demand_kwh=1.0,
                price=0.20,
                recommendation=Recommendations.BatteriesWaitMode.value,
            ),
        ]
        concentrate_discharge_on_expensive_slots(
            slots,
            _NOW,
            current_kwh=0.0,
            usable_kwh=10.0,
            max_discharge_per_slot=None,
        )
        assert _count_discharge_slots(slots) == 0

    def test_all_slots_fit_keeps_all(self) -> None:
        """Battery capacity is sufficient for all discharge slots."""
        slots = [
            _make_discharge_slot(demand_kwh=1.0, price=0.30),
            _make_discharge_slot(demand_kwh=1.0, price=0.20),
        ]
        concentrate_discharge_on_expensive_slots(
            slots,
            _NOW,
            current_kwh=0.0,
            usable_kwh=2.0,
            max_discharge_per_slot=None,
        )
        assert _count_discharge_slots(slots) == 2

    def test_break_bug_regression_viable_cheap_slot_is_kept(self) -> None:
        """Regression test for #446: after a large slot exhausts most battery
        capacity, a subsequent small-demand slot should still be evaluated
        and kept if it fits.

        Battery: 5.0 kWh usable
        Slot A: demand=4.8, price=2.50 → kept, remaining = 0.2
        Slot B: demand=0.3, price=2.40 → skipped (0.3 > 0.2)
        Slot C: demand=0.15, price=2.10 → kept (0.15 <= 0.2)

        Before the fix (#446), Slot C was silently dropped because ``break``
        exited the loop when Slot B couldn't be served.
        """
        slots = [
            _make_discharge_slot(demand_kwh=4.8, price=2.50),
            _make_discharge_slot(demand_kwh=0.3, price=2.40),
            _make_discharge_slot(demand_kwh=0.15, price=2.10),
        ]
        concentrate_discharge_on_expensive_slots(
            slots,
            _NOW,
            current_kwh=0.0,
            usable_kwh=5.0,
            max_discharge_per_slot=None,
        )

        kept = [s for s in slots if s.recommendation in DISCHARGE_RECS]
        cleared = [
            s
            for s in slots
            if s.recommendation == Recommendations.BatteriesWaitMode.value
        ]

        # Slot A (2.50) and Slot C (2.10) must be kept
        assert len(kept) == 2, f"Expected 2 kept, got {len(kept)}"
        kept_prices = sorted([s.price.import_price for s in kept])
        assert kept_prices == pytest.approx([2.10, 2.50])

        # Slot B (2.40) must be cleared
        assert len(cleared) == 1, f"Expected 1 cleared, got {len(cleared)}"
        assert cleared[0].price.import_price == pytest.approx(2.40)

    def test_no_battery_capacity_clears_all(self) -> None:
        """When usable_kwh is 0.0, no discharge slots are kept."""
        slots = [
            _make_discharge_slot(demand_kwh=1.0, price=2.50),
            _make_discharge_slot(demand_kwh=1.0, price=2.00),
        ]
        concentrate_discharge_on_expensive_slots(
            slots,
            _NOW,
            current_kwh=0.0,
            usable_kwh=0.0,
            max_discharge_per_slot=None,
        )
        assert _count_discharge_slots(slots) == 0

    def test_zero_demand_slots(self) -> None:
        """Slots with zero demand should be kept (no battery cost)."""
        slots = [
            _make_discharge_slot(demand_kwh=0.0, price=2.50),
            _make_discharge_slot(demand_kwh=0.0, price=2.00),
        ]
        concentrate_discharge_on_expensive_slots(
            slots,
            _NOW,
            current_kwh=0.0,
            usable_kwh=1.0,
            max_discharge_per_slot=None,
        )
        assert _count_discharge_slots(slots) == 2

    def test_discharge_efficiency_reduces_battery_needed(self) -> None:
        """With <100% efficiency, more battery capacity is consumed per slot."""
        slots = [
            _make_discharge_slot(demand_kwh=5.0, price=2.50),
            _make_discharge_slot(demand_kwh=1.0, price=2.00),
        ]
        # 80% efficiency means battery_needed = 5.0 / 0.8 = 6.25 > 5.0
        # First slot doesn't fit, second shouldn't either.
        concentrate_discharge_on_expensive_slots(
            slots,
            _NOW,
            current_kwh=0.0,
            usable_kwh=5.0,
            discharge_efficiency_pct=80.0,
            max_discharge_per_slot=None,
        )
        # With 80% eff, 5.0/0.8 = 6.25 > 5.0 → skip slot A
        # total_battery_kwh stays 5.0, then 1.0/0.8 = 1.25, 1.25 <= 5.0 → keep slot B
        assert _count_discharge_slots(slots) == 1
        assert slots[1].recommendation in DISCHARGE_RECS
        # With 80% eff, 5.0/0.8 = 6.25 > 5.0 → skip slot A
        # total_battery_kwh stays 5.0, then 1.0/0.8 = 1.25, 1.25 <= 5.0 → keep slot B
        assert _count_discharge_slots(slots) == 1
        assert slots[1].recommendation in DISCHARGE_RECS

    def test_per_slot_power_limit_clamps_demand(self) -> None:
        """max_discharge_per_slot caps the battery consumption estimate."""
        slots = [
            _make_discharge_slot(demand_kwh=10.0, price=2.50),
            _make_discharge_slot(demand_kwh=10.0, price=2.00),
        ]
        # max 1.0 kWh per slot, so each slot effectively costs 1.0 kWh
        concentrate_discharge_on_expensive_slots(
            slots,
            _NOW,
            current_kwh=0.0,
            usable_kwh=1.5,
            max_discharge_per_slot=1.0,
        )
        # Slot A: 1.0 <= 1.5 → keep, remaining = 0.5
        # Slot B: 1.0 > 0.5 → skip (continue), but no more slots to check
        assert _count_discharge_slots(slots) == 1

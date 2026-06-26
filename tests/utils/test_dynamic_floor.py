"""Tests for DynamicDischargeFloor (issue #600).

Covers bridge computation, safety margin self-correction, edge cases,
and integration with various slot resolutions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from custom_components.hsem.utils.dynamic_floor import (
    DynamicDischargeFloor,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeSlot:
    """Minimal PlannedSlot stand-in for dynamic floor tests."""

    start: datetime
    end: datetime
    estimated_net_consumption_kwh: float = 0.0
    batteries_charged_kwh: float = 0.0
    recommendation: str | None = None


def _make_slots(
    now: datetime,
    net_kwh_values: list[float],
    slot_minutes: int = 60,
    charged_kwh: list[float] | None = None,
    recommendations: list[str | None] | None = None,
) -> list[_FakeSlot]:
    """Build a list of fake slots starting from *now*.

    Args:
        now: Start time reference.
        net_kwh_values: Net consumption per slot (negative = surplus).
        slot_minutes: Duration of each slot in minutes.
        charged_kwh: Batteries charged per slot (None → all zero).
        recommendations: Recommendation per slot (None → all None).
    """
    slots: list[_FakeSlot] = []
    for i, net in enumerate(net_kwh_values):
        start = now + timedelta(minutes=i * slot_minutes)
        end = start + timedelta(minutes=slot_minutes)
        chg = charged_kwh[i] if charged_kwh else 0.0
        rec = recommendations[i] if recommendations else None
        slots.append(
            _FakeSlot(
                start=start,
                end=end,
                estimated_net_consumption_kwh=net,
                batteries_charged_kwh=chg,
                recommendation=rec,
            )
        )
    return slots


# ---------------------------------------------------------------------------
# Bridge computation tests
# ---------------------------------------------------------------------------


class TestBridgeComputation:
    """Tests for DynamicDischargeFloor.compute_floor()."""

    def test_solar_refill_basic(self) -> None:
        """Reserve = consumption until first solar surplus slot."""
        now = datetime(2025, 6, 15, 12, 0)
        df = DynamicDischargeFloor()
        # 3 hours of consumption → solar surplus at hour 4
        slots = _make_slots(now, [0.5, 0.3, 0.4, -1.0, 0.2])
        floor_pct, diag = df.compute_floor(
            now=now,
            slots=slots,
            current_kwh=5.0,
            usable_kwh=10.0,
            configured_min_soc_pct=10.0,
        )
        # Reserve = (0.5 + 0.3 + 0.4) * 1.15 = 1.38 kWh
        # Reserve SoC = 1.38 / 10.0 * 100 = 13.8%
        assert floor_pct == pytest.approx(13.8, rel=1e-4)
        assert diag["refill_type"] == "solar_surplus"
        assert diag["reserve_kwh"] == pytest.approx(1.2, rel=1e-4)
        assert diag["bridge_duration_hours"] == pytest.approx(3.0, rel=1e-4)

    def test_grid_charge_refill(self) -> None:
        """Reserve = consumption until planned grid charge covers the need."""
        now = datetime(2025, 6, 15, 18, 0)
        df = DynamicDischargeFloor()
        # Evening consumption, then grid charge at slot 3 that covers the bridge.
        slots = _make_slots(
            now,
            [1.0, 0.8, 0.5, 0.3],
            slot_minutes=60,
            charged_kwh=[0.0, 0.0, 0.0, 3.0],
            recommendations=[None, None, None, "batteries_charge_grid"],
        )
        floor_pct, diag = df.compute_floor(
            now=now,
            slots=slots,
            current_kwh=5.0,
            usable_kwh=10.0,
            configured_min_soc_pct=10.0,
        )
        # Reserve = (1.0 + 0.8 + 0.5) - 3.0 (grid charge) = neg → 0
        # Since grid charge covers, refill is the charge slot
        assert diag["refill_type"] == "grid_charge"
        assert diag["reserve_kwh"] == 0.0

    def test_configured_min_is_absolute_floor(self) -> None:
        """Dynamic floor must be at least the configured minimum."""
        now = datetime(2025, 6, 15, 12, 0)
        df = DynamicDischargeFloor()
        # Very low consumption → computed floor would be below configured min.
        slots = _make_slots(now, [0.05, -1.0, 0.2])
        floor_pct, diag = df.compute_floor(
            now=now,
            slots=slots,
            current_kwh=5.0,
            usable_kwh=10.0,
            configured_min_soc_pct=15.0,
        )
        assert floor_pct == pytest.approx(15.0, rel=1e-4)

    def test_no_future_refill_uses_full_horizon(self) -> None:
        """When no refill is found, accumulate over full horizon."""
        now = datetime(2025, 6, 15, 22, 0)
        df = DynamicDischargeFloor()
        # All positive consumption, no solar surplus, no grid charge.
        slots = _make_slots(now, [0.5, 0.5, 0.5, 0.5])
        floor_pct, diag = df.compute_floor(
            now=now,
            slots=slots,
            current_kwh=5.0,
            usable_kwh=10.0,
            configured_min_soc_pct=5.0,
        )
        assert diag["refill_type"] == "none"
        assert diag["reserve_kwh"] == pytest.approx(2.0, rel=1e-4)
        assert floor_pct == pytest.approx(23.0, rel=1e-4)

    def test_empty_slots(self) -> None:
        """Empty slot list returns configured minimum."""
        now = datetime(2025, 6, 15, 12, 0)
        df = DynamicDischargeFloor()
        floor_pct, diag = df.compute_floor(
            now=now,
            slots=[],
            current_kwh=5.0,
            usable_kwh=10.0,
            configured_min_soc_pct=10.0,
        )
        assert floor_pct == pytest.approx(10.0, rel=1e-4)
        assert diag["refill_type"] == "none"

    def test_past_slots_skipped(self) -> None:
        """Slots in the past are ignored."""
        now = datetime(2025, 6, 15, 13, 0)
        df = DynamicDischargeFloor()
        base = datetime(2025, 6, 15, 12, 0)
        slots = [
            _FakeSlot(
                start=base,
                end=base + timedelta(minutes=30),
                estimated_net_consumption_kwh=10.0,
            ),
            _FakeSlot(
                start=base + timedelta(minutes=30),
                end=base + timedelta(minutes=60),
                estimated_net_consumption_kwh=5.0,
            ),
            _FakeSlot(
                start=base + timedelta(minutes=60),
                end=base + timedelta(minutes=90),
                estimated_net_consumption_kwh=-2.0,
            ),
        ]
        floor_pct, diag = df.compute_floor(
            now=now,
            slots=slots,
            current_kwh=5.0,
            usable_kwh=10.0,
            configured_min_soc_pct=5.0,
        )
        assert diag["refill_type"] == "solar_surplus"
        assert diag["reserve_kwh"] == 0.0

    def test_15min_slot_resolution(self) -> None:
        """Bridge computation works with 15-minute slots."""
        now = datetime(2025, 6, 15, 12, 0)
        df = DynamicDischargeFloor()
        net_values = [0.1, 0.1, 0.1, 0.1, 0.05, 0.05, 0.1, -0.5]
        slots = _make_slots(now, net_values, slot_minutes=15)
        floor_pct, diag = df.compute_floor(
            now=now,
            slots=slots,
            current_kwh=5.0,
            usable_kwh=10.0,
            configured_min_soc_pct=5.0,
        )
        assert diag["refill_type"] == "solar_surplus"
        assert diag["reserve_kwh"] == pytest.approx(0.6, rel=1e-4)
        assert diag["bridge_duration_hours"] == pytest.approx(1.75, rel=1e-4)
        assert floor_pct == pytest.approx(6.9, rel=1e-4)

    def test_30min_slot_resolution(self) -> None:
        """Bridge computation works with 30-minute slots."""
        now = datetime(2025, 6, 15, 12, 0)
        df = DynamicDischargeFloor()
        net_values = [0.3, 0.2, -0.8]
        slots = _make_slots(now, net_values, slot_minutes=30)
        floor_pct, diag = df.compute_floor(
            now=now,
            slots=slots,
            current_kwh=5.0,
            usable_kwh=10.0,
            configured_min_soc_pct=5.0,
        )
        assert diag["refill_type"] == "solar_surplus"
        assert diag["reserve_kwh"] == pytest.approx(0.5, rel=1e-4)
        assert diag["bridge_duration_hours"] == pytest.approx(1.0, rel=1e-4)

    def test_solar_surplus_between_consumption_reduces_reserve(self) -> None:
        """Solar surplus stops the scan at first surplus, even if followed by more consumption."""
        now = datetime(2025, 6, 15, 14, 0)
        df = DynamicDischargeFloor()
        slots = _make_slots(now, [0.5, -0.1, 0.3, -2.0])
        floor_pct, diag = df.compute_floor(
            now=now,
            slots=slots,
            current_kwh=5.0,
            usable_kwh=10.0,
            configured_min_soc_pct=5.0,
        )
        assert diag["refill_type"] == "solar_surplus"
        assert diag["reserve_kwh"] == pytest.approx(0.5, rel=1e-4)


# ---------------------------------------------------------------------------
# Safety margin self-correction tests
# ---------------------------------------------------------------------------


class TestMarginCorrection:
    """Tests for DynamicDischargeFloor.correct_margin()."""

    def test_margin_increases_after_consecutive_below_floor(self) -> None:
        """Margin steps up after 2 consecutive days below floor."""
        df = DynamicDischargeFloor()
        original = df.safety_margin
        floor_pct = 20.0

        # Day 1: below floor
        df.correct_margin(15.0, floor_pct)
        assert df.safety_margin == original

        # Day 2: below floor → trigger increase
        df.correct_margin(15.0, floor_pct)
        assert df.safety_margin == pytest.approx(original + 0.05, rel=1e-4)
        assert df._days_below_floor == 0

    def test_margin_decreases_after_consecutive_above_floor(self) -> None:
        """Margin steps down after 7 consecutive days well above floor."""
        df = DynamicDischargeFloor()
        original = df.safety_margin
        floor_pct = 20.0

        for _ in range(7):
            df.correct_margin(30.0, floor_pct)
        assert df.safety_margin == pytest.approx(original - 0.02, rel=1e-4)
        assert df._days_above_floor == 0

    def test_margin_never_below_min(self) -> None:
        """Safety margin clamped at min_margin."""
        df = DynamicDischargeFloor(safety_margin=1.06, min_margin=1.05)
        floor_pct = 20.0
        for _ in range(7):
            df.correct_margin(30.0, floor_pct)
        assert df.safety_margin == pytest.approx(1.05, rel=1e-4)

    def test_margin_never_above_max(self) -> None:
        """Safety margin clamped at max_margin."""
        df = DynamicDischargeFloor(safety_margin=1.48, max_margin=1.50)
        floor_pct = 20.0
        df.correct_margin(15.0, floor_pct)
        df.correct_margin(15.0, floor_pct)
        assert df.safety_margin == pytest.approx(1.50, rel=1e-4)

    def test_counter_resets_on_normal_soc(self) -> None:
        """Counters reset when SoC is between floor and well-above threshold."""
        df = DynamicDischargeFloor()
        floor_pct = 20.0

        df.correct_margin(15.0, floor_pct)
        df.correct_margin(22.0, floor_pct)
        assert df._days_below_floor == 0
        assert df._days_above_floor == 0

        df.correct_margin(15.0, floor_pct)
        assert df._days_below_floor == 1

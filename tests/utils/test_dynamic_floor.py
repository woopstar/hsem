"""Tests for DynamicDischargeFloor (issue #600).

Covers bridge computation, safety margin self-correction, edge cases,
and integration with various slot resolutions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
            usable_kwh=10.0,
            configured_min_soc_pct=10.0,
        )
        # Reserve = (1.0 + 0.8 + 0.5) - 3.0 (grid charge) = neg → 0
        # Since grid charge covers, refill is the charge slot
        assert diag["refill_type"] == "grid_charge"
        # Deliberate (issue #1140, planner-spec § "Dynamic discharge floor"):
        # a covering grid-charge refill releases the floor to the configured
        # minimum. The MILP already prices the pre-charge bridge energy.
        assert diag["reserve_kwh"] == pytest.approx(0.0)
        assert floor_pct == pytest.approx(10.0)

    def test_configured_min_is_absolute_floor(self) -> None:
        """Dynamic floor must be at least the configured minimum."""
        now = datetime(2025, 6, 15, 12, 0)
        df = DynamicDischargeFloor()
        # Very low consumption → computed floor would be below configured min.
        slots = _make_slots(now, [0.05, -1.0, 0.2])
        floor_pct, diag = df.compute_floor(
            now=now,
            slots=slots,
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
            usable_kwh=10.0,
            configured_min_soc_pct=5.0,
        )
        assert diag["refill_type"] == "solar_surplus"
        assert diag["reserve_kwh"] == pytest.approx(0.5, rel=1e-4)
        assert diag["bridge_duration_hours"] == pytest.approx(1.0, rel=1e-4)

    def test_hours_ahead_bounds_the_scan(self) -> None:
        """A refill beyond hours_ahead must not extend the bridge scan."""
        now = datetime(2025, 6, 15, 12, 0)
        df = DynamicDischargeFloor()
        # 50 hourly slots of steady consumption; solar surplus only shows up
        # at hour 49 — outside the default 48h look-ahead window.
        net_values = [0.5] * 49 + [-5.0]
        slots = _make_slots(now, net_values)
        floor_pct, diag = df.compute_floor(
            now=now,
            slots=slots,
            usable_kwh=100.0,
            configured_min_soc_pct=5.0,
        )
        # The surplus slot at hour 49 must be excluded from the scan, so no
        # refill is found within the window and consumption accumulates
        # only over the 48 in-window slots (0.5 * 48 = 24.0 kWh).
        assert diag["refill_type"] == "none"
        assert diag["reserve_kwh"] == pytest.approx(24.0, rel=1e-4)

    def test_hours_ahead_custom_window(self) -> None:
        """A custom hours_ahead further restricts the scan window."""
        now = datetime(2025, 6, 15, 12, 0)
        df = DynamicDischargeFloor()
        # Solar surplus at hour 5, but hours_ahead=3 excludes it.
        net_values = [0.5, 0.5, 0.5, 0.5, 0.5, -5.0]
        slots = _make_slots(now, net_values)
        floor_pct, diag = df.compute_floor(
            now=now,
            slots=slots,
            usable_kwh=100.0,
            configured_min_soc_pct=5.0,
            hours_ahead=3,
        )
        assert diag["refill_type"] == "none"
        assert diag["reserve_kwh"] == pytest.approx(1.5, rel=1e-4)

    def test_solar_surplus_between_consumption_reduces_reserve(self) -> None:
        """Solar surplus stops the scan at first surplus, even if followed by more consumption."""
        now = datetime(2025, 6, 15, 14, 0)
        df = DynamicDischargeFloor()
        slots = _make_slots(now, [0.5, -0.1, 0.3, -2.0])
        floor_pct, diag = df.compute_floor(
            now=now,
            slots=slots,
            usable_kwh=10.0,
            configured_min_soc_pct=5.0,
        )
        assert diag["refill_type"] == "solar_surplus"
        assert diag["reserve_kwh"] == pytest.approx(0.5, rel=1e-4)


# ---------------------------------------------------------------------------
# Safety margin self-correction tests
# ---------------------------------------------------------------------------


# 5-minute coordinator cycles on a +02:00 local clock (issue #1141).
_TZ = timezone(timedelta(hours=2))
_DAY_ZERO = datetime(2026, 9, 1, tzinfo=_TZ)
_CYCLE = timedelta(minutes=5)
_CYCLES_PER_DAY = 288


def _run_day(
    df: DynamicDischargeFloor, day: int, socs: list[float], floor_pct: float
) -> None:
    """Feed one local day of 5-minute cycles, one SoC reading per cycle."""
    start = _DAY_ZERO + timedelta(days=day)
    for i, soc in enumerate(socs):
        df.correct_margin(soc, floor_pct, now=start + i * _CYCLE)


def _day_start(df: DynamicDischargeFloor, day: int, soc: float, floor: float) -> None:
    """First cycle of *day* — the call that closes the previous day."""
    df.correct_margin(soc, floor, now=_DAY_ZERO + timedelta(days=day))


# Held at/above the 20 % floor, drained more than 1 point below it, then
# recharged before midnight.
_SHORTFALL_DAY = [25.0] * 96 + [15.0] * 96 + [25.0] * 96
_WELL_ABOVE_DAY = [30.0] * _CYCLES_PER_DAY  # above 20 % × 1.3
_NEUTRAL_DAY = [22.0] * _CYCLES_PER_DAY  # between 20 % and 26 %


class TestMarginCorrection:
    """Tests for DynamicDischargeFloor.correct_margin() (issues #600, #1141)."""

    def test_margin_rises_once_after_two_shortfall_days(self) -> None:
        """288 cycles a day move the margin by one step, on the day boundary."""
        df = DynamicDischargeFloor()
        original = df.safety_margin

        _run_day(df, 0, _SHORTFALL_DAY, 20.0)
        assert df.safety_margin == pytest.approx(original)
        _run_day(df, 1, _SHORTFALL_DAY, 20.0)
        assert df.safety_margin == pytest.approx(original)

        _day_start(df, 2, 25.0, 20.0)
        assert df.safety_margin == pytest.approx(original + 0.05)
        assert df._days_below_floor == 0

    def test_margin_falls_once_after_seven_well_above_days(self) -> None:
        """Seven comfortable days lower the margin by 0.02, once."""
        df = DynamicDischargeFloor()
        original = df.safety_margin

        for day in range(7):
            _run_day(df, day, _WELL_ABOVE_DAY, 20.0)
        assert df.safety_margin == pytest.approx(original)

        _day_start(df, 7, 30.0, 20.0)
        assert df.safety_margin == pytest.approx(original - 0.02)
        assert df._days_above_floor == 0

    def test_single_shortfall_day_is_not_enough(self) -> None:
        """A shortfall day followed by a comfortable day starts a new chain."""
        df = DynamicDischargeFloor()
        original = df.safety_margin

        _run_day(df, 0, _SHORTFALL_DAY, 20.0)
        _run_day(df, 1, _WELL_ABOVE_DAY, 20.0)
        _day_start(df, 2, 30.0, 20.0)

        assert df.safety_margin == pytest.approx(original)
        assert df._days_below_floor == 0
        assert df._days_above_floor == 1

    def test_neutral_day_resets_the_chain(self) -> None:
        """A day between the floor and floor × 1.3 resets both counters."""
        df = DynamicDischargeFloor()
        original = df.safety_margin

        _run_day(df, 0, _SHORTFALL_DAY, 20.0)
        _run_day(df, 1, _NEUTRAL_DAY, 20.0)
        assert df._days_below_floor == 1
        _day_start(df, 2, 22.0, 20.0)
        assert df._days_below_floor == 0
        assert df._days_above_floor == 0

        _run_day(df, 2, _SHORTFALL_DAY, 20.0)
        _day_start(df, 3, 25.0, 20.0)
        assert df.safety_margin == pytest.approx(original)
        assert df._days_below_floor == 1

    def test_unreachable_floor_is_not_a_shortfall(self) -> None:
        """#1125 shape: a floor above the live SoC never ratchets the margin."""
        df = DynamicDischargeFloor()
        original = df.safety_margin

        for day in range(3):
            _run_day(df, day, [68.0] * _CYCLES_PER_DAY, 87.2)
        _day_start(df, 3, 68.0, 87.2)

        assert df.safety_margin == pytest.approx(original)
        assert df._days_below_floor == 0

    def test_floor_jumping_above_the_soc_is_not_a_shortfall(self) -> None:
        """Only a drain below the floor in force counts, not a new higher floor."""
        df = DynamicDischargeFloor()
        original = df.safety_margin
        for day in range(2):
            start = _DAY_ZERO + timedelta(days=day)
            for i in range(_CYCLES_PER_DAY):
                floor = 5.0 if i % 2 == 0 else 87.2
                df.correct_margin(68.0, floor, now=start + i * _CYCLE)
        _day_start(df, 2, 68.0, 5.0)

        assert df.safety_margin == pytest.approx(original)
        assert df._days_below_floor == 0

    @pytest.mark.parametrize(
        ("soc_after", "shortfall"),
        [(29.5, False), (29.0, False), (28.5, True)],
    )
    def test_dip_within_one_point_is_not_a_shortfall(
        self, soc_after: float, shortfall: bool
    ) -> None:
        """Landing on the floor, or a reading just under it, is tolerated."""
        df = DynamicDischargeFloor()
        _run_day(df, 0, [30.0, soc_after], 30.0)
        assert df._day_shortfall is shortfall

    def test_first_call_has_no_floor_in_force(self) -> None:
        """Without a previous call there is nothing to judge the SoC against."""
        df = DynamicDischargeFloor()
        _run_day(df, 0, [5.0], 20.0)
        assert df._day_shortfall is False
        assert df._day_evaluated is False

    def test_gap_between_days_breaks_the_chain(self) -> None:
        """Consecutive means observed back to back — a missing day resets."""
        df = DynamicDischargeFloor()
        original = df.safety_margin

        _run_day(df, 0, _SHORTFALL_DAY, 20.0)
        _run_day(df, 2, _SHORTFALL_DAY, 20.0)
        _day_start(df, 3, 25.0, 20.0)

        assert df.safety_margin == pytest.approx(original)
        assert df._days_below_floor == 1

    def test_margin_never_below_min(self) -> None:
        """Safety margin clamped at min_margin."""
        df = DynamicDischargeFloor(safety_margin=1.06, min_margin=1.05)
        for day in range(7):
            _run_day(df, day, _WELL_ABOVE_DAY, 20.0)
        _day_start(df, 7, 30.0, 20.0)
        assert df.safety_margin == pytest.approx(1.05)

    def test_margin_never_above_max(self) -> None:
        """Safety margin clamped at max_margin."""
        df = DynamicDischargeFloor(safety_margin=1.48, max_margin=1.50)
        _run_day(df, 0, _SHORTFALL_DAY, 20.0)
        _run_day(df, 1, _SHORTFALL_DAY, 20.0)
        _day_start(df, 2, 25.0, 20.0)
        assert df.safety_margin == pytest.approx(1.50)

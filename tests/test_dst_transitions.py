"""DST (Daylight Saving Time) transition tests for issue #284.

Acceptance criteria:
- No pytz usage anywhere in HSEM.
- All planner datetimes remain timezone-aware across DST transitions.
- Planner intervals stay correct on DST forward (spring) and backward (autumn) days.
- ``next_window_start_dt`` resolves charge/discharge windows correctly across DST gaps and folds.
- ``_parse_now`` accepts valid timezone-aware ISO-8601 strings with DST offsets.

Timezone under test: ``Europe/Copenhagen``
  - DST forward (spring):  last Sunday of March  — 2024-03-31 02:00 → 03:00 (UTC+1 → UTC+2)
  - DST backward (autumn): last Sunday of October — 2024-10-27 03:00 → 02:00 (UTC+2 → UTC+1)
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.planner.engine import _parse_now
from custom_components.hsem.planner.slot_population import build_slots
from custom_components.hsem.utils.misc import (
    interval_ends_before_window_start,
    next_window_start_dt,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TZ_CPH = ZoneInfo("Europe/Copenhagen")

# Spring forward: 2024-03-31, clocks jump 02:00 → 03:00 (UTC+1 → UTC+2)
# The local wall-clock hour 02:xx does not exist this day (23-hour day).
_SPRING_FORWARD_DATE = "2024-03-31"

# Autumn fallback: 2024-10-27, clocks fall 03:00 → 02:00 (UTC+2 → UTC+1)
# The local wall-clock hour 02:xx exists *twice* this day (25-hour day).
_AUTUMN_FALLBACK_DATE = "2024-10-27"


def _cph(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Return a Copenhagen-local timezone-aware datetime (fold=0, first occurrence)."""
    return datetime(year, month, day, hour, minute, tzinfo=_TZ_CPH)


def _cph_fold(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Return a Copenhagen-local timezone-aware datetime with fold=1 (second occurrence)."""
    return datetime(year, month, day, hour, minute, fold=1, tzinfo=_TZ_CPH)


# ---------------------------------------------------------------------------
# 1. _parse_now: accepts timezone-aware ISO strings with DST offsets
# ---------------------------------------------------------------------------


class TestParseNowDst:
    """_parse_now must accept valid DST offset strings and reject naive strings."""

    def test_spring_forward_before_transition_is_accepted(self):
        """ISO string with +01:00 offset just before spring-forward is parsed correctly."""
        dt = _parse_now("2024-03-31T01:30:00+01:00")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(hours=1)

    def test_spring_forward_after_transition_is_accepted(self):
        """ISO string with +02:00 offset just after spring-forward is parsed correctly."""
        dt = _parse_now("2024-03-31T03:00:00+02:00")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(hours=2)

    def test_autumn_fallback_first_occurrence_is_accepted(self):
        """First occurrence of the folded hour (UTC+2, fold=0) is parsed correctly."""
        dt = _parse_now("2024-10-27T02:30:00+02:00")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(hours=2)

    def test_autumn_fallback_second_occurrence_is_accepted(self):
        """Second occurrence of the folded hour (UTC+1, fold=1) is parsed correctly."""
        dt = _parse_now("2024-10-27T02:30:00+01:00")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(hours=1)

    def test_naive_string_raises(self):
        """A naive ISO-8601 string must raise ValueError."""
        with pytest.raises(ValueError, match="timezone-aware"):
            _parse_now("2024-03-31T01:00:00")

    def test_naive_string_raises_on_dst_day(self):
        """Naive string on a DST day must also raise ValueError."""
        with pytest.raises(ValueError, match="timezone-aware"):
            _parse_now("2024-10-27T02:30:00")


# ---------------------------------------------------------------------------
# 2. build_slots: slot count is correct on DST transition days
# ---------------------------------------------------------------------------


class TestBuildSlotsDst:
    """build_slots must always generate exactly 24 one-hour slots per day.

    Slots are generated via timedelta arithmetic from a UTC-equivalent
    midnight, so the *calendar* day always has 24 slots regardless of the
    local wall-clock length (23 h spring-forward / 25 h autumn-fallback).
    """

    def _make_input_stub(
        self, interval_minutes: int = 60, interval_length_hours: int = 24
    ):
        """Return a minimal object with the fields build_slots needs."""

        class _Stub:
            pass

        stub = _Stub()
        stub.interval_minutes = interval_minutes
        stub.interval_length_hours = interval_length_hours
        return stub

    def test_spring_forward_produces_24_slots(self):
        """Planning from midnight on spring-forward day → 24 one-hour slots."""
        # Midnight UTC+1 (before the spring-forward at 02:00)
        now = _parse_now("2024-03-31T00:00:00+01:00")
        slots = build_slots(self._make_input_stub(), now)
        assert len(slots) == 24, f"Expected 24 slots, got {len(slots)}"

    def test_autumn_fallback_produces_24_slots(self):
        """Planning from midnight on autumn-fallback day → 24 one-hour slots."""
        # Midnight UTC+2 (before the autumn fallback at 03:00 → 02:00)
        now = _parse_now("2024-10-27T00:00:00+02:00")
        slots = build_slots(self._make_input_stub(), now)
        assert len(slots) == 24, f"Expected 24 slots, got {len(slots)}"

    def test_spring_forward_all_slots_are_aware(self):
        """Every slot start/end must be timezone-aware on spring-forward day."""
        now = _parse_now("2024-03-31T00:00:00+01:00")
        slots = build_slots(self._make_input_stub(), now)
        for slot in slots:
            assert slot.start.tzinfo is not None, f"slot.start is naive: {slot.start}"
            assert slot.end.tzinfo is not None, f"slot.end is naive: {slot.end}"

    def test_autumn_fallback_all_slots_are_aware(self):
        """Every slot start/end must be timezone-aware on autumn-fallback day."""
        now = _parse_now("2024-10-27T00:00:00+02:00")
        slots = build_slots(self._make_input_stub(), now)
        for slot in slots:
            assert slot.start.tzinfo is not None, f"slot.start is naive: {slot.start}"
            assert slot.end.tzinfo is not None, f"slot.end is naive: {slot.end}"

    def test_spring_forward_slots_are_contiguous(self):
        """Slots must be gapless and non-overlapping on spring-forward day."""
        now = _parse_now("2024-03-31T00:00:00+01:00")
        slots = build_slots(self._make_input_stub(), now)
        for a, b in zip(slots, slots[1:]):
            assert (
                a.end == b.start
            ), f"Gap between {a.end.isoformat()} and {b.start.isoformat()}"

    def test_autumn_fallback_slots_are_contiguous(self):
        """Slots must be gapless and non-overlapping on autumn-fallback day."""
        now = _parse_now("2024-10-27T00:00:00+02:00")
        slots = build_slots(self._make_input_stub(), now)
        for a, b in zip(slots, slots[1:]):
            assert (
                a.end == b.start
            ), f"Gap between {a.end.isoformat()} and {b.start.isoformat()}"

    def test_spring_forward_slot_span_equals_24_hours(self):
        """Total duration of all slots on spring-forward day must be 24 hours."""
        now = _parse_now("2024-03-31T00:00:00+01:00")
        slots = build_slots(self._make_input_stub(), now)
        total = slots[-1].end - slots[0].start
        assert total == timedelta(hours=24)

    def test_autumn_fallback_slot_span_equals_24_hours(self):
        """Total duration of all slots on autumn-fallback day must be 24 hours."""
        now = _parse_now("2024-10-27T00:00:00+02:00")
        slots = build_slots(self._make_input_stub(), now)
        total = slots[-1].end - slots[0].start
        assert total == timedelta(hours=24)


# ---------------------------------------------------------------------------
# 3. next_window_start_dt: correct resolution across DST transitions
# ---------------------------------------------------------------------------


class TestNextWindowStartDstForward:
    """next_window_start_dt across a spring-forward DST gap."""

    def test_window_before_gap_is_in_future(self):
        """A 01:00 window resolved from 00:30 on spring-forward day is in the future."""
        # It is currently 00:30 UTC+1 on spring-forward day
        now = _parse_now("2024-03-31T00:30:00+01:00")
        result = next_window_start_dt(now, time(1, 0))
        assert result > now, f"Expected result > now, got {result!r} <= {now!r}"

    def test_window_in_dst_gap_resolves_to_next_day(self):
        """A window at 02:30 on spring-forward day (non-existent wall time) is skipped.

        The gap hour (02:00–03:00) does not exist.  When now is already at 03:30 UTC+2,
        the 02:30 target is in the past, so next_window_start_dt must return tomorrow.
        """
        # It is 03:30 UTC+2 (after the spring-forward)
        now = _parse_now("2024-03-31T03:30:00+02:00")
        result = next_window_start_dt(now, time(2, 30))
        # The result must be strictly in the future relative to now
        assert result > now, f"Expected result > now, got {result!r} <= {now!r}"

    def test_window_after_gap_is_same_day(self):
        """A 04:00 window resolved from 00:30 on spring-forward day is later today."""
        now = _parse_now("2024-03-31T00:30:00+01:00")
        result = next_window_start_dt(now, time(4, 0))
        assert result > now
        # Must be on the same calendar date (still 2024-03-31)
        assert result.date() == now.date()


class TestNextWindowStartDstFallback:
    """next_window_start_dt across an autumn-fallback DST fold."""

    def test_window_before_fold_is_in_future(self):
        """A 01:00 window resolved from 00:30 on autumn-fallback day is in the future."""
        now = _parse_now("2024-10-27T00:30:00+02:00")
        result = next_window_start_dt(now, time(1, 0))
        assert result > now

    def test_window_in_folded_hour_resolved_after_now(self):
        """A window at 02:30 on autumn-fallback day is resolved correctly.

        When it is currently 01:00 UTC+2 (before the fold), 02:30 is in the future.
        """
        now = _parse_now("2024-10-27T01:00:00+02:00")
        result = next_window_start_dt(now, time(2, 30))
        assert result > now

    def test_window_after_fold_is_future(self):
        """A 04:00 window resolved from 00:30 UTC+2 on autumn-fallback day is in future."""
        now = _parse_now("2024-10-27T00:30:00+02:00")
        result = next_window_start_dt(now, time(4, 0))
        assert result > now
        assert result.date() == now.date()

    def test_window_already_passed_goes_to_next_day(self):
        """A 01:00 window resolved when it is already 02:00 (post-fold) advances by 1 day."""
        # After the fold at 03:00 → 02:00, it is now 02:30 UTC+1 (fold=1)
        now = _parse_now("2024-10-27T02:30:00+01:00")
        result = next_window_start_dt(now, time(1, 0))
        assert result > now
        # Must have advanced to the next calendar day
        assert result.date() > now.date()


# ---------------------------------------------------------------------------
# 4. interval_ends_before_window_start: correct across DST transitions
# ---------------------------------------------------------------------------


class TestIntervalEndsDst:
    """interval_ends_before_window_start must be consistent across DST transitions."""

    def test_spring_forward_interval_before_gap(self):
        """An interval ending at 01:30 is before a 03:00 window on spring-forward day."""
        now = _parse_now("2024-03-31T00:00:00+01:00")
        # Interval ends at 01:30 UTC+1
        interval_end = _parse_now("2024-03-31T01:30:00+01:00")
        # Window starts at 03:00 (wall clock — UTC+2 after the spring-forward)
        assert interval_ends_before_window_start(interval_end, time(3, 0), now)

    def test_spring_forward_interval_after_gap(self):
        """An interval ending at 04:00 UTC+2 is before tomorrow's 03:00 window.

        When *now* is 03:30 UTC+2, the 03:00 wall-clock time has already passed
        today, so ``next_window_start_dt`` advances to 03:00 tomorrow.
        An interval ending at 04:00 *today* ends before 03:00 *tomorrow*, so
        the function correctly returns ``True``.
        """
        now = _parse_now("2024-03-31T03:30:00+02:00")
        interval_end = _parse_now("2024-03-31T04:00:00+02:00")
        # 04:00 today < 03:00 tomorrow → interval ends before the (next-day) window
        assert interval_ends_before_window_start(interval_end, time(3, 0), now)

    def test_autumn_fallback_interval_before_fold(self):
        """An interval ending at 01:30 UTC+2 is before a 03:00 window on fallback day."""
        now = _parse_now("2024-10-27T00:00:00+02:00")
        interval_end = _parse_now("2024-10-27T01:30:00+02:00")
        assert interval_ends_before_window_start(interval_end, time(3, 0), now)

    def test_autumn_fallback_interval_after_fold(self):
        """An interval ending at 04:00 UTC+1 is before tomorrow's 03:00 window.

        When *now* is 03:30 UTC+1 (post-fold), the 03:00 wall-clock time has
        already passed today (03:00 == now's day, rolled to next-day).  An
        interval ending at 04:00 today ends before 03:00 tomorrow.
        """
        now = _parse_now("2024-10-27T03:30:00+01:00")
        interval_end = _parse_now("2024-10-27T04:00:00+01:00")
        # 04:00 today < 03:00 tomorrow → interval ends before the (next-day) window
        assert interval_ends_before_window_start(interval_end, time(3, 0), now)


# ---------------------------------------------------------------------------
# 5. Full planner run: planner executes without error on DST days
# ---------------------------------------------------------------------------


class TestPlannerRunsDstDays:
    """run_planner must complete without raising on both DST transition days."""

    def _base_input(self, now_iso: str):
        """Return a minimal PlannerInput for a 24-hour summer-like day."""
        from custom_components.hsem.models.planner_inputs import (
            BatteryScheduleInput,
            HourlyConsumptionAverage,
            PlannerInput,
            PricePoint,
            SolcastSlot,
        )

        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(24)
        ]
        solar = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
        consumption = [
            HourlyConsumptionAverage(
                hour=h,
                avg_1d=0.3,
                avg_3d=0.3,
                avg_7d=0.3,
                avg_14d=0.3,
            )
            for h in range(24)
        ]
        schedules = [
            BatteryScheduleInput(
                enabled=True,
                start=time(17, 0),
                end=time(21, 0),
                min_price_difference=0.05,
            )
        ]
        return PlannerInput(
            now_iso=now_iso,
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_charge_power_w=5000.0,
            battery_conversion_loss_pct=10.0,
            battery_purchase_price=10_000.0,
            battery_expected_cycles=6000,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=consumption,
            price_points=prices,
            solcast_slots=solar,
            battery_schedules=schedules,
            excess_export_enabled=False,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
            is_read_only=True,
        )

    def test_spring_forward_midnight_completes(self):
        """Planner runs without error from midnight on spring-forward day (UTC+1)."""
        from custom_components.hsem.planner import run_planner

        inp = self._base_input("2024-03-31T00:00:00+01:00")
        result = run_planner(inp)
        assert result.slots, "Expected non-empty slots on spring-forward day"
        assert len(result.slots) == 24

    def test_autumn_fallback_midnight_completes(self):
        """Planner runs without error from midnight on autumn-fallback day (UTC+2)."""
        from custom_components.hsem.planner import run_planner

        inp = self._base_input("2024-10-27T00:00:00+02:00")
        result = run_planner(inp)
        assert result.slots, "Expected non-empty slots on autumn-fallback day"
        assert len(result.slots) == 24

    def test_spring_forward_all_output_slots_are_aware(self):
        """All output slots from a spring-forward run must have timezone-aware datetimes."""
        from custom_components.hsem.planner import run_planner

        result = run_planner(self._base_input("2024-03-31T00:00:00+01:00"))
        for slot in result.slots:
            assert slot.start.tzinfo is not None, f"slot.start is naive: {slot.start}"
            assert slot.end.tzinfo is not None, f"slot.end is naive: {slot.end}"

    def test_autumn_fallback_all_output_slots_are_aware(self):
        """All output slots from an autumn-fallback run must have timezone-aware datetimes."""
        from custom_components.hsem.planner import run_planner

        result = run_planner(self._base_input("2024-10-27T00:00:00+02:00"))
        for slot in result.slots:
            assert slot.start.tzinfo is not None, f"slot.start is naive: {slot.start}"
            assert slot.end.tzinfo is not None, f"slot.end is naive: {slot.end}"

    def test_spring_forward_discharge_window_evening_is_planned(self):
        """Discharge window 17:00–21:00 must be planned correctly on spring-forward day.

        Spring-forward is at 02:00, so the evening window is unaffected and must
        contain ``BatteriesDischargeMode`` recommendations.
        """
        from custom_components.hsem.planner import run_planner
        from custom_components.hsem.utils.recommendations import Recommendations

        result = run_planner(self._base_input("2024-03-31T00:00:00+01:00"))
        discharge_slots = result.slots_with_recommendation(
            Recommendations.BatteriesDischargeMode.value
        )
        assert discharge_slots, "Expected discharge slots on spring-forward day"

    def test_autumn_fallback_discharge_window_evening_is_planned(self):
        """Discharge window 17:00–21:00 must be planned correctly on autumn-fallback day.

        Autumn fallback is at 03:00, so the evening window is unaffected.
        """
        from custom_components.hsem.planner import run_planner
        from custom_components.hsem.utils.recommendations import Recommendations

        result = run_planner(self._base_input("2024-10-27T00:00:00+02:00"))
        discharge_slots = result.slots_with_recommendation(
            Recommendations.BatteriesDischargeMode.value
        )
        assert discharge_slots, "Expected discharge slots on autumn-fallback day"

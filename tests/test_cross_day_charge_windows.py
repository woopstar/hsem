"""Regression tests for cross-date-boundary charge-window planning (issue #267).

Verifies that HSEM can plan battery charging at night (e.g. 02:00) to cover a
morning discharge window on the *next calendar day* (e.g. 07:00), including
the scenario where the planning run occurs late in the evening (22:00) before
the date boundary is crossed.

Acceptance criteria from issue #267:
- HSEM can charge at night for morning peak use.
- Tests cover cheap 02:00 grid charge and expensive 07:00 consumption.
- ``next_window_start_dt`` resolves the next occurrence correctly.
- ``interval_ends_before_window_start`` allows pre-midnight charge slots.
- The discharge window on the next calendar day is correctly included in
  battery-schedule capacity planning.
"""

from datetime import UTC, datetime, time, timedelta

from custom_components.hsem.utils.misc import (
    interval_ends_before_window_start,
    next_window_start_dt,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = UTC

# Base date for tests: 2026-05-11 (arbitrary Monday)
_BASE_DATE = datetime(2026, 5, 11, tzinfo=_UTC)


def _dt(hour: int, minute: int = 0, day_offset: int = 0) -> datetime:
    """Return a UTC-aware datetime on the base date plus an optional day offset."""
    return _BASE_DATE.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    ) + timedelta(days=day_offset)


# ---------------------------------------------------------------------------
# Tests for next_window_start_dt
# ---------------------------------------------------------------------------


class TestNextWindowStartDt:
    """Unit tests for the next_window_start_dt helper introduced for issue #267."""

    def test_same_day_window_in_future(self):
        """At 06:00 a 07:00 window is resolved to today at 07:00."""
        now = _dt(6, 0)
        result = next_window_start_dt(now, time(7, 0))
        assert result == _dt(7, 0), f"Expected today 07:00, got {result}"

    def test_same_day_window_just_past(self):
        """At 09:00 a 07:00 window is already past → resolved to tomorrow at 07:00."""
        now = _dt(9, 0)
        result = next_window_start_dt(now, time(7, 0))
        assert result == _dt(7, 0, day_offset=1), (
            f"Expected tomorrow 07:00, got {result}"
        )

    def test_next_day_window_from_late_evening(self):
        """At 22:00 a 07:00 morning window is resolved to tomorrow at 07:00.

        This is the key P0-03 scenario: night charging at 02:00 for a 07:00
        discharge window on the following calendar day.
        """
        now = _dt(22, 0)
        result = next_window_start_dt(now, time(7, 0))
        assert result == _dt(7, 0, day_offset=1), (
            f"At 22:00, the 07:00 window should resolve to tomorrow, got {result}"
        )

    def test_exact_window_start_treated_as_past(self):
        """At exactly 07:00 the 07:00 window is considered to have started and
        thus the *next* occurrence is tomorrow."""
        now = _dt(7, 0)
        result = next_window_start_dt(now, time(7, 0))
        assert result == _dt(7, 0, day_offset=1), (
            "Exact window start should resolve to tomorrow (window already started)"
        )

    def test_midnight_window_at_00_30(self):
        """At 00:30 a 23:00 cross-midnight window is resolved to tonight at 23:00."""
        now = _dt(0, 30)
        result = next_window_start_dt(now, time(23, 0))
        assert result == _dt(23, 0), (
            f"At 00:30, the 23:00 window should be tonight (same day), got {result}"
        )

    def test_before_midnight_window_today(self):
        """At 21:00 a 23:00 window is resolved to tonight at 23:00."""
        now = _dt(21, 0)
        result = next_window_start_dt(now, time(23, 0))
        assert result == _dt(23, 0), f"Expected today 23:00, got {result}"

    def test_result_always_strictly_after_now(self):
        """The returned datetime must always be strictly after ``now``."""
        test_cases = [
            (_dt(6, 0), time(7, 0)),  # same day, future
            (_dt(22, 0), time(7, 0)),  # next day
            (_dt(21, 0), time(23, 0)),  # same day, tonight
            (_dt(0, 30), time(23, 0)),  # tonight from early morning
        ]
        for now, ws in test_cases:
            result = next_window_start_dt(now, ws)
            assert result > now, (
                f"next_window_start_dt({now.time()}, {ws}) = {result.time()} "
                f"is not strictly after now"
            )


# ---------------------------------------------------------------------------
# Tests for interval_ends_before_window_start (cross-day scenarios)
# ---------------------------------------------------------------------------


class TestIntervalEndsBeforeWindowStartCrossDay:
    """Cross-date-boundary tests for interval_ends_before_window_start.

    These complement the existing TestIntervalEndsBeforeWindowStart tests in
    test_midnight_rollover.py, focusing on the P0-03 scenario.
    """

    def test_night_charge_slot_before_next_day_morning_window(self):
        """A 02:00-03:00 slot tonight ends before a 07:00 window tomorrow.

        Scenario: now=22:00 day D, charge slot ends at 03:00 day D+1,
        discharge window starts at 07:00 day D+1.
        Expected: True — the slot is a valid pre-charge window.
        """
        now = _dt(22, 0)  # 22:00 tonight
        charge_slot_end = _dt(3, 0, day_offset=1)  # 03:00 tomorrow
        discharge_window_start = time(7, 0)  # 07:00

        assert (
            interval_ends_before_window_start(
                charge_slot_end, discharge_window_start, now
            )
            is True
        ), (
            "A 03:00 charge slot end should be before a 07:00 discharge window "
            "when it is currently 22:00"
        )

    def test_charge_slot_at_02_before_07_discharge(self):
        """P0-03 acceptance criterion: cheap 02:00 charge before expensive 07:00 use.

        Slot ends at 03:00 (UTC), window starts at 07:00.  With now=22:00 the
        function should confirm 03:00 < 07:00 (next day).
        """
        now = _dt(22, 0)
        charge_slot_end = _dt(3, 0, day_offset=1)  # 03:00 next day
        discharge_window_start = time(7, 0)

        result = interval_ends_before_window_start(
            charge_slot_end, discharge_window_start, now
        )
        assert result is True, (
            "02:00-03:00 grid charge must be flagged as 'before' the 07:00 "
            "morning discharge window (cross-day boundary)"
        )

    def test_charge_slot_overlapping_discharge_window_excluded(self):
        """A slot that ends at 08:00 is NOT before the 07:00 discharge window."""
        now = _dt(22, 0)
        charge_slot_end = _dt(8, 0, day_offset=1)  # 08:00 next day
        discharge_window_start = time(7, 0)

        assert (
            interval_ends_before_window_start(
                charge_slot_end, discharge_window_start, now
            )
            is False
        ), "A slot ending at 08:00 must NOT be treated as 'before' the 07:00 window"

    def test_same_evening_slot_before_next_morning_window(self):
        """A 22:30-23:00 slot tonight is before the 07:00 window tomorrow."""
        now = _dt(22, 0)
        charge_slot_end = _dt(23, 0)  # 23:00 tonight
        discharge_window_start = time(7, 0)

        assert (
            interval_ends_before_window_start(
                charge_slot_end, discharge_window_start, now
            )
            is True
        ), "A 22:30-23:00 slot tonight is before tomorrow's 07:00 discharge window"

    def test_slot_after_current_time_but_past_window_start_excluded(self):
        """A slot ending at 09:00 is after the 07:00 window → must be excluded."""
        now = _dt(22, 0)
        charge_slot_end = _dt(9, 0, day_offset=1)  # 09:00 next day
        discharge_window_start = time(7, 0)

        assert (
            interval_ends_before_window_start(
                charge_slot_end, discharge_window_start, now
            )
            is False
        )


# ---------------------------------------------------------------------------
# Integration-style tests: simulate the full planning scenario
# ---------------------------------------------------------------------------


class TestCrossDayChargePlanningIntegration:
    """Simulate the high-level planning decisions for the P0-03 scenario.

    These tests validate that, given realistic price data, the planner logic
    correctly identifies cheap 02:00 charge slots for a 07:00-09:00 morning
    discharge window that starts on the next calendar day.

    We exercise the helper functions in isolation (without instantiating the
    full HA sensor) to verify the correctness of the building blocks.
    """

    def _make_intervals(
        self, now: datetime, interval_minutes: int = 60, total_hours: int = 48
    ) -> list[dict]:
        """Generate fake hourly recommendation intervals covering ``total_hours``."""
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        intervals = []
        steps = (total_hours * 60) // interval_minutes
        for i in range(steps):
            s = start + timedelta(minutes=i * interval_minutes)
            e = s + timedelta(minutes=interval_minutes)
            intervals.append({"start": s, "end": e})
        return intervals

    def test_02_00_slot_identified_as_valid_charge_window(self):
        """The 02:00-03:00 slot (next day) is included when planning for 07:00 discharge.

        Setup:
          - now = 22:00 day D
          - discharge window = 07:00-09:00 day D+1
          - recommendation horizon = 48 h (covers both today and tomorrow)
        """
        now = _dt(22, 0)
        discharge_start = time(7, 0)
        discharge_end = time(9, 0)

        # Build a 48-hour horizon of hourly intervals from midnight today
        intervals = self._make_intervals(now, interval_minutes=60, total_hours=48)

        # The discharge window starts at next_window_start_dt
        window_dt = next_window_start_dt(now, discharge_start)

        # Build window end dt (same day as window_dt since 09:00 > 07:00)
        if discharge_end > discharge_start:
            window_end_dt = datetime.combine(window_dt.date(), discharge_end).replace(
                tzinfo=now.tzinfo
            )
        else:
            window_end_dt = datetime.combine(
                (window_dt + timedelta(days=1)).date(), discharge_end
            ).replace(tzinfo=now.tzinfo)

        # Identify discharge intervals (within the window)
        discharge_intervals = [
            iv
            for iv in intervals
            if iv["start"] >= window_dt and iv["end"] <= window_end_dt
        ]

        assert len(discharge_intervals) == 2, (
            f"Expected 2 discharge intervals (07:00-08:00, 08:00-09:00), "
            f"got {len(discharge_intervals)}: {[(iv['start'].time(), iv['end'].time()) for iv in discharge_intervals]}"
        )

        # Identify valid pre-charge intervals: not yet passed, before the window
        valid_charge_intervals = [
            iv
            for iv in intervals
            if iv["end"] > now
            and interval_ends_before_window_start(iv["end"], discharge_start, now)
        ]

        # The 02:00-03:00 slot must be among valid charge windows
        charge_02_to_03 = _dt(2, 0, day_offset=1)
        found = any(iv["start"] == charge_02_to_03 for iv in valid_charge_intervals)
        assert found, (
            "The 02:00-03:00 slot (next day) must be a valid charge window when "
            "planning for a 07:00 morning discharge window from 22:00 the night before. "
            f"Valid charge windows found: "
            f"{[(iv['start'].time(), iv['start'].date()) for iv in valid_charge_intervals]}"
        )

    def test_expensive_07_00_interval_is_discharge_window(self):
        """The 07:00-09:00 intervals on day D+1 are flagged as discharge windows."""
        now = _dt(22, 0)
        discharge_start = time(7, 0)
        discharge_end = time(9, 0)

        window_dt = next_window_start_dt(now, discharge_start)
        if discharge_end > discharge_start:
            window_end_dt = datetime.combine(window_dt.date(), discharge_end).replace(
                tzinfo=now.tzinfo
            )
        else:
            window_end_dt = datetime.combine(
                (window_dt + timedelta(days=1)).date(), discharge_end
            ).replace(tzinfo=now.tzinfo)

        # Intervals for day D+1
        intervals = self._make_intervals(now, interval_minutes=60, total_hours=48)
        discharge_intervals = [
            iv
            for iv in intervals
            if iv["start"] >= window_dt and iv["end"] <= window_end_dt
        ]

        # Both 07:00-08:00 and 08:00-09:00 must be captured
        starts = {iv["start"].time() for iv in discharge_intervals}
        assert time(7, 0) in starts, "07:00-08:00 must be a discharge interval"
        assert time(8, 0) in starts, "08:00-09:00 must be a discharge interval"

        # All discharge intervals must be on the next calendar day
        for iv in discharge_intervals:
            assert iv["start"].date() == (now.date() + timedelta(days=1)), (
                f"Discharge interval {iv['start']} is not on the expected next-day date"
            )

    def test_no_charge_slots_after_discharge_window_start(self):
        """Slots that start after the discharge window begins must not be charge windows."""
        now = _dt(22, 0)
        discharge_start = time(7, 0)

        intervals = self._make_intervals(now, interval_minutes=60, total_hours=48)

        # No interval ending after the window start should be flagged as valid charge
        post_window_slots = [
            iv
            for iv in intervals
            if iv["end"] > next_window_start_dt(now, discharge_start)
            and interval_ends_before_window_start(iv["end"], discharge_start, now)
        ]

        assert post_window_slots == [], (
            "No slots ending after the discharge window start should qualify as "
            f"pre-charge windows. Found: "
            f"{[(iv['start'].time(), iv['start'].date()) for iv in post_window_slots]}"
        )

    def test_cheap_night_price_preferred_over_expensive_morning(self):
        """Verify price sorting puts cheap night hours before expensive morning hours.

        This is a conceptual test: given a list of (hour, price) pairs, the
        cheapest pre-discharge hours should sort to the top.  This mirrors the
        third-priority sort in ``_async_find_best_time_to_charge_battery_schedule``.
        """
        now = _dt(22, 0)
        discharge_start = time(7, 0)

        # Simulate import prices for a 48-hour horizon
        # Night prices are cheap, morning prices expensive
        price_by_hour = {
            0: 0.15,
            1: 0.12,
            2: 0.08,
            3: 0.07,  # cheap night (day D+1)
            4: 0.09,
            5: 0.10,
            6: 0.13,  # pre-dawn (day D+1)
            7: 0.45,
            8: 0.50,  # morning peak — discharge window
            22: 0.20,
            23: 0.18,  # this evening (day D)
        }

        intervals = self._make_intervals(now, interval_minutes=60, total_hours=48)

        # Tag each interval with its import price
        tagged = []
        for iv in intervals:
            h = iv["start"].hour
            price = price_by_hour.get(h, 0.15)
            tagged.append({**iv, "import_price": price})

        # Filter valid charge intervals
        valid_charge = [
            iv
            for iv in tagged
            if iv["end"] > now
            and interval_ends_before_window_start(iv["end"], discharge_start, now)
        ]

        # Sort by cheapest first (mirrors planner logic)
        valid_charge.sort(key=lambda x: (x["import_price"], x["start"]))

        assert valid_charge, "There must be at least one valid pre-charge interval"

        cheapest = valid_charge[0]
        assert cheapest["import_price"] <= 0.10, (
            f"The cheapest pre-charge slot should cost ≤0.10, got {cheapest['import_price']} "
            f"at {cheapest['start'].time()} on {cheapest['start'].date()}"
        )

        # The 02:00-03:00 slot (price=0.08) should be among the top-3 cheapest
        top3_hours = {iv["start"].hour for iv in valid_charge[:3]}
        assert 2 in top3_hours or 3 in top3_hours, (
            f"Expected cheap night hours (02:00-04:00) in top-3, got hours: {top3_hours}"
        )

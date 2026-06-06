"""Regression tests for midnight rollover interval filtering (P0-02).

Verifies that charge/discharge recommendation windows crossing midnight are
handled correctly by the utility helpers and the schedule validator.

Acceptance criteria from issue #266:
- A window from 23:00 to 02:00 works.
- A charge window from 00:00 to 06:00 works.
- Tests cover same-day windows and cross-midnight windows.

Note: schedule validator functions are ``async`` but contain no I/O — we run
them via ``asyncio.run()`` to avoid pytest-asyncio / pytest-socket conflicts on
Windows.
"""

import asyncio
from datetime import UTC, datetime, time, timedelta

from custom_components.hsem.utils.time_windows import (
    interval_ends_before_window_start,
    is_time_in_window,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = UTC


def _dt(hour: int, minute: int = 0, date_offset: int = 0) -> datetime:
    """Return a UTC-aware datetime on 2026-01-15 (+ optional day offset)."""
    base = datetime(2026, 1, 15, hour, minute, tzinfo=_UTC)
    return base + timedelta(days=date_offset)


# ---------------------------------------------------------------------------
# Tests for is_time_in_window
# ---------------------------------------------------------------------------


class TestIsTimeInWindow:
    """Unit tests for the is_time_in_window helper."""

    # --- Same-day windows ---------------------------------------------------

    def test_same_day_inside(self):
        """Current time falls inside a same-day window."""
        assert is_time_in_window(time(8, 0), time(7, 0), time(9, 0)) is True

    def test_same_day_at_start(self):
        """Current time equals the window start (inclusive boundary)."""
        assert is_time_in_window(time(7, 0), time(7, 0), time(9, 0)) is True

    def test_same_day_at_end(self):
        """Current time equals the window end (exclusive boundary)."""
        assert is_time_in_window(time(9, 0), time(7, 0), time(9, 0)) is False

    def test_same_day_before_window(self):
        """Current time is before a same-day window."""
        assert is_time_in_window(time(6, 59), time(7, 0), time(9, 0)) is False

    def test_same_day_after_window(self):
        """Current time is after a same-day window."""
        assert is_time_in_window(time(9, 1), time(7, 0), time(9, 0)) is False

    # --- Cross-midnight windows (P0-02 acceptance criteria) ----------------

    def test_cross_midnight_23_to_02_at_2300(self):
        """At 23:00 the 23:00-02:00 window has just started → inside."""
        assert is_time_in_window(time(23, 0), time(23, 0), time(2, 0)) is True

    def test_cross_midnight_23_to_02_at_0100(self):
        """At 01:00 we are inside the 23:00-02:00 window."""
        assert is_time_in_window(time(1, 0), time(23, 0), time(2, 0)) is True

    def test_cross_midnight_23_to_02_at_0200(self):
        """At 02:00 the 23:00-02:00 window has ended (exclusive)."""
        assert is_time_in_window(time(2, 0), time(23, 0), time(2, 0)) is False

    def test_cross_midnight_23_to_02_at_2100(self):
        """At 21:00 the 23:00-02:00 window has not started yet."""
        assert is_time_in_window(time(21, 0), time(23, 0), time(2, 0)) is False

    def test_cross_midnight_0000_to_0600_at_0300(self):
        """At 03:00 we are inside the 00:00-06:00 window (P0-02 AC)."""
        # 00:00-06:00 is a same-day window (start < end)
        assert is_time_in_window(time(3, 0), time(0, 0), time(6, 0)) is True

    def test_cross_midnight_0000_to_0600_at_2300(self):
        """At 23:00 we are outside the 00:00-06:00 window."""
        assert is_time_in_window(time(23, 0), time(0, 0), time(6, 0)) is False


# ---------------------------------------------------------------------------
# Tests for interval_ends_before_window_start
# ---------------------------------------------------------------------------


class TestIntervalEndsBeforeWindowStart:
    """Unit tests for interval_ends_before_window_start helper."""

    def test_interval_ends_before_same_day_window(self):
        """Interval ending at 06:00 is before a same-day window at 07:00."""
        now = _dt(5, 0)  # 05:00
        interval_end = _dt(6, 0)  # 06:00
        window_start = time(7, 0)
        assert (
            interval_ends_before_window_start(interval_end, window_start, now) is True
        )

    def test_interval_ends_after_same_day_window(self):
        """Interval ending at 08:00 is NOT before a window starting at 07:00."""
        now = _dt(5, 0)
        interval_end = _dt(8, 0)
        window_start = time(7, 0)
        assert (
            interval_ends_before_window_start(interval_end, window_start, now) is False
        )

    def test_interval_ends_before_cross_midnight_window(self):
        """Interval ending at 22:00 is before a cross-midnight window at 23:00."""
        now = _dt(21, 0)  # 21:00
        interval_end = _dt(22, 0)  # 22:00
        window_start = time(23, 0)  # tonight at 23:00
        assert (
            interval_ends_before_window_start(interval_end, window_start, now) is True
        )

    def test_interval_ends_after_cross_midnight_window_start(self):
        """Interval ending at 23:30 is NOT before a cross-midnight window at 23:00."""
        now = _dt(21, 0)
        interval_end = _dt(23, 30)
        window_start = time(23, 0)
        assert (
            interval_ends_before_window_start(interval_end, window_start, now) is False
        )

    def test_interval_next_day_before_midnight_window_start(self):
        """Interval ending 00:30 tomorrow is NOT before a window at 23:00 today."""
        now = _dt(21, 0)  # today 21:00
        # interval ends tomorrow at 00:30
        interval_end = _dt(0, 30, date_offset=1)
        window_start = time(23, 0)  # tonight
        assert (
            interval_ends_before_window_start(interval_end, window_start, now) is False
        )


# ---------------------------------------------------------------------------
# Tests for schedule validator (batteries_schedule_*.py)
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run a coroutine synchronously using the SelectorEventLoop.

    ``asyncio.run()`` respects the current event loop policy, which in the HA
    test environment points to ``WindowsProactorEventLoopPolicy``.  That loop
    requires ``socket.socketpair()`` which is blocked by ``pytest-socket``.
    We bypass the policy by explicitly creating a ``SelectorEventLoop`` (which
    also needs a socket pair on Windows) while temporarily enabling sockets.
    """
    import sys

    import pytest_socket

    pytest_socket.enable_socket()
    try:
        if sys.platform == "win32":
            loop = asyncio.SelectorEventLoop()
        else:
            loop = asyncio.new_event_loop()
    finally:
        pytest_socket.disable_socket(allow_unix_socket=True)

    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestScheduleValidator:
    """Tests that cross-midnight windows pass schedule validation.

    The validator functions are ``async`` but contain no I/O.  We run them
    using ``_run_async()`` which creates a short-lived SelectorEventLoop with
    sockets temporarily enabled (required on Windows) so that pytest-socket
    does not block the internal self-pipe creation.
    """

    def test_same_day_window_valid(self):
        """A same-day window (start < end) passes validation."""
        from custom_components.hsem.flows.batteries_schedule_1 import (
            validate_batteries_schedule_1_input,
        )

        errors = _run_async(
            validate_batteries_schedule_1_input(
                {
                    "hsem_batteries_enable_batteries_schedule_1": True,
                    "hsem_batteries_enable_batteries_schedule_1_start": "07:00:00",
                    "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
                }
            )
        )
        assert errors == {}

    def test_cross_midnight_window_valid(self):
        """A cross-midnight window (start > end, e.g. 23:00-02:00) passes validation."""
        from custom_components.hsem.flows.batteries_schedule_1 import (
            validate_batteries_schedule_1_input,
        )

        errors = _run_async(
            validate_batteries_schedule_1_input(
                {
                    "hsem_batteries_enable_batteries_schedule_1": True,
                    "hsem_batteries_enable_batteries_schedule_1_start": "23:00:00",
                    "hsem_batteries_enable_batteries_schedule_1_end": "02:00:00",
                }
            )
        )
        assert errors == {}, (
            f"Expected no errors for cross-midnight window, got: {errors}"
        )

    def test_zero_to_six_window_valid(self):
        """A 00:00-06:00 window (P0-02 AC) passes validation."""
        from custom_components.hsem.flows.batteries_schedule_1 import (
            validate_batteries_schedule_1_input,
        )

        errors = _run_async(
            validate_batteries_schedule_1_input(
                {
                    "hsem_batteries_enable_batteries_schedule_1": True,
                    "hsem_batteries_enable_batteries_schedule_1_start": "00:00:00",
                    "hsem_batteries_enable_batteries_schedule_1_end": "06:00:00",
                }
            )
        )
        assert errors == {}

    def test_equal_start_end_invalid(self):
        """A window with identical start and end times is invalid."""
        from custom_components.hsem.flows.batteries_schedule_1 import (
            validate_batteries_schedule_1_input,
        )

        errors = _run_async(
            validate_batteries_schedule_1_input(
                {
                    "hsem_batteries_enable_batteries_schedule_1": True,
                    "hsem_batteries_enable_batteries_schedule_1_start": "09:00:00",
                    "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
                }
            )
        )
        assert errors != {}

    def test_schedule_2_cross_midnight_window_valid(self):
        """Schedule 2: cross-midnight window passes validation."""
        from custom_components.hsem.flows.batteries_schedule_2 import (
            validate_batteries_schedule_2_input,
        )

        errors = _run_async(
            validate_batteries_schedule_2_input(
                {
                    "hsem_batteries_enable_batteries_schedule_2": True,
                    "hsem_batteries_enable_batteries_schedule_2_start": "23:00:00",
                    "hsem_batteries_enable_batteries_schedule_2_end": "02:00:00",
                }
            )
        )
        assert errors == {}, (
            f"Expected no errors for cross-midnight window, got: {errors}"
        )

    def test_schedule_3_cross_midnight_window_valid(self):
        """Schedule 3: cross-midnight window passes validation."""
        from custom_components.hsem.flows.batteries_schedule_3 import (
            validate_batteries_schedule_3_input,
        )

        errors = _run_async(
            validate_batteries_schedule_3_input(
                {
                    "hsem_batteries_enable_batteries_schedule_3": True,
                    "hsem_batteries_enable_batteries_schedule_3_start": "23:00:00",
                    "hsem_batteries_enable_batteries_schedule_3_end": "02:00:00",
                }
            )
        )
        assert errors == {}, (
            f"Expected no errors for cross-midnight window, got: {errors}"
        )

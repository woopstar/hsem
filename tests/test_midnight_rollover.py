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
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = UTC


def _dt(hour: int, minute: int = 0, date_offset: int = 0) -> datetime:
    """Return a UTC-aware datetime on 2026-01-15 (+ optional day offset)."""
    base = datetime(2026, 1, 15, hour, minute, tzinfo=_UTC)
    return base + timedelta(days=date_offset)


# ---------------------------------------------------------------------------
# Tests for schedule validator (batteries_schedule_*.py)
#
# Note: the former ``interval_ends_before_window_start`` unit tests that
# lived in this module were removed in issue #898 along with the helper
# itself — it had no production caller and could not safely replace
# ``pre_charge.py``'s per-occurrence eligible-slot filter (see that file's
# comments). Cross-midnight eligible-slot filtering is now covered directly
# via ``apply_charge_schedules`` in
# ``tests/planner/test_charge_scheduler_capacity.py``.
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
        from custom_components.hsem.flows.schedule_helpers import (
            validate_batteries_schedule_input,
        )

        errors = _run_async(
            validate_batteries_schedule_input(
                1,
                {
                    "hsem_batteries_enable_batteries_schedule_1": True,
                    "hsem_batteries_enable_batteries_schedule_1_start": "07:00:00",
                    "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
                },
            )
        )
        assert errors == {}

    def test_cross_midnight_window_valid(self):
        """A cross-midnight window (start > end, e.g. 23:00-02:00) passes validation."""
        from custom_components.hsem.flows.schedule_helpers import (
            validate_batteries_schedule_input,
        )

        errors = _run_async(
            validate_batteries_schedule_input(
                1,
                {
                    "hsem_batteries_enable_batteries_schedule_1": True,
                    "hsem_batteries_enable_batteries_schedule_1_start": "23:00:00",
                    "hsem_batteries_enable_batteries_schedule_1_end": "02:00:00",
                },
            )
        )
        assert errors == {}, (
            f"Expected no errors for cross-midnight window, got: {errors}"
        )

    def test_zero_to_six_window_valid(self):
        """A 00:00-06:00 window (P0-02 AC) passes validation."""
        from custom_components.hsem.flows.schedule_helpers import (
            validate_batteries_schedule_input,
        )

        errors = _run_async(
            validate_batteries_schedule_input(
                1,
                {
                    "hsem_batteries_enable_batteries_schedule_1": True,
                    "hsem_batteries_enable_batteries_schedule_1_start": "00:00:00",
                    "hsem_batteries_enable_batteries_schedule_1_end": "06:00:00",
                },
            )
        )
        assert errors == {}

    def test_equal_start_end_invalid(self):
        """A window with identical start and end times is invalid."""
        from custom_components.hsem.flows.schedule_helpers import (
            validate_batteries_schedule_input,
        )

        errors = _run_async(
            validate_batteries_schedule_input(
                1,
                {
                    "hsem_batteries_enable_batteries_schedule_1": True,
                    "hsem_batteries_enable_batteries_schedule_1_start": "09:00:00",
                    "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
                },
            )
        )
        assert errors != {}

    def test_schedule_2_cross_midnight_window_valid(self):
        """Schedule 2: cross-midnight window passes validation."""
        from custom_components.hsem.flows.schedule_helpers import (
            validate_batteries_schedule_input,
        )

        errors = _run_async(
            validate_batteries_schedule_input(
                2,
                {
                    "hsem_batteries_enable_batteries_schedule_2": True,
                    "hsem_batteries_enable_batteries_schedule_2_start": "23:00:00",
                    "hsem_batteries_enable_batteries_schedule_2_end": "02:00:00",
                },
            )
        )
        assert errors == {}, (
            f"Expected no errors for cross-midnight window, got: {errors}"
        )

    def test_schedule_3_cross_midnight_window_valid(self):
        """Schedule 3: cross-midnight window passes validation."""
        from custom_components.hsem.flows.schedule_helpers import (
            validate_batteries_schedule_input,
        )

        errors = _run_async(
            validate_batteries_schedule_input(
                3,
                {
                    "hsem_batteries_enable_batteries_schedule_3": True,
                    "hsem_batteries_enable_batteries_schedule_3_start": "23:00:00",
                    "hsem_batteries_enable_batteries_schedule_3_end": "02:00:00",
                },
            )
        )
        assert errors == {}, (
            f"Expected no errors for cross-midnight window, got: {errors}"
        )

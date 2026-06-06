"""Time-window helpers for comparing and advancing wall-clock times.

Used by the planner engine to check whether a time falls within a
charge/discharge window, calculate the next occurrence of a window
start, and determine if an interval ends before a window begins.
"""

from datetime import datetime, time, timedelta


def is_time_in_window(current: time, start: time, end: time) -> bool:
    """Check whether *current* falls within the [start, end) window.

    Handles windows that cross midnight (e.g. 23:00–02:00) correctly.

    Args:
        current: The time to test.
        start: Start of the window (inclusive).
        end: End of the window (exclusive).

    Returns:
        True if *current* is within the window, False otherwise.
    """
    if start <= end:
        # Same-day window (e.g. 07:00–09:00)
        return start <= current < end
    # Cross-midnight window (e.g. 23:00–02:00)
    return current >= start or current < end


def next_window_start_dt(now: datetime, window_start: time) -> datetime:
    """Return the next upcoming datetime when a discharge/charge window begins.

    Anchors ``window_start`` to today's date and advances by one day when that
    moment has already passed, so the returned datetime is always strictly in
    the future relative to ``now``.

    This enables cross-date-boundary charge planning: a 07:00 discharge
    window configured for the next calendar day is correctly resolved when
    it is currently, say, 22:00 on the previous day.

    Args:
        now: Current timezone-aware datetime.
        window_start: Wall-clock start time of the discharge/charge window.

    Returns:
        Timezone-aware datetime of the next occurrence of *window_start*.
    """
    candidate = datetime.combine(now.date(), window_start).replace(tzinfo=now.tzinfo)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def interval_ends_before_window_start(
    interval_end: datetime,
    window_start: time,
    now: datetime,
) -> bool:
    """Return True when an *interval* ends strictly before a schedule *window* begins.

    Resolves ``window_start`` to a timezone-aware :class:`datetime` on the
    correct calendar date so that cross-midnight windows (e.g. a window that
    starts at ``23:00`` today and ends at ``02:00`` tomorrow) are handled
    without false positives.

    Args:
        interval_end: Timezone-aware end of the recommendation interval.
        window_start: Wall-clock start time of the charge/discharge window.
        now: Current timezone-aware datetime (used to anchor the date).

    Returns:
        True if the interval ends before the window starts.
    """
    return interval_end <= next_window_start_dt(now, window_start)

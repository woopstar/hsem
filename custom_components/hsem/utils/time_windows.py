"""Time-window helpers for comparing and advancing wall-clock times.

Used by the planner engine to check whether a time falls within a
charge/discharge window and to calculate the next occurrence of a
window start.
"""

from datetime import datetime, time, timedelta


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

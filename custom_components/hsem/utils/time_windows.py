"""Time-window helpers for comparing and advancing wall-clock times.

Used by the planner engine to check whether a time falls within a
charge/discharge window and to calculate the next occurrence of a
window start.
"""

from datetime import datetime, time, timedelta


def window_containing_or_next(
    now: datetime, window_start: time, window_end: time
) -> tuple[datetime, datetime]:
    """Return the active wall-clock window occurrence or the next occurrence.

    Window membership uses a half-open interval: ``[start, end)``. For a
    cross-midnight window, an early-morning ``now`` can therefore resolve to an
    occurrence that started on the previous calendar day.

    Args:
        now: Current timezone-aware datetime.
        window_start: Wall-clock start time of the window.
        window_end: Wall-clock end time of the window.

    Returns:
        Absolute start and end datetimes for the occurrence containing ``now``,
        or for the next occurrence when no window is active.
    """
    today_start = datetime.combine(now.date(), window_start).replace(tzinfo=now.tzinfo)
    end_day_offset = 0 if window_end > window_start else 1
    today_end = datetime.combine(
        now.date() + timedelta(days=end_day_offset), window_end
    ).replace(tzinfo=now.tzinfo)

    if today_start <= now < today_end:
        return today_start, today_end

    if end_day_offset:
        previous_start = today_start - timedelta(days=1)
        previous_end = datetime.combine(now.date(), window_end).replace(
            tzinfo=now.tzinfo
        )
        if previous_start <= now < previous_end:
            return previous_start, previous_end

    if now < today_start:
        return today_start, today_end

    return today_start + timedelta(days=1), today_end + timedelta(days=1)


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

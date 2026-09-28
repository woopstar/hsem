"""Downtime tracking for the hour-block rolling-average sensors.

An hour block's utility-meter reading is a real measurement only when Home
Assistant observed the block end to end (issue #1101). Issue #1110 showed
that "this session started before the block" is far too strict. Any restart
or HSEM reload after a block began threw away a fully metered block, even
when Home Assistant was down for a few seconds and the meter kept its
restored reading. After a reset each hour has at most one sample, so every
lost block left a gap in the load forecast for a whole day.

This module measures the real downtime instead. Each average sensor
persists a heartbeat (``last_alive``) and a bounded list of *unobserved
intervals* through ``RestoreEntity`` extra data. On start-up the interval
between the last heartbeat and the new session start is added to that list.
A block counts as observed when these intervals overlap it by at most
:data:`BLOCK_RESET_TOLERANCE` in total.

The heartbeat is read whenever Home Assistant dumps restore state: on stop,
on entity removal (reload), and every 15 minutes. After a crash the gap is
therefore overestimated by at most one dump interval, which errs on the side
of skipping a sample. With no heartbeat at all (first run after an upgrade,
or unreadable data) the whole past counts as unobserved. That is the original
strict #1101 rule, so the fallback fails closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, override

import homeassistant.util.dt as dt_util
from homeassistant.helpers.restore_state import ExtraStoredData

#: Maximum total unobserved time inside an hour block, and maximum drift
#: between the block start and the meter's ``last_reset``, for the reading
#: to count as an observation of the whole block. Absorbs reset-callback
#: latency and quick restarts (issues #1101 and #1110).
BLOCK_RESET_TOLERANCE = timedelta(minutes=5)

#: Unobserved intervals older than this can no longer affect a block that is
#: still waiting to be stored (the overnight block is the latest, stored up
#: to ~24 h after it started).
UNOBSERVED_RETENTION = timedelta(hours=48)

#: Upper bound on persisted intervals, so a restart loop cannot grow the
#: restore payload without limit.
MAX_UNOBSERVED_INTERVALS = 32

#: Start of the "everything before this session" interval used when no
#: heartbeat is available (strict fallback).
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

Interval = tuple[datetime, datetime]


def _parse_instant(value: Any) -> datetime | None:
    """Return a timezone-aware UTC datetime, or ``None`` when unparseable."""
    if isinstance(value, datetime):
        parsed: datetime | None = value
    elif isinstance(value, str):
        parsed = dt_util.parse_datetime(value)
    else:
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    return dt_util.as_utc(parsed)


def prune_unobserved(intervals: Sequence[Interval], now: datetime) -> list[Interval]:
    """Drop expired intervals and keep only the newest bounded set.

    Args:
        intervals: Unobserved ``(start, end)`` intervals.
        now: Reference time for the retention window.

    Returns:
        The retained intervals sorted by start, at most
        :data:`MAX_UNOBSERVED_INTERVALS` long.
    """
    cutoff = dt_util.as_utc(now) - UNOBSERVED_RETENTION
    kept = sorted(
        (interval for interval in intervals if interval[1] >= cutoff),
        key=lambda interval: interval[0],
    )
    return kept[-MAX_UNOBSERVED_INTERVALS:]


def restore_unobserved(payload: Any, session_start: datetime) -> list[Interval]:
    """Rebuild the unobserved intervals when a sensor (re)joins Home Assistant.

    Args:
        payload: The dict produced by :meth:`AvgSensorExtraData.as_dict` in
            the previous session, or anything else when unavailable.
        session_start: When the current session started.

    Returns:
        The previous session's intervals plus the downtime
        ``(last_alive, session_start)``. Without a readable heartbeat this is
        ``[(epoch, session_start)]``, so no block that started before this
        session is ever stored.
    """
    start_utc = dt_util.as_utc(session_start)
    last_alive = (
        _parse_instant(payload.get("last_alive")) if isinstance(payload, dict) else None
    )
    if last_alive is None:
        return [(_EPOCH, start_utc)]

    intervals: list[Interval] = []
    raw_intervals = payload.get("unobserved")
    if isinstance(raw_intervals, list):
        for raw in raw_intervals:
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                continue
            begin, end = _parse_instant(raw[0]), _parse_instant(raw[1])
            if begin is not None and end is not None and end > begin:
                intervals.append((begin, end))
    if start_utc > last_alive:
        intervals.append((last_alive, start_utc))
    return prune_unobserved(intervals, start_utc)


def unobserved_overlap(
    unobserved: Sequence[Interval], block_start: datetime, block_end: datetime
) -> timedelta:
    """Return the total unobserved time inside ``[block_start, block_end)``."""
    start = dt_util.as_utc(block_start)
    end = dt_util.as_utc(block_end)
    total = timedelta(0)
    for begin, finish in unobserved:
        overlap = min(dt_util.as_utc(finish), end) - max(dt_util.as_utc(begin), start)
        if overlap > timedelta(0):
            total += overlap
    return total


def block_observed(
    last_reset: Any,
    unobserved: Sequence[Interval] | None,
    block_start: datetime,
    block_end: datetime,
) -> bool:
    """Return True when the given hour block was observed end to end.

    Two conditions must hold:

    - **The meter reset at the block start** (issue #1101). The daily
      utility meter resets at ``hour_start``, so its reading covers the
      block only when ``last_reset`` lies within
      :data:`BLOCK_RESET_TOLERANCE` of ``block_start``. A later reset is a
      missed reset fired on restart. An earlier one belongs to a previous day.
    - **Home Assistant was down for no more than the tolerance inside the
      block** (issue #1110). The overlap of the unobserved intervals with the
      block must be at most :data:`BLOCK_RESET_TOLERANCE`. A restart lasting
      a few seconds keeps the block; a crash spanning part of the hour does not.

    A missing or unparseable ``last_reset``, or unknown observation history
    (``unobserved is None``), cannot be verified and is rejected (fail closed,
    missing is not zero).

    Args:
        last_reset: Raw ``last_reset`` attribute (ISO string or datetime).
        unobserved: Unobserved intervals of this sensor, ``None`` before the
            sensor has joined Home Assistant.
        block_start: Timezone-aware start of the block being sampled.
        block_end: Timezone-aware end of the block being sampled.

    Returns:
        True when the reading may be stored as that block's sample.
    """
    if unobserved is None:
        return False
    if unobserved_overlap(unobserved, block_start, block_end) > BLOCK_RESET_TOLERANCE:
        return False
    parsed = _parse_instant(last_reset)
    if parsed is None:
        return False
    drift = abs(parsed - dt_util.as_utc(block_start))
    return drift <= BLOCK_RESET_TOLERANCE


class AvgSensorExtraData(ExtraStoredData):
    """Heartbeat and unobserved intervals persisted across restarts."""

    def __init__(self, last_alive: datetime, unobserved: Sequence[Interval]) -> None:
        """Initialize the extra restore data.

        Args:
            last_alive: The moment Home Assistant last saw the sensor running.
            unobserved: Unobserved intervals to carry into the next session.
        """
        self.last_alive = last_alive
        self.unobserved = list(unobserved)

    @override
    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "last_alive": dt_util.as_utc(self.last_alive).isoformat(),
            "unobserved": [
                [dt_util.as_utc(begin).isoformat(), dt_util.as_utc(end).isoformat()]
                for begin, end in self.unobserved
            ],
        }

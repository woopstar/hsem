"""Shared datetime helpers for the HSEM integration.

Single responsibility: provide one canonical path for all datetime
normalisation and slot-key computation inside HSEM.

**Design rules**

- Current time always comes from ``homeassistant.util.dt.now()`` so that
  HA's configured timezone is respected throughout the integration.
- All slot timestamps are normalised to the HA local timezone and truncated
  to whole seconds before being used as dictionary keys or for comparisons.
- ``slot_key`` is the single authoritative key used when matching planner
  slots to recommendation slots; it floors the timestamp to the configured
  interval boundary so that small timing differences never cause a mismatch.

Usage
-----
>>> from custom_components.hsem.utils.datetime_utils import now, slot_key
>>> current = now()
>>> key = slot_key(current, interval_minutes=60)

Pure-module usage (no HA dependency needed)
-------------------------------------------
The planner engine and related pure modules receive ``now`` as an argument.
They should use ``as_tz(dt, now.tzinfo)`` instead of ``dt.astimezone(now.tzinfo)``
so that all timezone normalisation is routed through this module.

>>> from custom_components.hsem.utils.datetime_utils import as_tz
>>> slot_local = as_tz(slot.start, now.tzinfo)
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, time, timedelta, timezone, tzinfo

import homeassistant.util.dt as dt_util


def now() -> datetime:
    """Return the current HA-local timezone-aware datetime without microseconds.

    Always prefer this over ``datetime.now()``, ``datetime.utcnow()``, or
    ``datetime.now(timezone.utc)`` inside HSEM so that the integration
    consistently uses the user-configured Home Assistant timezone.

    Returns:
        A timezone-aware :class:`~datetime.datetime` in the HA local timezone
        with ``microsecond=0``.
    """
    return dt_util.now().replace(microsecond=0)


def normalize_datetime(value: datetime) -> datetime:
    """Return a HA-local timezone-aware copy of *value* without microseconds.

    Naive datetimes are assumed to be in the HA local timezone.
    Timezone-aware datetimes are converted to the HA local timezone.
    Microseconds are always stripped so that timestamps from different sources
    (e.g. ``dt_util.now()`` with sub-second jitter vs planner arithmetic
    anchored at midnight) compare equal.

    Args:
        value: Any :class:`~datetime.datetime`, naive or aware.

    Returns:
        A timezone-aware :class:`~datetime.datetime` in the HA local timezone
        with ``microsecond=0``.
    """
    return dt_util.as_local(value).replace(microsecond=0)


def normalize_slot_start(value: datetime, interval_minutes: int) -> datetime:
    """Return *value* floored to the start of its enclosing interval slot.

    Examples::

        normalize_slot_start(22:17:42, 60)  -> 22:00:00
        normalize_slot_start(22:17:42, 15)  -> 22:15:00
        normalize_slot_start(22:00:00, 60)  -> 22:00:00  (already on boundary)

    Args:
        value: Any timezone-aware or naive datetime.
        interval_minutes: Slot width in minutes (e.g. 15 or 60).

    Returns:
        A timezone-aware :class:`~datetime.datetime` in the HA local timezone
        floored to the nearest ``interval_minutes`` boundary, with
        ``second=0`` and ``microsecond=0``.

    Raises:
        ValueError: If ``interval_minutes`` is not a positive integer.
    """
    if interval_minutes <= 0:
        raise ValueError(f"interval_minutes must be positive, got {interval_minutes}")

    local = normalize_datetime(value)
    floored_minute = (local.minute // interval_minutes) * interval_minutes
    return local.replace(minute=floored_minute, second=0, microsecond=0)


def slot_key(value: datetime, interval_minutes: int) -> datetime:
    """Return the canonical key used to match planner and recommendation slots.

    This is the single authoritative key for all slot-lookup dictionaries
    inside HSEM.  Using it on both sides of a lookup guarantees that:

    - Slots from different timezones (e.g. ``ZoneInfo('Europe/Copenhagen')``
      vs a fixed ``+02:00`` offset) that represent the same physical instant
      produce the same key.
    - The two occurrences of an autumn repeated wall-clock hour remain distinct.
    - Sub-second jitter (microseconds) from ``dt_util.now()`` is stripped.
    - Timestamps with seconds != 0 (e.g. from ``dt_util.now()``) are floored
      to the interval boundary so they match planner slots anchored at midnight.

    Args:
        value: A timezone-aware or naive datetime.
        interval_minutes: Slot width in minutes (e.g. 15 or 60).

    Returns:
        A timezone-aware :class:`~datetime.datetime` that uniquely identifies
        the slot containing *value* under the given interval width.
    """
    return utc_key(normalize_slot_start(value, interval_minutes))


def slot_contains(start: datetime, end: datetime, value: datetime) -> bool:
    """Return whether *value* is inside the half-open slot ``[start, end)``.

    All operands are compared by UTC instant.  Direct comparisons between two
    datetimes carrying the same :class:`zoneinfo.ZoneInfo` object compare their
    wall-clock fields and can treat the two occurrences of an autumn repeated
    hour as equal.
    """
    return utc_key(start) <= utc_key(value) < utc_key(end)


def as_tz(value: datetime, tz: tzinfo | None) -> datetime:
    """Return *value* converted to the given timezone without microseconds.

    This is the canonical replacement for the ``dt.astimezone(now.tzinfo)``
    pattern used throughout the pure planner modules.  Using this function
    instead of calling ``.astimezone()`` directly ensures all timezone
    conversions are routed through one place and microseconds are always
    stripped.

    For pure planner modules that have no Home Assistant dependency, pass
    ``now.tzinfo`` (where ``now`` was obtained from the coordinator via
    ``dt_util.now()``).  For HA-aware modules, prefer ``normalize_datetime``
    which also handles naive inputs.

    Args:
        value: A timezone-aware :class:`~datetime.datetime`.
        tz: Target timezone (e.g. ``now.tzinfo``).  When ``None`` the
            system local timezone is used as a safe fallback — in practice
            ``tz`` is always non-``None`` because callers pass
            ``now.tzinfo`` from a timezone-aware ``now``.

    Returns:
        A timezone-aware :class:`~datetime.datetime` in *tz* with
        ``microsecond=0``.
    """
    return value.astimezone(tz).replace(microsecond=0)


def future_slot_indices(slot_ends: Iterable[datetime], now: datetime) -> list[int]:
    """Return the indices of slots that have not yet ended at *now*.

    The MILP's LP index ``t`` enumerates exactly these slots, in order, so
    ``future_slot_indices(...)[t]`` maps an LP index back to the full slot list.
    Any caller that aligns per-slot data with a MILP solve must use this rather
    than re-deriving the filter, or the two will drift.

    Args:
        slot_ends: End datetime of every slot, in slot order.
        now: Timezone-aware current datetime.

    Returns:
        Ascending indices of slots whose end lies strictly after *now*.
    """
    # Compare physical instants: on the DST fall-back day the repeated hour
    # has two slots with the same wall-clock time.
    now_utc = now.astimezone(UTC)
    return [i for i, end in enumerate(slot_ends) if end.astimezone(UTC) > now_utc]


def _fixed_offset(instant: datetime, zone: tzinfo | None) -> datetime:
    """Return *instant* as local time in *zone*, pinned to its UTC offset.

    A fixed-offset ``tzinfo`` makes Python compare, sort and subtract slot
    boundaries by physical instant.  Two datetimes that share one
    ``ZoneInfo`` are compared by wall clock instead, which ignores ``fold``
    and breaks on the DST fall-back day.
    """
    local = instant.astimezone(zone)
    return local.replace(tzinfo=timezone(local.utcoffset() or timedelta(0)))


def _local_day_start_utc(day: datetime, zone: tzinfo | None) -> datetime:
    """Return the physical (UTC) instant at which *day*'s local date begins."""
    return datetime.combine(day.date(), time(0), tzinfo=zone).astimezone(UTC)


def physical_slot_grid(
    anchor: datetime, interval_minutes: int, horizon_hours: int
) -> list[tuple[datetime, datetime]]:
    """Return ``(start, end)`` slot boundaries stepped in physical time.

    The grid starts at local midnight of *anchor*'s date and ends at the
    local wall-clock time ``horizon_hours`` later, so a 24 h horizon on a
    DST day covers the whole local day: 23 real hours (92 × 15 min) on the
    spring-forward day, 25 real hours (100 × 15 min) on the fall-back day.
    Every slot spans exactly *interval_minutes* of real time.  Boundaries
    carry a fixed UTC offset (see :func:`_fixed_offset`); ``.hour`` and
    ``.date()`` still read local wall-clock time.

    Args:
        anchor: Timezone-aware datetime whose local date starts the grid.
        interval_minutes: Positive slot width in minutes.
        horizon_hours: Positive horizon length in local wall-clock hours.

    Returns:
        Chronological, contiguous ``(start, end)`` pairs.
    """
    zone = anchor.tzinfo
    start_utc = _local_day_start_utc(anchor, zone)
    end_wall = datetime.combine(anchor.date(), time(0)) + timedelta(hours=horizon_hours)
    end_utc = end_wall.replace(tzinfo=zone).astimezone(UTC)
    step = timedelta(minutes=interval_minutes)
    count = (end_utc - start_utc) // step
    return [
        (
            _fixed_offset(start_utc + i * step, zone),
            _fixed_offset(start_utc + (i + 1) * step, zone),
        )
        for i in range(count)
    ]


def slot_position(
    start: datetime, planning_midnight: datetime, interval_minutes: int
) -> tuple[int, int]:
    """Return ``(day_offset, slot_in_day)`` for a physical slot start.

    *slot_in_day* counts real *interval_minutes* steps since the start of
    the slot's local date, so it is unique within a day even when the DST
    fall-back hour repeats a wall-clock time.  On ordinary days it equals
    ``(hour * 60 + minute) // interval_minutes``.

    Args:
        start: Timezone-aware slot start.
        planning_midnight: Timezone-aware local midnight of the planning
            day; its ``tzinfo`` is the local zone.
        interval_minutes: Positive slot width in minutes.

    Returns:
        ``(day_offset, slot_in_day)``.
    """
    zone = planning_midnight.tzinfo
    local = start.astimezone(zone)
    day_offset = (local.date() - planning_midnight.date()).days
    elapsed = start.astimezone(UTC) - _local_day_start_utc(local, zone)
    return day_offset, elapsed // timedelta(minutes=interval_minutes)


def utc_now_iso() -> str:
    """Return the current HA-local datetime as an ISO-8601 string.

    Replaces scattered ``dt_util.now().isoformat()`` calls so that the
    microsecond=0 invariant is always honoured.

    Returns:
        ISO-8601 string of the current HA-local time without microseconds.
    """
    return now().isoformat()


def physical_elapsed(later: datetime, earlier: datetime) -> timedelta:
    """Return elapsed time between two datetimes by physical UTC instant.

    A naive *earlier* is assumed to share *later*'s timezone before
    conversion, so pairing a HA-local "now" with an older naive calendar
    timestamp still yields a correct physical duration.

    Args:
        later: The more recent datetime, timezone-aware or naive.
        earlier: The older datetime, timezone-aware or naive.

    Returns:
        The physical duration between the two instants.
    """
    later_aware = later if later.tzinfo is not None else later.astimezone()
    earlier_aware = (
        earlier
        if earlier.tzinfo is not None
        else earlier.replace(tzinfo=later_aware.tzinfo)
    )
    return utc_key(later_aware) - utc_key(earlier_aware)


def cache_is_fresh(cached_at: datetime, current: datetime, window: timedelta) -> bool:
    """Return whether *cached_at* is within *window* of *current*.

    Used by short-lived in-memory caches (recorder history, weather
    forecasts) to decide whether a cached value can be reused without
    re-querying its source.

    Args:
        cached_at: When the cached value was fetched.
        current: The current reference time.
        window: The maximum age before the cache is considered stale.

    Returns:
        ``True`` when ``0 <= age < window``.
    """
    age = physical_elapsed(current, cached_at)
    return timedelta(0) <= age < window


def utc_key(dt: datetime) -> datetime:
    """Normalise a timezone-aware datetime to a UTC key for slot matching.

    Two datetimes that represent the **same instant** but carry different
    ``tzinfo`` objects (e.g. ``ZoneInfo('Europe/Copenhagen')`` vs a fixed
    ``+02:00`` offset) hash and compare as equal in Python.  However,
    sub-second fields can differ when the recommendation slot was created
    from one path while the planner slot was built from ``timedelta``
    arithmetic anchored at midnight.  Stripping microseconds on both sides
    guarantees a deterministic match regardless of when each was created.

    Args:
        dt: A timezone-aware :class:`datetime.datetime`.

    Returns:
        A ``datetime`` normalised to UTC with ``microsecond=0`` that can be
        used as a dictionary key for slot matching.
    """
    return dt.astimezone(UTC).replace(microsecond=0)

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
>>> from custom_components.hsem.datetime_utils import now, slot_key
>>> current = now()
>>> key = slot_key(current, interval_minutes=60)
"""

from __future__ import annotations

from datetime import datetime

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
      vs a fixed ``+02:00`` offset) that represent the same wall-clock instant
      produce the same key.
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
    return normalize_slot_start(value, interval_minutes)

"""Per-slot energy deltas from accumulator readings.

Shared by :class:`custom_components.hsem.ml.history_reader.HistoryReader`
for full and incremental reads (#1355): the last reading of every completed
slot is computed first so a caller can continue from it later.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from custom_components.hsem.utils.datetime_utils import (
    normalize_datetime,
    normalize_slot_start,
    slot_key,
    utc_key,
)

# Maximum sane per-slot consumption in kWh (cap for data errors).
MAX_SLOT_KWH = 12.5


def compute_slot_deltas(
    readings: list[tuple[datetime, float]],
    now: datetime,
    slot_minutes: int,
) -> list[tuple[datetime, int, float]]:
    """Compute per-slot energy deltas from accumulator readings.

    Groups readings into time slots of ``slot_minutes`` width and computes
    the delta between the last reading of consecutive slots.

    Args:
        readings: Sorted list of ``(timestamp, accumulator_value)``.
        now: Current time (used to skip incomplete slots).
        slot_minutes: Width of each slot in minutes.

    Returns:
        List of ``(slot_start_dt, slot_index, energy_kwh)``.

    Note:
        Physical slot identity is a canonical UTC datetime, never an
        ``ordinal * slots_per_day`` integer.  A wall-clock integer would
        collapse both folds of an autumn repeated hour and cannot
        represent a 92/100-slot DST day.
    """
    return deltas_from_slot_ends(slot_ends(readings, now, slot_minutes), slot_minutes)


def slot_ends(
    readings: list[tuple[datetime, float]],
    now: datetime,
    slot_minutes: int,
) -> list[tuple[datetime, datetime, float]]:
    """Return the last reading of every completed slot, oldest first.

    Args:
        readings: Sorted list of ``(timestamp, accumulator_value)``.
        now: Current time (used to skip the incomplete current slot).
        slot_minutes: Width of each slot in minutes.

    Returns:
        ``(slot_key, timestamp, accumulator_value)`` per completed slot.
    """
    # Group readings by physical slot identity after converting recorder
    # UTC timestamps to Home Assistant local time.  Calendar features
    # (date, DOW, and wall slot) must be derived from the local timestamp;
    # ordering and identity must remain UTC-based.
    slot_groups: dict[datetime, list[tuple[datetime, float]]] = {}
    for ts, val in readings:
        if not math.isfinite(val):
            continue
        local_ts = normalize_datetime(ts)
        key = slot_key(local_ts, slot_minutes)
        slot_groups.setdefault(key, []).append((local_ts, val))

    # Get the last reading of each slot.
    ends: list[tuple[datetime, datetime, float]] = []
    for key in sorted(slot_groups.keys()):
        group = slot_groups[key]
        last_ts, last_val = max(group, key=lambda x: utc_key(x[0]))
        ends.append((key, last_ts, last_val))

    # Compute the slot key for "now" and skip incomplete current slot.
    now_key = slot_key(now, slot_minutes)
    return [(k, ts, v) for k, ts, v in ends if k < now_key]


def deltas_from_slot_ends(
    slot_ends: list[tuple[datetime, datetime, float]],
    slot_minutes: int,
) -> list[tuple[datetime, int, float]]:
    """Compute per-slot energy deltas between consecutive slot ends.

    Args:
        slot_ends: Output of :func:`slot_ends`.
        slot_minutes: Width of each slot in minutes.

    Returns:
        List of ``(slot_start_dt, slot_index, energy_kwh)``.
    """
    if len(slot_ends) < 2:
        return []

    # Compute deltas between consecutive slots.
    history: list[tuple[datetime, int, float]] = []
    for i in range(1, len(slot_ends)):
        prev_key, _prev_ts, prev_val = slot_ends[i - 1]
        curr_key, curr_ts, curr_val = slot_ends[i]

        # Never turn a recorder outage into one oversized slot.  Only
        # adjacent physical slots have compatible accumulator boundaries.
        if curr_key - prev_key != timedelta(minutes=slot_minutes):
            continue

        delta_kwh = curr_val - prev_val

        # Skip non-finite, negative, reset, and zero deltas.
        if not math.isfinite(delta_kwh) or delta_kwh <= 0:
            continue

        # Cap unreasonably large deltas.
        if delta_kwh > MAX_SLOT_KWH:
            continue

        # Consecutive end-of-slot readings delimit the *current* slot.
        # Labelling the delta with ``prev_ts`` shifts every observation one
        # interval early.  Keep the local timestamp (including DST fold)
        # for calendar features and expose the wall slot separately.
        slot_start = normalize_slot_start(curr_ts, slot_minutes)
        slot_index = (slot_start.hour * 60 + slot_start.minute) // slot_minutes

        history.append((slot_start, slot_index, round(delta_kwh, 4)))

    return history

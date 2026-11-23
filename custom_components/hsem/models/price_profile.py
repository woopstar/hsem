"""Weekday and time-of-day price profile for missing prices (issue #1434).

The planner fills a slot that has no published price from this profile before
it falls back to the nearest-earlier-day estimate of issue #1002.  The profile
is built by the coordinator from the persisted price history
(:mod:`custom_components.hsem.models.price_history_tracker`) and handed to the
planner in :class:`~custom_components.hsem.models.planner_input.PlannerInput`,
so the planner layer stays free of I/O.

Prices are kept on a 15-minute grid of local wall-clock time.  A sample that
is wider than 15 minutes covers each quarter it spans, so hourly and
quarter-hourly samples of the same period land on the same keys and a switch
between the two does not skew the profile.

A bucket holds the median of its days, so one extreme day does not move it,
and it exists only once :data:`PROFILE_MIN_DAYS` days were recorded for it.
The profile carries the *shape* of the week; the planner shifts it to the
level of the prices that are published now before it uses it.  The shift of
the last cycle that had published prices travels with the profile, for a
price outage that outlasts the held prices (issue #1452).  Pure — no Home
Assistant imports.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from statistics import median
from typing import Any

from custom_components.hsem.utils.persistence import (
    aware_datetime_from_iso,
    finite_float,
)

#: Width of one profile bucket in minutes.
PROFILE_STEP_MINUTES = 15

#: Days a profile bucket must have seen before it is used.  On a year of
#: real prices a bucket of three days was a worse estimate than the nearest
#: earlier day, and six days was the first size that was not (issue #1452,
#: ``scripts/score_price_estimates.py``).
PROFILE_MIN_DAYS = 6

#: Minutes of published prices the profile must overlap before the planner
#: trusts the level shift it derives from them.
PROFILE_MIN_ANCHOR_MINUTES = 240

#: ``(import price, export price)`` in local currency per kWh.
PricePair = tuple[float, float]

#: One recorded slot: ``(start, slot width in minutes, import, export)``.
PriceSample = tuple[datetime, int, float, float]

#: How long after the last published slot its level shift is still used.
#: One day later the shift was a better estimate than the profile as
#: recorded, two days later it was a worse one (issue #1452).
LEVEL_SHIFT_MAX_AGE = timedelta(hours=24)

#: The level shift of one price side: ``(shift, start of the first slot that
#: is too late to use it)``.
LevelShift = tuple[float, datetime]


def level_shift_applies(start: datetime, until: datetime) -> bool:
    """Return whether a slot starts early enough to use a level shift.

    Args:
        start: Slot start.  A start without a timezone has no place on the
            shift's time axis and never qualifies.
        until: Start of the first slot that is too late for the shift.
    """
    return start.tzinfo is not None and start < until


def median_shift(
    known: Iterable[float | None],
    profile_side: Iterable[float | None],
    interval_minutes: int,
) -> float | None:
    """Return the shift that moves the profile to the published price level.

    The profile is a median of up to eight weeks, so its absolute level can
    be far from today's.  Its shape is still useful.  The shift is the median
    difference between the published prices and the profile on the slots
    that have both, so the profile is read relative to the prices that are
    known now (issue #1434).

    Args:
        known: Published price per slot, ``None`` where it is missing.
        profile_side: Profile price per slot, ``None`` where it has none.
        interval_minutes: Slot width in minutes.

    Returns:
        The shift to add to a profile price, or ``None`` when fewer than
        :data:`PROFILE_MIN_ANCHOR_MINUTES` of published prices overlap the
        profile.
    """
    differences = [
        price - shape
        for price, shape in zip(known, profile_side)
        if price is not None and shape is not None
    ]
    if len(differences) * interval_minutes < PROFILE_MIN_ANCHOR_MINUTES:
        return None
    return median(differences)


def _level_row(level: LevelShift | None) -> list[Any] | None:
    """Return a level shift as a JSON row, or ``None`` without one."""
    return None if level is None else [level[0], level[1].isoformat()]


def level_from_row(row: Any) -> LevelShift | None:
    """Rebuild a level shift from :func:`_level_row` output.

    Args:
        row: ``[shift, ISO time]``; anything else gives ``None``.
    """
    if not isinstance(row, list) or len(row) != 2 or not isinstance(row[1], str):
        return None
    shift = finite_float(row[0])
    until = aware_datetime_from_iso(row[1])
    if shift is None or until is None:
        return None
    return shift, until


def _median_pair(pairs: Iterable[PricePair]) -> PricePair:
    """Return the per-side median of *pairs*."""
    rows = list(pairs)
    return (median(pair[0] for pair in rows), median(pair[1] for pair in rows))


def _mean_pair(pairs: list[PricePair]) -> PricePair:
    """Return the per-side mean of *pairs*; a single pair is returned as is."""
    if len(pairs) == 1:
        return pairs[0]
    count = len(pairs)
    return (
        sum(pair[0] for pair in pairs) / count,
        sum(pair[1] for pair in pairs) / count,
    )


def _buckets[K](
    by_key: Mapping[K, Mapping[Any, list[PricePair]]], min_days: int
) -> dict[K, PricePair]:
    """Return the median over days of every bucket with enough days.

    Args:
        by_key: Per bucket, the price pairs recorded on each local date.
        min_days: Dates a bucket needs before it is kept.

    Returns:
        Bucket key to the median of its per-day means.
    """
    return {
        key: _median_pair(_mean_pair(pairs) for pairs in days.values())
        for key, days in by_key.items()
        if len(days) >= min_days
    }


def _rows(table: Mapping[Any, PricePair]) -> list[list[float]]:
    """Return a bucket table as sorted JSON rows of ``[*key, import, export]``."""
    return [
        [*(key if isinstance(key, tuple) else (key,)), *pair]
        for key, pair in sorted(table.items())
    ]


def _table(rows: Any, key_width: int) -> dict[Any, PricePair]:
    """Rebuild a bucket table from :func:`_rows` output."""
    table: dict[Any, PricePair] = {}
    for row in rows or []:
        key = tuple(int(part) for part in row[:key_width])
        table[key if key_width > 1 else key[0]] = (
            float(row[key_width]),
            float(row[key_width + 1]),
        )
    return table


@dataclass(frozen=True)
class PriceProfile:
    """Median real prices by local weekday and time of day.

    Every table only holds buckets that saw at least
    :data:`PROFILE_MIN_DAYS` days.

    Attributes:
        quarters: Price pair keyed by ``(weekday, hour, minute)``, where
            ``weekday`` is ``datetime.weekday()`` (Monday = 0) and ``minute``
            is 0, 15, 30 or 45.
        hours: Price pair of the whole hour, keyed by ``(weekday, hour)``.
            Used when the profile lacks the quarters a slot spans.
        any_day_quarters: Price pair keyed by ``(hour, minute)`` over every
            weekday.  Used for a weekday the history has not seen often
            enough.
        any_day_hours: Price pair of the whole hour over every weekday,
            keyed by ``hour``.
        import_level: Level shift of the import price on the last cycle
            that had published prices, or ``None``.
        export_level: The same for the export price.
    """

    quarters: Mapping[tuple[int, int, int], PricePair] = field(default_factory=dict)
    hours: Mapping[tuple[int, int], PricePair] = field(default_factory=dict)
    any_day_quarters: Mapping[tuple[int, int], PricePair] = field(default_factory=dict)
    any_day_hours: Mapping[int, PricePair] = field(default_factory=dict)
    import_level: LevelShift | None = None
    export_level: LevelShift | None = None

    @property
    def is_empty(self) -> bool:
        """Return whether the profile has no bucket at all."""
        return not (
            self.quarters or self.hours or self.any_day_quarters or self.any_day_hours
        )

    @classmethod
    def from_samples(
        cls,
        samples: Iterable[PriceSample],
        zone: tzinfo | None,
        min_days: int = PROFILE_MIN_DAYS,
    ) -> PriceProfile:
        """Build a profile from recorded real prices.

        Each sample is spread over the quarters it spans and each quarter is
        keyed by its own local wall-clock time, so both passes of a DST
        fall-back hour share a key and are averaged into that day's value,
        and an hour the spring-forward day skips never gets one.  A bucket is
        the median over its days.

        Args:
            samples: Recorded slots with timezone-aware starts.
            zone: Local zone the weekday and time of day are read in.
            min_days: Local dates a bucket must have seen to be kept.

        Returns:
            The profile; empty when no bucket has enough days.
        """
        quarters: dict[tuple[int, int, int], dict[Any, list[PricePair]]] = {}
        hours: dict[tuple[int, int], dict[Any, list[PricePair]]] = {}
        any_quarters: dict[tuple[int, int], dict[Any, list[PricePair]]] = {}
        any_hours: dict[int, dict[Any, list[PricePair]]] = {}
        for start, step, import_price, export_price in samples:
            pair = (import_price, export_price)
            for offset in range(0, max(step, 1), PROFILE_STEP_MINUTES):
                local = (start + timedelta(minutes=offset)).astimezone(zone)
                day = local.date()
                weekday, hour = local.weekday(), local.hour
                quarter = local.minute // PROFILE_STEP_MINUTES * PROFILE_STEP_MINUTES
                quarters.setdefault((weekday, hour, quarter), {}).setdefault(
                    day, []
                ).append(pair)
                hours.setdefault((weekday, hour), {}).setdefault(day, []).append(pair)
                any_quarters.setdefault((hour, quarter), {}).setdefault(day, []).append(
                    pair
                )
                any_hours.setdefault(hour, {}).setdefault(day, []).append(pair)
        return cls(
            quarters=_buckets(quarters, min_days),
            hours=_buckets(hours, min_days),
            any_day_quarters=_buckets(any_quarters, min_days),
            any_day_hours=_buckets(any_hours, min_days),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the profile as JSON-safe rows, for the diagnostics dump.

        Returns:
            One list of ``[*key, import, export]`` rows per table, and the
            level shift of each side as ``[shift, ISO time]`` or ``None``.
        """
        return {
            "quarters": _rows(self.quarters),
            "hours": _rows(self.hours),
            "any_day_quarters": _rows(self.any_day_quarters),
            "any_day_hours": _rows(self.any_day_hours),
            "import_level": _level_row(self.import_level),
            "export_level": _level_row(self.export_level),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PriceProfile:
        """Rebuild a profile from :meth:`as_dict` rows."""
        return cls(
            quarters=_table(data.get("quarters"), 3),
            hours=_table(data.get("hours"), 2),
            any_day_quarters=_table(data.get("any_day_quarters"), 2),
            any_day_hours=_table(data.get("any_day_hours"), 1),
            import_level=level_from_row(data.get("import_level")),
            export_level=level_from_row(data.get("export_level")),
        )

    def level_shift(
        self,
        prices: Mapping[datetime, float],
        side: int,
        interval_minutes: int,
        zone: tzinfo | None,
    ) -> float | None:
        """Return the shift that moves one side of the profile to *prices*.

        Args:
            prices: Published price by timezone-aware slot start.
            side: 0 for the import price, 1 for the export price.
            interval_minutes: Slot width in minutes.
            zone: Local zone the weekday and time of day are read in.

        Returns:
            The median difference between the prices and the profile, see
            :func:`median_shift`; ``None`` when too few slots overlap.
        """
        shapes: list[float | None] = []
        for start in prices:
            local = start.astimezone(zone)
            pair = self.lookup(
                local.weekday(), local.hour, local.minute, interval_minutes
            )
            shapes.append(None if pair is None else pair[side])
        return median_shift(prices.values(), shapes, interval_minutes)

    def lookup(
        self, weekday: int, hour: int, minute: int, interval_minutes: int
    ) -> PricePair | None:
        """Return the profile price for a slot, or ``None`` when it has none.

        Args:
            weekday: Local weekday of the slot start (Monday = 0).
            hour: Local wall-clock hour (0-23) of the slot start.
            minute: Local wall-clock minute (0-59) of the slot start.
            interval_minutes: Slot width in minutes.

        Returns:
            The first of these the profile has: the mean of the weekday's
            quarters the slot spans within its hour, the weekday's hour, the
            same quarters over every weekday, the hour over every weekday.
            ``None`` when it has none of them.
        """
        first = minute // PROFILE_STEP_MINUTES * PROFILE_STEP_MINUTES
        last = min(minute + max(interval_minutes, 1), 60)
        spanned = range(first, last, PROFILE_STEP_MINUTES)
        found = [
            self.quarters[key]
            for quarter in spanned
            if (key := (weekday, hour, quarter)) in self.quarters
        ]
        if found:
            return _mean_pair(found)
        if (pair := self.hours.get((weekday, hour))) is not None:
            return pair
        found = [
            self.any_day_quarters[any_key]
            for quarter in spanned
            if (any_key := (hour, quarter)) in self.any_day_quarters
        ]
        if found:
            return _mean_pair(found)
        return self.any_day_hours.get(hour)

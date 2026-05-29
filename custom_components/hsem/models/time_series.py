"""Shared time-series slot model for the HSEM planner (issue #286).

This module provides a single, authoritative time axis for all planner
time-series inputs (prices, PV forecast, load, import/export, battery SoC).

Design goals
------------
- **Single slot index**: every series maps to the same ``SlotKey`` so
  there is no risk of off-by-one or hour-lookup disagreements.
- **Configurable resolution**: default 15-minute slots, any divisor of 60.
- **DST-safe**: slot boundaries are computed via ``timedelta`` arithmetic
  from a UTC-normalised midnight so spring-forward / autumn-fallback days
  always produce the expected number of slots.
- **Explicit missing slots**: ``TimeSeriesIndex.missing_slots`` lists every
  ``SlotKey`` for which at least one series has no data, so callers can
  surface gaps rather than silently defaulting to zero.

Key types
---------
``SlotKey``
    A ``(day_offset, slot_in_day)`` named-tuple.  *day_offset* is the
    number of whole calendar days since the planning midnight; *slot_in_day*
    is the 0-based slot index within that day.  This is unambiguous across
    DST transitions because it does not rely on wall-clock hours.

``TimeSeriesIndex``
    The central alignment object.  Construct it once from the planning
    ``now`` and configuration, then call ``align_*`` methods to project
    each raw series onto the shared slot grid.

Usage example
-------------
>>> from zoneinfo import ZoneInfo
>>> from datetime import datetime
>>> from custom_components.hsem.models.time_series import TimeSeriesIndex
>>>
>>> tz = ZoneInfo("Europe/Copenhagen")
>>> now = datetime(2024, 6, 15, 14, 0, tzinfo=tz)
>>> tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)
>>> len(tsi)  # 24 * 4 = 96 slots
96
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Default slot width in minutes used when none is specified.
DEFAULT_SLOT_MINUTES: int = 15

#: Sentinel value placed in aligned series where source data is absent.
MISSING_SENTINEL: float = float("nan")


# ---------------------------------------------------------------------------
# SlotKey — canonical slot address
# ---------------------------------------------------------------------------


class SlotKey(NamedTuple):
    """Unambiguous address for a single planning slot.

    Attributes:
        day_offset:
            Number of whole calendar days since the planning midnight (0 for
            today, 1 for tomorrow, etc.).
        slot_in_day:
            0-based index of this slot within its calendar day.  For 15-min
            slots there are 96 indices per day (0-95); for 60-min slots there
            are 24 (0-23).
    """

    day_offset: int
    slot_in_day: int


# ---------------------------------------------------------------------------
# SlotMeta — metadata attached to every slot in the index
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotMeta:
    """Immutable metadata for one slot in the shared time axis.

    Attributes:
        key:
            Canonical :class:`SlotKey` for this slot.
        start:
            Timezone-aware start of the slot.
        end:
            Timezone-aware end of the slot (= next slot's ``start``).
        hour:
            Wall-clock hour of ``start`` (0-23).  Provided as a convenience
            for lookups that are naturally keyed by hour (e.g. hourly price
            data, hourly Solcast estimates).
        minute:
            Wall-clock minute of ``start`` (0-59).
        slot_fraction:
            Fraction of an hour occupied by this slot
            (``interval_minutes / 60``).  Multiply an hourly kWh value by
            this to get the per-slot kWh equivalent.
    """

    key: SlotKey
    start: datetime
    end: datetime
    hour: int
    minute: int
    slot_fraction: float


# ---------------------------------------------------------------------------
# TimeSeriesIndex
# ---------------------------------------------------------------------------


@dataclass
class TimeSeriesIndex:
    """Shared time axis for all HSEM planner series.

    All alignment methods project their input onto this axis, producing a
    list aligned 1-to-1 with :attr:`slots`.  Missing entries are filled
    with :data:`MISSING_SENTINEL` (``float("nan")``) so callers can detect
    and handle gaps explicitly.

    Attributes:
        slots:
            Ordered list of :class:`SlotMeta` objects covering the full
            planning horizon, from earliest to latest.
        interval_minutes:
            Slot width in minutes (must be a positive divisor of 60).
        missing_slots:
            Set of :class:`SlotKey` values for which at least one series
            alignment call found no source data.  Populated lazily as
            ``align_*`` methods are called.
        missing_price_slots:
            Subset of :attr:`missing_slots` populated only by
            :meth:`align_hourly_prices`.  Enables callers to distinguish
            missing price data from missing PV data.
        missing_pv_slots:
            Subset of :attr:`missing_slots` populated only by
            :meth:`align_hourly_pv`.  Enables callers to distinguish missing
            PV data from missing price data.
    """

    slots: list[SlotMeta] = field(default_factory=list)
    interval_minutes: int = DEFAULT_SLOT_MINUTES
    missing_slots: set[SlotKey] = field(default_factory=set)
    #: Keys for which price alignment found no source data.
    missing_price_slots: set[SlotKey] = field(default_factory=set)
    #: Keys for which PV alignment found no source data.
    missing_pv_slots: set[SlotKey] = field(default_factory=set)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_now(
        cls,
        now: datetime,
        interval_minutes: int = DEFAULT_SLOT_MINUTES,
        horizon_hours: int = 24,
    ) -> TimeSeriesIndex:
        """Build a :class:`TimeSeriesIndex` anchored at *now*.

        Slots start at midnight of *now*'s calendar day (wall-clock midnight
        in *now*'s timezone) and extend *horizon_hours* into the future.
        Boundaries are computed with ``timedelta`` arithmetic so DST
        transitions never cause gaps, duplicates, or incorrect slot counts.

        Args:
            now:
                Timezone-aware current datetime.  The timezone embedded in
                *now* is used for all slot start/end times.
            interval_minutes:
                Slot width in minutes.  Must be a positive integer that
                divides 60 evenly (e.g. 15, 30, 60).
            horizon_hours:
                Number of hours of slots to generate from midnight.

        Returns:
            Fully populated :class:`TimeSeriesIndex`.

        Raises:
            ValueError: If *now* is naive, *interval_minutes* ≤ 0 or does
                not divide 60 evenly, or *horizon_hours* ≤ 0.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware; got a naive datetime.")
        if interval_minutes <= 0 or 60 % interval_minutes != 0:
            raise ValueError(
                f"interval_minutes must be a positive divisor of 60; got {interval_minutes}."
            )
        if horizon_hours <= 0:
            raise ValueError(f"horizon_hours must be positive; got {horizon_hours}.")

        # Midnight in the same timezone — use timedelta to stay in the same tz
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        slots_per_hour = 60 // interval_minutes
        total_slots = horizon_hours * slots_per_hour
        slot_fraction = interval_minutes / 60.0

        slots: list[SlotMeta] = []
        for i in range(total_slots):
            start = midnight + timedelta(minutes=i * interval_minutes)
            end = midnight + timedelta(minutes=(i + 1) * interval_minutes)
            day_offset = i // (24 * slots_per_hour)
            slot_in_day = i % (24 * slots_per_hour)
            key = SlotKey(day_offset=day_offset, slot_in_day=slot_in_day)
            slots.append(
                SlotMeta(
                    key=key,
                    start=start,
                    end=end,
                    hour=start.hour,
                    minute=start.minute,
                    slot_fraction=slot_fraction,
                )
            )

        return cls(slots=slots, interval_minutes=interval_minutes)

    # ------------------------------------------------------------------
    # Alignment helpers
    # ------------------------------------------------------------------

    def align_hourly_prices(
        self,
        import_prices: dict[int, float] | dict[tuple[int, int], float],
        export_prices: dict[int, float] | dict[tuple[int, int], float],
    ) -> tuple[list[float], list[float]]:
        """Align hourly import and export price dicts onto the slot grid.

        Accepts two key formats:

        - **Hour-only** (``dict[int, float]``): every slot in a given hour
          receives the same value regardless of which day of the horizon it
          falls on.  This is the legacy format and is equivalent to cyclical
          day-0 data.
        - **Day-hour** (``dict[tuple[int, int], float]``): keys are
          ``(day_offset, hour)`` pairs where *day_offset* is the number of
          whole calendar days from the planning midnight (0 = today,
          1 = tomorrow, …).  When this format is detected the lookup uses
          the slot’s ``(key.day_offset, hour)`` first; if that key is absent
          it falls back to ``(0, hour)`` so that callers providing only
          today’s data still produce correct output for day-0 slots.

        Missing hours are filled with :data:`MISSING_SENTINEL` and their
        keys are added to :attr:`missing_slots`.

        Args:
            import_prices:
                Import prices — either ``{hour: price}`` or
                ``{(day_offset, hour): price}``.
            export_prices:
                Export prices — either ``{hour: price}`` or
                ``{(day_offset, hour): price}``.

        Returns:
            ``(aligned_import, aligned_export)`` — two lists, each
            parallel to :attr:`slots`.
        """
        aligned_import: list[float] = []
        aligned_export: list[float] = []

        # Detect whether callers supplied (day_offset, hour) tuple keys.
        _use_day_key = bool(import_prices) and isinstance(
            next(iter(import_prices)), tuple
        )

        for meta in self.slots:
            if _use_day_key:
                day_hour_key = (meta.key.day_offset, meta.hour)
                imp = import_prices.get(day_hour_key)  # type: ignore[call-overload]
                exp = export_prices.get(day_hour_key)  # type: ignore[call-overload]
            else:
                imp = import_prices.get(meta.hour)  # type: ignore[call-overload]
                exp = export_prices.get(meta.hour)  # type: ignore[call-overload]

            if imp is None:
                self.missing_slots.add(meta.key)
                self.missing_price_slots.add(meta.key)
                aligned_import.append(MISSING_SENTINEL)
            else:
                aligned_import.append(imp)

            if exp is None:
                self.missing_slots.add(meta.key)
                self.missing_price_slots.add(meta.key)
                aligned_export.append(MISSING_SENTINEL)
            else:
                aligned_export.append(exp)

        return aligned_import, aligned_export

    def align_hourly_pv(
        self,
        pv_by_hour: dict[int, float] | dict[tuple[int, int], float],
    ) -> list[float]:
        """Align an hourly PV forecast dict onto the slot grid.

        Each hourly kWh value is divided proportionally across the sub-hour
        slots so that the energy sum over the full hour is preserved.

        Accepts two key formats (see :meth:`align_hourly_prices` for details):

        - **Hour-only** (``dict[int, float]``): cyclical same-hour-every-day.
        - **Day-hour** (``dict[tuple[int, int], float]``): per-day per-hour.

        Missing hours are filled with :data:`MISSING_SENTINEL`.

        Args:
            pv_by_hour:
                PV estimates — either ``{hour: kwh}`` or
                ``{(day_offset, hour): kwh}``.

        Returns:
            List of per-slot PV estimates parallel to :attr:`slots`.
        """
        _use_day_key = bool(pv_by_hour) and isinstance(next(iter(pv_by_hour)), tuple)
        aligned: list[float] = []
        for meta in self.slots:
            if _use_day_key:
                key = (meta.key.day_offset, meta.hour)
                hourly_kwh = pv_by_hour.get(key)  # type: ignore[call-overload]
            else:
                hourly_kwh = pv_by_hour.get(meta.hour)  # type: ignore[call-overload]
            if hourly_kwh is None:
                self.missing_slots.add(meta.key)
                self.missing_pv_slots.add(meta.key)
                aligned.append(MISSING_SENTINEL)
            else:
                aligned.append(round(hourly_kwh * meta.slot_fraction, 6))
        return aligned

    def align_hourly_load(
        self,
        load_by_hour: dict[int, float] | dict[tuple[int, int], float],
    ) -> list[float]:
        """Align an hourly load (consumption) dict onto the slot grid.

        Like :meth:`align_hourly_pv`, each hourly kWh value is divided
        proportionally across sub-hour slots.

        Accepts two key formats (see :meth:`align_hourly_prices` for details):

        - **Hour-only** (``dict[int, float]``): cyclical same-hour-every-day.
        - **Day-hour** (``dict[tuple[int, int], float]``): per-day per-hour.

        Missing hours are filled with :data:`MISSING_SENTINEL`.

        Args:
            load_by_hour:
                Load estimates — either ``{hour: kwh}`` or
                ``{(day_offset, hour): kwh}``.

        Returns:
            List of per-slot load estimates parallel to :attr:`slots`.
        """
        _use_day_key = bool(load_by_hour) and isinstance(
            next(iter(load_by_hour)), tuple
        )
        aligned: list[float] = []
        for meta in self.slots:
            if _use_day_key:
                key = (meta.key.day_offset, meta.hour)
                hourly_kwh = load_by_hour.get(key)  # type: ignore[call-overload]
            else:
                hourly_kwh = load_by_hour.get(meta.hour)  # type: ignore[call-overload]
            if hourly_kwh is None:
                self.missing_slots.add(meta.key)
                aligned.append(MISSING_SENTINEL)
            else:
                aligned.append(round(hourly_kwh * meta.slot_fraction, 6))
        return aligned

    def align_net_import_export(
        self,
        import_by_hour: dict[int, float],
        export_by_hour: dict[int, float],
    ) -> tuple[list[float], list[float]]:
        """Align hourly grid import and export energy dicts onto the slot grid.

        Values are divided proportionally across sub-hour slots.

        Missing hours are filled with :data:`MISSING_SENTINEL`.

        Args:
            import_by_hour:
                Dict mapping hour (0-23) to grid import energy in kWh for
                the full hour.
            export_by_hour:
                Dict mapping hour (0-23) to grid export energy in kWh for
                the full hour.

        Returns:
            ``(aligned_import_kwh, aligned_export_kwh)`` — two lists,
            each parallel to :attr:`slots`.
        """
        aligned_import: list[float] = []
        aligned_export: list[float] = []

        for meta in self.slots:
            imp = import_by_hour.get(meta.hour)
            exp = export_by_hour.get(meta.hour)

            if imp is None:
                self.missing_slots.add(meta.key)
                aligned_import.append(MISSING_SENTINEL)
            else:
                aligned_import.append(round(imp * meta.slot_fraction, 6))

            if exp is None:
                self.missing_slots.add(meta.key)
                aligned_export.append(MISSING_SENTINEL)
            else:
                aligned_export.append(round(exp * meta.slot_fraction, 6))

        return aligned_import, aligned_export

    def align_battery_soc(
        self,
        soc_by_hour: dict[int, float],
    ) -> list[float]:
        """Align a battery state-of-charge (SoC) series onto the slot grid.

        SoC is a *state* (%), not an energy flux, so no scaling is applied —
        every slot in the same hour receives the same SoC value.

        Missing hours are filled with :data:`MISSING_SENTINEL`.

        Args:
            soc_by_hour:
                Dict mapping hour (0-23) to battery SoC percentage (0-100).

        Returns:
            List of per-slot SoC values parallel to :attr:`slots`.
        """
        aligned: list[float] = []
        for meta in self.slots:
            soc = soc_by_hour.get(meta.hour)
            if soc is None:
                self.missing_slots.add(meta.key)
                aligned.append(MISSING_SENTINEL)
            else:
                aligned.append(soc)
        return aligned

    def slot_index_for(self, dt: datetime) -> int | None:
        """Return the 0-based position of the slot containing *dt*, or ``None``.

        The search is performed against the UTC-equivalent of each slot's
        ``start``/``end`` so that comparisons are DST-safe.

        Args:
            dt:
                Timezone-aware datetime to locate.

        Returns:
            Integer index into :attr:`slots`, or ``None`` if *dt* falls
            outside the planning horizon.
        """
        if dt.tzinfo is None:
            raise ValueError("dt must be timezone-aware.")
        for i, meta in enumerate(self.slots):
            # Compare via UTC to be DST-safe: convert both sides to UTC first.
            start_utc = meta.start.astimezone(ZoneInfo("UTC"))
            end_utc = meta.end.astimezone(ZoneInfo("UTC"))
            dt_utc = dt.astimezone(ZoneInfo("UTC"))
            if start_utc <= dt_utc < end_utc:
                return i
        return None

    def has_missing(self) -> bool:
        """Return ``True`` if any series alignment found a missing slot."""
        return bool(self.missing_slots)

    def missing_hours(self) -> set[int]:
        """Return the wall-clock hours (0-23) that have at least one missing slot."""
        key_to_hour: dict[SlotKey, int] = {m.key: m.hour for m in self.slots}
        return {key_to_hour[key] for key in self.missing_slots if key in key_to_hour}

    def missing_tomorrow_price_hours(self) -> set[int]:
        """Return wall-clock hours (0-23) in ``day_offset=1`` that lack price data.

        Returns an empty set when the planning horizon does not include tomorrow
        (i.e. ``horizon_hours`` ≤ 24) or when all tomorrow price hours are present.

        Returns:
            Set of integer hours (0-23) from tomorrow that have no price data.
        """
        return self.missing_future_day_price_hours(1)

    def missing_tomorrow_pv_hours(self) -> set[int]:
        """Return wall-clock hours (0-23) in ``day_offset=1`` that lack PV data.

        Returns an empty set when the planning horizon does not include tomorrow
        (i.e. ``horizon_hours`` ≤ 24) or when all tomorrow PV hours are present.

        Returns:
            Set of integer hours (0-23) from tomorrow that have no PV forecast data.
        """
        return self.missing_future_day_pv_hours(1)

    def missing_future_day_price_hours(self, day_offset: int) -> set[int]:
        """Return wall-clock hours (0-23) for *day_offset* that lack price data.

        Generalised version of :meth:`missing_tomorrow_price_hours` that works for
        any day in the planning horizon (e.g. day 1 = tomorrow, day 2 = day after
        tomorrow, etc.).

        Args:
            day_offset:
                0-based offset from today (0 = today, 1 = tomorrow, 2 = day+2, …).

        Returns:
            Set of integer hours (0-23) for the requested day that have no price data.
            Empty when the horizon does not include that day, or when data is complete.
        """
        key_to_hour: dict[SlotKey, int] = {m.key: m.hour for m in self.slots}
        return {
            key_to_hour[key]
            for key in self.missing_price_slots
            if key in key_to_hour and key.day_offset == day_offset
        }

    def missing_future_day_pv_hours(self, day_offset: int) -> set[int]:
        """Return wall-clock hours (0-23) for *day_offset* that lack PV data.

        Generalised version of :meth:`missing_tomorrow_pv_hours` that works for
        any day in the planning horizon (e.g. day 1 = tomorrow, day 2 = day after
        tomorrow, etc.).

        Args:
            day_offset:
                0-based offset from today (0 = today, 1 = tomorrow, 2 = day+2, …).

        Returns:
            Set of integer hours (0-23) for the requested day that have no PV data.
            Empty when the horizon does not include that day, or when data is complete.
        """
        key_to_hour: dict[SlotKey, int] = {m.key: m.hour for m in self.slots}
        return {
            key_to_hour[key]
            for key in self.missing_pv_slots
            if key in key_to_hour and key.day_offset == day_offset
        }

    def has_tomorrow_slots(self) -> bool:
        """Return ``True`` when the planning horizon includes tomorrow (day_offset=1).

        Returns:
            ``True`` if at least one slot has ``day_offset == 1``.
        """
        return self.has_day_slots(1)

    def has_day_slots(self, day_offset: int) -> bool:
        """Return ``True`` when the planning horizon includes *day_offset*.

        Args:
            day_offset:
                0-based day offset (0 = today, 1 = tomorrow, 2 = day+2, …).

        Returns:
            ``True`` if at least one slot has the given ``day_offset``.
        """
        return any(m.key.day_offset == day_offset for m in self.slots)

    @property
    def horizon_days(self) -> int:
        """Return the number of distinct calendar days covered by this index.

        A 24-hour index anchored at midnight returns 1; a 48-hour index
        returns 2; a 72-hour index returns 3.

        Returns:
            Integer count of unique ``day_offset`` values in the slot list.
        """
        return len({m.key.day_offset for m in self.slots})

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        """Return the total number of slots in the index."""
        return len(self.slots)

    def __iter__(self) -> Iterator[SlotMeta]:
        """Iterate over :class:`SlotMeta` objects in chronological order."""
        return iter(self.slots)

    def __repr__(self) -> str:
        first = self.slots[0].start.isoformat() if self.slots else "empty"
        last = self.slots[-1].end.isoformat() if self.slots else "empty"
        return (
            f"TimeSeriesIndex("
            f"slots={len(self.slots)}, "
            f"interval_minutes={self.interval_minutes}, "
            f"range=[{first}, {last}])"
        )

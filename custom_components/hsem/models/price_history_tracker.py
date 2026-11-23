"""Persisted history of real electricity prices (issue #1434).

HSEM only knows the prices its price sensors expose right now.  This tracker
keeps two things across cycles and restarts:

* **History** — the real import and export price of every slot that has
  started, for a rolling window.  It feeds the weekday and time-of-day
  :class:`~custom_components.hsem.models.price_profile.PriceProfile` the
  planner uses for slots without a published price.
* **Held prices** — the prices the price sensors published on the last cycle
  they delivered any, kept per side.  When a price sensor goes down, the
  slots these cover are still planned with their published price.
* **Level shifts** — how far each side's published prices were from the
  profile on the last cycle that had any.  The planner uses it for an outage
  that outlasts the held prices (issue #1452).

The store belongs to one price source: the price entities and their units.
When that changes, the recorded prices are on another scale and everything
is dropped.

Only prices the price sensors published are ever passed in; a forecast
sensor's predictions are not.  An estimate (profile,
nearest-earlier-day or the ``0.0`` fallback) is made inside the planner and
never reaches this tracker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from custom_components.hsem.models.price_profile import (
    LEVEL_SHIFT_MAX_AGE,
    LevelShift,
    PricePair,
    PriceProfile,
    level_from_row,
)
from custom_components.hsem.utils.datetime_utils import utc_key
from custom_components.hsem.utils.persistence import (
    async_load_history,
    aware_datetime_from_iso,
    finite_float,
    write_json_history_file,
)

#: Rolling window of recorded prices: eight samples per weekday.
PRICE_HISTORY_RETENTION_DAYS = 56

#: Longest a price outage is planned from held and stored prices.  After
#: that the cycle reports the price data as missing and the planner stops.
PRICE_OUTAGE_MAX_HOURS = 48

_SCHEMA_VERSION = 2
_MAX_STEP_MINUTES = 60
#: A held price is kept this long after its slot started: the slots of a
#: cycle begin at local midnight, and a DST day has 25 hours.
_HELD_KEEP_HOURS = 26
_PRICE_EPSILON = 1e-9
_SIDES = ("import", "export")
_SOURCE_KEYS = ("import_entity", "export_entity", "import_unit", "export_unit")


def _same_pair(first: PricePair, second: PricePair) -> bool:
    """Return whether two price pairs are equal within float tolerance."""
    return (
        abs(first[0] - second[0]) < _PRICE_EPSILON
        and abs(first[1] - second[1]) < _PRICE_EPSILON
    )


def _parse_pair(import_raw: Any, export_raw: Any) -> PricePair | None:
    """Return a finite price pair, or ``None`` when either side is invalid."""
    import_price = finite_float(import_raw)
    export_price = finite_float(export_raw)
    if import_price is None or export_price is None:
        return None
    return import_price, export_price


def _parse_prices(raw: Any) -> dict[datetime, float]:
    """Return persisted ``{ISO start: price}`` rows that pass validation."""
    prices: dict[datetime, float] = {}
    for raw_start, raw_price in (raw if isinstance(raw, dict) else {}).items():
        start = aware_datetime_from_iso(raw_start)
        price = finite_float(raw_price)
        if start is not None and price is not None:
            prices[start.astimezone(UTC)] = price
    return prices


@dataclass
class PriceHistoryTracker:
    """Rolling store of real prices plus the last published prices.

    Attributes:
        history_file: Path of the JSON persistence file; empty until the
            coordinator sets it.
        retention_days: Age in days after which a recorded price is dropped.
        source: The price source the store belongs to: the import and export
            price entity and their units.
        outage_since: When the current price outage began, or ``None``.
    """

    history_file: str = ""
    retention_days: int = PRICE_HISTORY_RETENTION_DAYS
    source: dict[str, str] = field(default_factory=dict)
    outage_since: datetime | None = None
    _samples: dict[datetime, tuple[int, float, float]] = field(
        default_factory=dict, repr=False
    )
    _held_import: dict[datetime, float] = field(default_factory=dict, repr=False)
    _held_export: dict[datetime, float] = field(default_factory=dict, repr=False)
    _levels: dict[str, LevelShift] = field(default_factory=dict, repr=False)
    _dirty: bool = field(default=False, repr=False)
    _profile_cache: tuple[tzinfo | None, PriceProfile] | None = field(
        default=None, repr=False
    )

    @property
    def sample_count(self) -> int:
        """Return the number of recorded slots in the history."""
        return len(self._samples)

    def set_source(self, source: Mapping[str, str]) -> bool:
        """Bind the store to a price source and drop it when that changed.

        A changed entity always counts.  A unit only counts when both the
        stored and the new unit are known: an unavailable sensor reports no
        unit, and an outage must not wipe the history it is needed for.

        Args:
            source: ``import_entity``, ``export_entity``, ``import_unit`` and
                ``export_unit``; an unknown value is an empty string.

        Returns:
            ``True`` when the stored prices were dropped.
        """
        new = {key: str(source.get(key) or "") for key in _SOURCE_KEYS}
        changed = self.source_differs(source)
        had_data = bool(self._samples or self._held_import or self._held_export)
        merged = {
            key: new[key] or ("" if changed else self.source.get(key, ""))
            for key in _SOURCE_KEYS
        }
        if merged != {key: self.source.get(key, "") for key in _SOURCE_KEYS}:
            self._dirty = True
        self.source = merged
        if not changed or not had_data:
            return False
        self._samples.clear()
        self._held_import.clear()
        self._held_export.clear()
        self._levels.clear()
        self.outage_since = None
        self._profile_cache = None
        return True

    def source_differs(self, source: Mapping[str, str]) -> bool:
        """Return whether *source* is another price source than the stored one.

        The rule of :meth:`set_source`: a changed entity always counts, a unit
        only when both the stored and the new unit are known.

        Args:
            source: ``import_entity``, ``export_entity``, ``import_unit`` and
                ``export_unit``; an unknown value is an empty string.
        """
        new = {key: str(source.get(key) or "") for key in _SOURCE_KEYS}
        return any(
            self.source.get(key, "") != new[key]
            and (key.endswith("_entity") or bool(self.source.get(key) and new[key]))
            for key in _SOURCE_KEYS
        )

    def record(
        self,
        start: datetime,
        interval_minutes: int,
        import_price: float,
        export_price: float,
    ) -> None:
        """Record the real price of one slot; the latest value wins.

        Args:
            start: Timezone-aware slot start.
            interval_minutes: Slot width in minutes.
            import_price: Published import price.
            export_price: Published export price.
        """
        key = utc_key(start)
        sample = (int(interval_minutes), float(import_price), float(export_price))
        previous = self._samples.get(key)
        if (
            previous is not None
            and previous[0] == sample[0]
            and _same_pair(previous[1:], sample[1:])
        ):
            return
        self._samples[key] = sample
        self._dirty = True
        self._profile_cache = None

    def prune(self, now: datetime) -> None:
        """Drop recorded prices past retention and held prices that are over.

        A held price is kept while its slot can still be part of a cycle's
        slots, which begin at local midnight.
        """
        now_key = utc_key(now)
        cutoff = now_key - timedelta(days=self.retention_days)
        stale = [key for key in self._samples if key < cutoff]
        for key in stale:
            del self._samples[key]
        if stale:
            self._profile_cache = None
        held_cutoff = now_key - timedelta(hours=_HELD_KEEP_HOURS)
        over = 0
        for held in (self._held_import, self._held_export):
            for key in [key for key in held if key < held_cutoff]:
                del held[key]
                over += 1
        if stale or over:
            self._dirty = True

    def hold(self, side: str, prices: Mapping[datetime, float]) -> None:
        """Keep the prices one side published this cycle.

        Published slots replace what was held for them and the rest stays,
        so a price published earlier survives until its slot is over.  The
        file is rewritten only when a slot is new, not when a source revises
        a value.

        Args:
            side: ``"import"`` or ``"export"``.
            prices: Published price by UTC slot start.
        """
        held = self._held_import if side == "import" else self._held_export
        if not prices.keys() <= held.keys():
            self._dirty = True
        held.update(prices)

    def anchor_level(
        self,
        side: str,
        prices: Mapping[datetime, float],
        interval_minutes: int,
        zone: tzinfo | None,
    ) -> None:
        """Keep how far one side's published prices are from the profile.

        The shift is kept until the next cycle that can derive one.  It is
        valid for slots that start within
        :data:`~custom_components.hsem.models.price_profile.LEVEL_SHIFT_MAX_AGE`
        after the last published slot ended.

        Args:
            side: ``"import"`` or ``"export"``.
            prices: Published price by UTC slot start; not empty.
            interval_minutes: Slot width in minutes.
            zone: Local zone the profile is read in.
        """
        shift = self._bucket_profile(zone).level_shift(
            prices, _SIDES.index(side), interval_minutes, zone
        )
        if shift is None:
            return
        until = max(prices) + timedelta(minutes=interval_minutes) + LEVEL_SHIFT_MAX_AGE
        previous = self._levels.get(side)
        if (
            previous is None
            or previous[1] != until
            or abs(previous[0] - shift) >= _PRICE_EPSILON
        ):
            self._levels[side] = (shift, until)
            self._dirty = True

    def held_price(self, side: str, start: datetime) -> float | None:
        """Return the last published price of one side for a slot.

        Args:
            side: ``"import"`` or ``"export"``.
            start: Timezone-aware slot start.
        """
        held = self._held_import if side == "import" else self._held_export
        return held.get(utc_key(start))

    def mark_outage(self, now: datetime, active: bool) -> float:
        """Track how long the price sensors have been down.

        Args:
            now: Current time.
            active: Whether a price sensor publishes nothing this cycle.

        Returns:
            Hours the outage has lasted; ``0.0`` without one.
        """
        if not active:
            if self.outage_since is not None:
                self.outage_since = None
                self._dirty = True
            return 0.0
        if self.outage_since is None:
            self.outage_since = utc_key(now)
            self._dirty = True
        return max((utc_key(now) - self.outage_since).total_seconds() / 3600.0, 0.0)

    def profile(self, zone: tzinfo | None) -> PriceProfile:
        """Return the weekday and time-of-day profile of the history.

        Args:
            zone: Local zone the weekday and time of day are read in.

        Returns:
            The profile, cached until the history or the zone changes, with
            the level shifts kept by :meth:`anchor_level`.
        """
        return replace(
            self._bucket_profile(zone),
            import_level=self._levels.get("import"),
            export_level=self._levels.get("export"),
        )

    def _bucket_profile(self, zone: tzinfo | None) -> PriceProfile:
        """Return the cached profile of the history, without level shifts."""
        cached = self._profile_cache
        if cached is not None and cached[0] is zone:
            return cached[1]
        profile = PriceProfile.from_samples(
            (
                (start, step, import_price, export_price)
                for start, (step, import_price, export_price) in self._samples.items()
            ),
            zone,
        )
        self._profile_cache = (zone, profile)
        return profile

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable state."""
        return {
            "version": _SCHEMA_VERSION,
            "source": dict(self.source),
            "outage_since": (
                self.outage_since.isoformat() if self.outage_since else None
            ),
            "samples": {
                start.isoformat(): [step, import_price, export_price]
                for start, (step, import_price, export_price) in sorted(
                    self._samples.items()
                )
            },
            "held_import": {
                start.isoformat(): price
                for start, price in sorted(self._held_import.items())
            },
            "held_export": {
                start.isoformat(): price
                for start, price in sorted(self._held_export.items())
            },
            "levels": {
                side: [shift, until.isoformat()]
                for side, (shift, until) in sorted(self._levels.items())
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PriceHistoryTracker:
        """Rebuild a tracker from persisted state, skipping invalid entries.

        Args:
            data: Parsed JSON object written by :meth:`as_dict`.

        Returns:
            A tracker holding every entry that passed validation.  A file of
            another schema version gives an empty tracker: its prices cannot
            be tied to a price source.
        """
        tracker = cls()
        if data.get("version") != _SCHEMA_VERSION:
            return tracker
        source = data.get("source")
        if isinstance(source, dict):
            tracker.source = {key: str(source.get(key) or "") for key in _SOURCE_KEYS}
        outage_since = data.get("outage_since")
        if isinstance(outage_since, str):
            parsed = aware_datetime_from_iso(outage_since)
            tracker.outage_since = parsed.astimezone(UTC) if parsed else None
        samples = data.get("samples")
        for raw_start, raw in (samples if isinstance(samples, dict) else {}).items():
            start = aware_datetime_from_iso(raw_start)
            if start is None or not isinstance(raw, list) or len(raw) != 3:
                continue
            step = raw[0]
            pair = _parse_pair(raw[1], raw[2])
            if (
                pair is None
                or isinstance(step, bool)
                or not isinstance(step, int)
                or not 0 < step <= _MAX_STEP_MINUTES
            ):
                continue
            tracker._samples[start.astimezone(UTC)] = (step, pair[0], pair[1])
        tracker._held_import = _parse_prices(data.get("held_import"))
        tracker._held_export = _parse_prices(data.get("held_export"))
        levels = data.get("levels")
        for side in _SIDES:
            level = level_from_row(
                levels.get(side) if isinstance(levels, dict) else None
            )
            if level is not None:
                tracker._levels[side] = (level[0], level[1].astimezone(UTC))
        return tracker

    def restore_from(self, loaded: PriceHistoryTracker) -> None:
        """Take over the state of a reconstructed tracker."""
        self.source = dict(loaded.source)
        self.outage_since = loaded.outage_since
        self._samples = dict(loaded._samples)
        self._held_import = dict(loaded._held_import)
        self._held_export = dict(loaded._held_export)
        self._levels = dict(loaded._levels)
        self._dirty = False
        self._profile_cache = None

    async def load_history(self) -> None:
        """Restore the tracker from its history file, if there is one."""
        if not self.history_file:
            return
        await async_load_history(
            Path(self.history_file),
            lambda data: self.restore_from(PriceHistoryTracker.from_dict(data)),
        )

    async def save_history(self) -> bool:
        """Persist the state atomically when it changed since the last write.

        Returns:
            ``True`` when the file is up to date, ``False`` when the write
            failed or no history file is set.
        """
        if not self.history_file:
            return False
        if not self._dirty:
            return True
        written = await asyncio.to_thread(
            write_json_history_file, Path(self.history_file), self.as_dict()
        )
        if written:
            self._dirty = False
        return written

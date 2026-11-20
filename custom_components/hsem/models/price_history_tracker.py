"""Persisted history of real electricity prices (issue #1434).

HSEM only knows the prices its price sensors expose right now.  This tracker
keeps two things across cycles and restarts:

* **History** — the real import and export price of every slot that has
  started, for a rolling window.  It feeds the weekday and time-of-day
  :class:`~custom_components.hsem.models.price_profile.PriceProfile` the
  planner uses for slots without a published price.
* **Held prices** — the prices the sources published on the last cycle they
  delivered any.  When the price sensors go down, the slots these cover are
  still planned with their published price.

Only prices a source published are ever passed in.  An estimate (profile,
nearest-earlier-day or the ``0.0`` fallback) is made inside the planner and
never reaches this tracker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from custom_components.hsem.models.price_profile import PricePair, PriceProfile
from custom_components.hsem.utils.datetime_utils import utc_key
from custom_components.hsem.utils.persistence import (
    aware_datetime_from_iso,
    finite_float,
    write_json_history_file,
)

#: Rolling window of recorded prices: eight samples per weekday.
PRICE_HISTORY_RETENTION_DAYS = 56

_SCHEMA_VERSION = 1
_MAX_STEP_MINUTES = 60
_PRICE_EPSILON = 1e-9


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


@dataclass
class PriceHistoryTracker:
    """Rolling store of real prices plus the last published prices.

    Attributes:
        history_file: Path of the JSON persistence file; empty until the
            coordinator sets it.
        retention_days: Age in days after which a recorded price is dropped.
    """

    history_file: str = ""
    retention_days: int = PRICE_HISTORY_RETENTION_DAYS
    _samples: dict[datetime, tuple[int, float, float]] = field(
        default_factory=dict, repr=False
    )
    _held: dict[datetime, PricePair] = field(default_factory=dict, repr=False)
    _dirty: bool = field(default=False, repr=False)
    _profile_cache: tuple[tzinfo | None, PriceProfile] | None = field(
        default=None, repr=False
    )

    @property
    def sample_count(self) -> int:
        """Return the number of recorded slots in the history."""
        return len(self._samples)

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
        """Drop recorded prices older than the retention window."""
        cutoff = utc_key(now) - timedelta(days=self.retention_days)
        stale = [key for key in self._samples if key < cutoff]
        for key in stale:
            del self._samples[key]
        if stale:
            self._dirty = True
            self._profile_cache = None

    def hold(self, prices: Mapping[datetime, PricePair]) -> None:
        """Replace the held prices with the ones published this cycle.

        The file is rewritten only when a slot is new, not when a source
        revises a value, so a forecast sensor that updates every few minutes
        does not cause a write per cycle.

        Args:
            prices: Published price pair by timezone-aware slot start.
        """
        held = {utc_key(start): pair for start, pair in prices.items()}
        if not held.keys() <= self._held.keys():
            self._dirty = True
        self._held = held

    def held_price(self, start: datetime) -> PricePair | None:
        """Return the last published price of the slot starting at *start*."""
        return self._held.get(utc_key(start))

    def profile(self, zone: tzinfo | None) -> PriceProfile:
        """Return the weekday and time-of-day profile of the history.

        Args:
            zone: Local zone the weekday and time of day are read in.

        Returns:
            The profile, cached until the history or the zone changes.
        """
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
            "samples": {
                start.isoformat(): [step, import_price, export_price]
                for start, (step, import_price, export_price) in sorted(
                    self._samples.items()
                )
            },
            "held": {
                start.isoformat(): [import_price, export_price]
                for start, (import_price, export_price) in sorted(self._held.items())
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PriceHistoryTracker:
        """Rebuild a tracker from persisted state, skipping invalid entries.

        Args:
            data: Parsed JSON object written by :meth:`as_dict`.

        Returns:
            A tracker holding every entry that passed validation.
        """
        tracker = cls()
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
        held = data.get("held")
        for raw_start, raw in (held if isinstance(held, dict) else {}).items():
            start = aware_datetime_from_iso(raw_start)
            if start is None or not isinstance(raw, list) or len(raw) != 2:
                continue
            pair = _parse_pair(raw[0], raw[1])
            if pair is not None:
                tracker._held[start.astimezone(UTC)] = pair
        return tracker

    def restore_from(self, loaded: PriceHistoryTracker) -> None:
        """Take over the history and held prices of a reconstructed tracker."""
        self._samples = dict(loaded._samples)
        self._held = dict(loaded._held)
        self._dirty = False
        self._profile_cache = None

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

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
between the two does not skew the profile.  Pure — no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any

#: Width of one profile bucket in minutes.
PROFILE_STEP_MINUTES = 15

#: ``(import price, export price)`` in local currency per kWh.
PricePair = tuple[float, float]

#: One recorded slot: ``(start, slot width in minutes, import, export)``.
PriceSample = tuple[datetime, int, float, float]


def _mean_pair(pairs: list[PricePair]) -> PricePair:
    """Return the per-side mean of *pairs*; a single pair is returned as is."""
    if len(pairs) == 1:
        return pairs[0]
    count = len(pairs)
    return (
        sum(pair[0] for pair in pairs) / count,
        sum(pair[1] for pair in pairs) / count,
    )


@dataclass(frozen=True)
class PriceProfile:
    """Mean real prices by local weekday and time of day.

    Attributes:
        quarters: Mean price pair keyed by ``(weekday, hour, minute)``, where
            ``weekday`` is ``datetime.weekday()`` (Monday = 0) and ``minute``
            is 0, 15, 30 or 45.
        hours: Mean price pair of every sample in the hour, keyed by
            ``(weekday, hour)``.  Used when the profile lacks the quarters a
            slot spans.
    """

    quarters: Mapping[tuple[int, int, int], PricePair] = field(default_factory=dict)
    hours: Mapping[tuple[int, int], PricePair] = field(default_factory=dict)

    @classmethod
    def from_samples(
        cls, samples: Iterable[PriceSample], zone: tzinfo | None
    ) -> PriceProfile:
        """Build a profile from recorded real prices.

        Each sample is spread over the quarters it spans and each quarter is
        keyed by its own local wall-clock time, so both passes of a DST
        fall-back hour share a key and are averaged, and an hour the
        spring-forward day skips never gets one.

        Args:
            samples: Recorded slots with timezone-aware starts.
            zone: Local zone the weekday and time of day are read in.

        Returns:
            The profile; empty when *samples* is empty.
        """
        by_quarter: dict[tuple[int, int, int], list[PricePair]] = {}
        by_hour: dict[tuple[int, int], list[PricePair]] = {}
        for start, step, import_price, export_price in samples:
            for offset in range(0, max(step, 1), PROFILE_STEP_MINUTES):
                local = (start + timedelta(minutes=offset)).astimezone(zone)
                quarter = local.minute // PROFILE_STEP_MINUTES * PROFILE_STEP_MINUTES
                pair = (import_price, export_price)
                by_quarter.setdefault(
                    (local.weekday(), local.hour, quarter), []
                ).append(pair)
                by_hour.setdefault((local.weekday(), local.hour), []).append(pair)
        return cls(
            quarters={key: _mean_pair(pairs) for key, pairs in by_quarter.items()},
            hours={key: _mean_pair(pairs) for key, pairs in by_hour.items()},
        )

    def as_dict(self) -> dict[str, list[list[float]]]:
        """Return the profile as JSON-safe rows, for the diagnostics dump.

        Returns:
            ``quarters`` rows of ``[weekday, hour, minute, import, export]``
            and ``hours`` rows of ``[weekday, hour, import, export]``.
        """
        return {
            "quarters": [[*key, *pair] for key, pair in sorted(self.quarters.items())],
            "hours": [[*key, *pair] for key, pair in sorted(self.hours.items())],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PriceProfile:
        """Rebuild a profile from :meth:`as_dict` rows."""
        return cls(
            quarters={
                (int(row[0]), int(row[1]), int(row[2])): (float(row[3]), float(row[4]))
                for row in data.get("quarters", [])
            },
            hours={
                (int(row[0]), int(row[1])): (float(row[2]), float(row[3]))
                for row in data.get("hours", [])
            },
        )

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
            The mean of the quarters the slot spans within its hour, the
            hour's mean when none of them is known, or ``None`` when the
            profile has nothing for that weekday and hour.
        """
        first = minute // PROFILE_STEP_MINUTES * PROFILE_STEP_MINUTES
        last = min(minute + max(interval_minutes, 1), 60)
        found = [
            self.quarters[key]
            for quarter in range(first, last, PROFILE_STEP_MINUTES)
            if (key := (weekday, hour, quarter)) in self.quarters
        ]
        if found:
            return _mean_pair(found)
        return self.hours.get((weekday, hour))

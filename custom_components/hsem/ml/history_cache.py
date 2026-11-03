"""Per-entity energy history cache with incremental recorder refresh.

A sufficient energy history used to be re-read in full every
``_MIN_HISTORY_REFRESH``: 24 reads a day of the whole 90-day window per
energy entity, each one 700K rows and seconds of recorder time on a busy
meter (#1355).  History only gains a few hundred rows an hour, so this
module keeps the processed slot samples per entity together with the last
reading of the newest completed slot and continues from there.

A full read still happens once a day, after a restart (the cache is in
memory), when the cache key changes and when the entity's unit changes, so
recorder corrections, purges and meter resets are picked up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant

from custom_components.hsem.ml.history_reader import HistoryReader, HistoryResult
from custom_components.hsem.utils.datetime_utils import cache_is_fresh, utc_key
from custom_components.hsem.utils.logger import HSEM_LOGGER

type EnergySample = tuple[datetime, int, float]
type _EnergyCacheKey = tuple[int, str, int]

# How long the processed samples are extended incrementally before the whole
# window is read again.
FULL_READ_INTERVAL = timedelta(days=1)


@dataclass(slots=True)
class UnitTracker:
    """Remember which unit a history was read in and when that changed.

    An attribute-free read assumes one unit for every state in the window.
    After the entity's unit changes (Wh to kWh, °F to °C) the window keeps
    states in the old unit for ``max_days``; reads in that time must use
    each state's own attributes (#1355).

    Attributes:
        unit: The unit assumed by the last read.
        attributes_until: While ``now`` is before this instant, reads must
            join the state attributes.  ``None`` when no change is known.
    """

    unit: str | None
    attributes_until: datetime | None = None

    def needs_attributes(self, unit: str | None, now: datetime, max_days: int) -> bool:
        """Record the entity's current *unit* and say whether attributes are needed.

        Args:
            unit: The unit the entity reports in now.
            now: The current time.
            max_days: The widest history window any read fetches.

        Returns:
            ``True`` when the window may still contain states in another
            unit than *unit*.
        """
        if unit != self.unit:
            HSEM_LOGGER.info(
                "ML history: unit changed from %s to %s; reading state "
                "attributes for the next %d days.",
                self.unit,
                unit,
                max_days,
            )
            self.unit = unit
            self.attributes_until = utc_key(now) + timedelta(days=max_days)
        return (
            self.attributes_until is not None and utc_key(now) < self.attributes_until
        )


@dataclass(slots=True)
class _CachedEnergyHistory:
    """One entity's processed energy history and where to continue from."""

    full_read_at: datetime
    samples: list[EnergySample]
    anchor: tuple[datetime, float] | None
    units: UnitTracker


_energy_history_cache: dict[_EnergyCacheKey, _CachedEnergyHistory] = {}


def history_ready_at(history: list[EnergySample], min_days: int) -> datetime | None:
    """Return when *history*'s oldest sample will be ``min_days`` old.

    Args:
        history: Processed history samples in any order.
        min_days: The configured minimum history span in days.

    Returns:
        The UTC instant, or ``None`` when the history is empty.
    """
    if not history:
        return None
    oldest = min(utc_key(timestamp) for timestamp, _slot, _energy in history)
    return oldest + timedelta(days=min_days)


async def get_energy_history(
    hass: HomeAssistant,
    reader: HistoryReader,
    entity_id: str,
    *,
    min_days: int,
    max_days: int,
    slot_minutes: int,
    expected_unit: str | None,
    now_ts: datetime,
) -> HistoryResult[EnergySample]:
    """Return *entity_id*'s per-slot energy history, read incrementally.

    The first call and the first call of every day read the whole
    ``max_days`` window; calls in between read only the states after the
    cached anchor and append the new slots.  A unit change forces a full
    read with state attributes (see :class:`UnitTracker`).

    Args:
        hass: The Home Assistant instance.
        reader: The recorder history reader.
        entity_id: The energy accumulator entity.
        min_days: Minimum span in days before the history is usable.
        max_days: Width of the window a full read fetches.
        slot_minutes: Slot width in minutes.
        expected_unit: HSEM's canonical unit for the entity, or ``None``
            to skip normalization.
        now_ts: The current time.

    Returns:
        A :class:`HistoryResult` whose samples span at least ``min_days``;
        otherwise empty samples with ``ready_at`` set when it is known.
    """
    key: _EnergyCacheKey = (id(hass), entity_id, slot_minutes)
    entry = _energy_history_cache.get(key)
    unit = await reader.resolve_unit(entity_id) if expected_unit is not None else None

    if (
        entry is not None
        and entry.anchor is not None
        and unit == entry.units.unit
        and cache_is_fresh(entry.full_read_at, now_ts, FULL_READ_INTERVAL)
    ):
        result = await reader.read_energy_history(
            entity_id=entity_id,
            days=min_days,
            max_days=max_days,
            slot_minutes=slot_minutes,
            expected_unit=expected_unit,
            since=entry.anchor[0],
            with_attributes=entry.units.needs_attributes(unit, now_ts, max_days),
            source_unit=unit,
        )
        cutoff = utc_key(now_ts) - timedelta(days=max_days)
        entry.samples = [
            sample for sample in entry.samples if utc_key(sample[0]) >= cutoff
        ] + result.samples
        if result.anchor is not None:
            entry.anchor = result.anchor
        HSEM_LOGGER.debug(
            "ML history: %s extended by %d slots (%d cached).",
            entity_id,
            len(result.samples),
            len(entry.samples),
        )
        return _usable_or_ready(entity_id, entry.samples, min_days, now_ts)

    with_attributes = (
        entry.units.needs_attributes(unit, now_ts, max_days)
        if entry is not None
        else False
    )
    result = await reader.read_energy_history(
        entity_id=entity_id,
        days=min_days,
        max_days=max_days,
        slot_minutes=slot_minutes,
        expected_unit=expected_unit,
        with_attributes=with_attributes,
        source_unit=unit,
    )
    if not result.samples:
        _energy_history_cache.pop(key, None)
        return result

    _energy_history_cache[key] = _CachedEnergyHistory(
        full_read_at=now_ts,
        samples=result.samples,
        anchor=result.anchor,
        units=entry.units if entry is not None else UnitTracker(unit),
    )
    return HistoryResult(result.samples)


def _usable_or_ready(
    entity_id: str,
    samples: list[EnergySample],
    min_days: int,
    now_ts: datetime,
) -> HistoryResult[EnergySample]:
    """Return *samples* when they span ``min_days``, else when they will."""
    ready_at = history_ready_at(samples, min_days)
    if ready_at is not None and ready_at > utc_key(now_ts):
        HSEM_LOGGER.info(
            "ML history: cached history for %s is shorter than %d days; "
            "enough history is expected at %s.",
            entity_id,
            min_days,
            ready_at.isoformat(),
        )
        return HistoryResult(ready_at=ready_at)
    return HistoryResult(samples)

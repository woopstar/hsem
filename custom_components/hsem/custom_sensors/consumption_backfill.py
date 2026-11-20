"""Backfill of the hour-block consumption averages from long-term statistics.

The :class:`~custom_components.hsem.custom_sensors.avg_sensor.HSEMAvgSensor`
windows only fill while HSEM runs, one day at a time.  Home Assistant keeps
hourly long-term statistics for the house power sensor from before HSEM was
installed, so the days HSEM did not observe can be taken from there
(issue #1433).

The profile is built the way the live path measures it
(:class:`~custom_components.hsem.custom_sensors.house_consumption_power_sensor.HSEMHouseConsumptionPowerSensor`):

- The hourly mean of the house power source, minus both EV chargers when the
  house power includes them.
- Values are read in each sensor's own unit, without conversion, exactly as
  the live path reads the states.  Measured and backfilled days in one window
  are therefore always on the same scale.
- A charger hour without statistics counts as 0 W, as an unavailable charger
  does live, but only from the charger's first statistics on.  Before that
  the charger is unknown: the car may have charged without the sensor
  existing, so those hours are left out.  The first statistics are looked
  for up to :data:`CHARGER_LOOKBACK` before the read window.  A charger
  without any statistics makes every hour unknown, so nothing is backfilled.
- The source is the configured house sensor, whatever its current state, so
  a sensor that is slow at start-up is not replaced by the derived house
  load.

An hour is left out when the house source has no statistics for it and when
the result is negative.  Nothing is backfilled when the Home Assistant time
zone is not a whole number of hours from UTC (statistics hours would not
line up with the hour blocks).

All 96 average sensors of a config entry share one read, refreshed at most
once an hour.  A failed read is logged once and shared the same way, so the
sensors keep updating and the recorder is not asked again until the next
refresh.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.hsem.ml.statistics_reader import StatisticsReader
from custom_components.hsem.utils.datetime_utils import cache_is_fresh
from custom_components.hsem.utils.house_load import configured_house_power_entity
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.misc import get_config_value, optional_entity_id

#: Days of statistics read: the longest average window plus the current day.
BACKFILL_DAYS = 15

#: How long a read is shared before the statistics are queried again.  Home
#: Assistant compiles hourly statistics once an hour.
PROFILE_REFRESH = timedelta(hours=1)

#: How far before the read window a charger's statistics are looked for, to
#: tell a charger that was idle or unavailable from one that did not exist.
CHARGER_LOOKBACK = timedelta(days=90)

#: Energy in kWh per ``(ISO date, hour)`` block start in HA local time.
type ConsumptionProfile = dict[tuple[str, int], float]

#: House power entity and the EV charger entities subtracted from it.
type _Sources = tuple[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _CachedProfile:
    """One config entry's profile and what it was read from."""

    fetched_at: datetime
    sources: _Sources
    profile: ConsumptionProfile


_profile_cache: dict[str, _CachedProfile] = {}
_profile_locks: dict[str, asyncio.Lock] = {}
#: ``(house, charger)`` pairs already reported as impossible to backfill.
_reported_chargers: set[tuple[str, str]] = set()


def _sources(config_entry: ConfigEntry) -> _Sources | None:
    """Return the configured entities, or ``None`` without a house source."""
    house = configured_house_power_entity(config_entry)
    if not house:
        return None
    chargers: tuple[str, ...] = ()
    if get_config_value(config_entry, "hsem_house_power_includes_ev_charger_power"):
        chargers = tuple(
            entity_id
            for key in ("hsem_ev_charger_power", "hsem_ev_second_charger_power")
            if (entity_id := optional_entity_id(get_config_value(config_entry, key)))
        )
    return house, chargers


async def async_consumption_profile(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> ConsumptionProfile:
    """Return the hour-block consumption known from long-term statistics.

    Args:
        hass: The Home Assistant instance.
        config_entry: The HSEM config entry.

    Returns:
        Energy in kWh per ``(ISO date, hour)`` for the last
        :data:`BACKFILL_DAYS` days.  Empty when the recorder is not loaded,
        no house power source is configured, it has no statistics or the
        read failed.
    """
    if "recorder" not in hass.config.components:
        return {}
    sources = _sources(config_entry)
    if sources is None:
        return {}

    entry_id = config_entry.entry_id
    async with _profile_locks.setdefault(entry_id, asyncio.Lock()):
        now = dt_util.utcnow()
        cached = _profile_cache.get(entry_id)
        if (
            cached is not None
            and cached.sources == sources
            and cache_is_fresh(cached.fetched_at, now, PROFILE_REFRESH)
        ):
            return cached.profile
        try:
            profile = await _read_profile(hass, sources, now)
        except Exception as exc:
            # A recorder or database error must not stop the sensor update.
            # The empty result is cached, so the 96 sensors log and ask once.
            _LOGGER.warning(
                "Consumption backfill: reading statistics of %s failed, next "
                "try in %s: %s: %r",
                sources[0],
                PROFILE_REFRESH,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            profile = {}
        _profile_cache[entry_id] = _CachedProfile(now, sources, profile)
        _LOGGER.debug(
            "Consumption backfill: %d hour blocks from statistics of %s",
            len(profile),
            sources[0],
        )
        return profile


async def _read_profile(
    hass: HomeAssistant, sources: _Sources, now: datetime
) -> ConsumptionProfile:
    """Read the statistics of *sources* and build the hour-block profile."""
    house_entity, charger_entities = sources
    start = now - timedelta(days=BACKFILL_DAYS)
    reader = StatisticsReader(hass)
    house = await reader.read_hourly_mean(house_entity, start, now)
    if not house:
        return {}
    first_house_hour = min(house)
    chargers: list[tuple[dict[datetime, float], datetime]] = []
    for entity_id in charger_entities:
        series = await reader.read_hourly_mean(entity_id, start, now)
        known_from = await _charger_known_from(
            reader, entity_id, series, start, first_house_hour
        )
        if known_from is None:
            if (house_entity, entity_id) not in _reported_chargers:
                _reported_chargers.add((house_entity, entity_id))
                _LOGGER.info(
                    "Consumption backfill: %s has no long-term statistics, so "
                    "EV charging cannot be subtracted from %s. Nothing is "
                    "backfilled.",
                    entity_id,
                    house_entity,
                )
            return {}
        chargers.append((series, known_from))

    profile: ConsumptionProfile = {}
    for hour_start, house_power in house.items():
        local = dt_util.as_local(hour_start)
        if local.minute:
            return {}
        if any(hour_start < known_from for _series, known_from in chargers):
            # Before a charger's first statistics its power is unknown, and
            # the house power may hold EV charging that cannot be subtracted.
            continue
        block = (local.date().isoformat(), local.hour)
        # From then on a charger hour without statistics is 0 W, as an
        # unavailable charger is in the live path.
        ev_power = sum(series.get(hour_start, 0.0) for series, _from in chargers)
        # A mean of W over one hour is Wh.  Both folds of an autumn repeated
        # hour land in the same block: the utility meter resets once a day,
        # on the first fold, and so holds both as well.
        energy = (house_power - ev_power) / 1000.0
        profile[block] = profile.get(block, 0.0) + energy

    return {
        block: round(energy, 2)
        for block, energy in profile.items()
        if math.isfinite(energy) and energy >= 0.0
    }


async def _charger_known_from(
    reader: StatisticsReader,
    entity_id: str,
    series: dict[datetime, float],
    start: datetime,
    first_house_hour: datetime,
) -> datetime | None:
    """Return the hour from which a missing charger hour means 0 W.

    Args:
        reader: The statistics reader.
        entity_id: The EV charger power entity.
        series: Its hourly means inside the read window.
        start: Start of the read window.
        first_house_hour: The oldest hour the house source has statistics for.

    Returns:
        The charger's first statistics hour, or *start* when it has
        statistics from before the window.  ``None`` when it has none at all.
    """
    first = min(series, default=None)
    if first is not None and first <= first_house_hour:
        return first
    # The first row in the window is not the first row ever: a charger that
    # reports nothing while idle may have been silent for days.
    if await reader.read_hourly_mean(entity_id, start - CHARGER_LOOKBACK, start):
        return start
    return first


def missing_days(
    profile: ConsumptionProfile,
    hour: int,
    observed: Collection[str],
    count: int,
    before: str,
) -> dict[str, float]:
    """Return the newest days of *hour* that statistics know and HSEM does not.

    Args:
        profile: Output of :func:`async_consumption_profile`.
        hour: Start hour (0-23) of the block.
        observed: ISO dates that already have a measurement; never returned.
        count: The most days to return.
        before: ISO date; only days earlier than it are returned.

    Returns:
        Energy in kWh per ISO date, at most ``count`` entries.
    """
    days = sorted(
        (
            day
            for day, block_hour in profile
            if block_hour == hour and day < before and day not in observed
        ),
        reverse=True,
    )
    return {day: profile[(day, hour)] for day in days[: max(count, 0)]}

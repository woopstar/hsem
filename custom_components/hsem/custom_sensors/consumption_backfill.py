"""Backfill of the hour-block consumption averages from long-term statistics.

The :class:`~custom_components.hsem.custom_sensors.avg_sensor.HSEMAvgSensor`
windows only fill while HSEM runs, one day at a time.  Home Assistant keeps
hourly long-term statistics for the house power sensor from before HSEM was
installed, so the days HSEM did not observe can be taken from there
(issue #1433).

The profile is built the way the live path measures it
(:class:`~custom_components.hsem.custom_sensors.house_consumption_power_sensor.HSEMHouseConsumptionPowerSensor`):
the hourly mean of the house power source, minus both EV chargers when the
house power includes them.  An hour is left out when a required source has
no statistics for it, when the result is negative, and when the Home
Assistant time zone is not a whole number of hours from UTC (statistics
hours would not line up with the hour blocks).

All 96 average sensors of a config entry share one read, refreshed at most
once an hour.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant

from custom_components.hsem.ml.statistics_reader import StatisticsReader
from custom_components.hsem.utils.datetime_utils import cache_is_fresh
from custom_components.hsem.utils.house_load import entry_house_power_entity
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.misc import get_config_value, optional_entity_id

#: Days of statistics read: the longest average window plus the current day.
BACKFILL_DAYS = 15

#: How long a read is shared before the statistics are queried again.  Home
#: Assistant compiles hourly statistics once an hour.
PROFILE_REFRESH = timedelta(hours=1)

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


def _sources(hass: HomeAssistant, config_entry: ConfigEntry) -> _Sources | None:
    """Return the entities the live path reads, or ``None`` without a source."""
    house = entry_house_power_entity(hass, config_entry)
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
        no house power source is configured or it has no statistics.
    """
    if "recorder" not in hass.config.components:
        return {}
    sources = _sources(hass, config_entry)
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
        profile = await _read_profile(hass, sources, now)
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
    house = await reader.read_hourly_mean(house_entity, start, now, UnitOfPower.WATT)
    if not house:
        return {}
    chargers = [
        await reader.read_hourly_mean(entity_id, start, now, UnitOfPower.WATT)
        for entity_id in charger_entities
    ]

    profile: ConsumptionProfile = {}
    incomplete: set[tuple[str, int]] = set()
    for hour_start, house_watts in house.items():
        local = dt_util.as_local(hour_start)
        if local.minute:
            return {}
        block = (local.date().isoformat(), local.hour)
        charger_watts = [series.get(hour_start) for series in chargers]
        if any(watts is None for watts in charger_watts):
            incomplete.add(block)
            continue
        # A mean of W over one hour is Wh.  Both folds of an autumn repeated
        # hour land in the same block, as they do in the utility meter.
        energy = (house_watts - sum(w for w in charger_watts if w is not None)) / 1000.0
        profile[block] = profile.get(block, 0.0) + energy

    return {
        block: round(energy, 2)
        for block, energy in profile.items()
        if block not in incomplete and math.isfinite(energy) and energy >= 0.0
    }


def missing_days(
    profile: ConsumptionProfile, hour: int, observed: Collection[str], count: int
) -> dict[str, float]:
    """Return the newest days of *hour* that statistics know and HSEM does not.

    Args:
        profile: Output of :func:`async_consumption_profile`.
        hour: Start hour (0-23) of the block.
        observed: ISO dates that already have a measurement; never returned.
        count: The most days to return.

    Returns:
        Energy in kWh per ISO date, at most ``count`` entries.
    """
    days = sorted(
        (
            day
            for day, block_hour in profile
            if block_hour == hour and day not in observed
        ),
        reverse=True,
    )
    return {day: profile[(day, hour)] for day in days[: max(count, 0)]}

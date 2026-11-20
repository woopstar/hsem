"""Statistics reader — hourly long-term statistics from the HA recorder.

Raw states are purged after the recorder's ``purge_keep_days`` (10 by
default), but Home Assistant keeps hourly long-term statistics for every
sensor with a ``state_class``.  This module reads them so a new install can
build its consumption profile from history that predates HSEM (issue #1433):

- :meth:`StatisticsReader.read_hourly_energy` turns the ``sum`` of an energy
  accumulator into the energy used in each hour.
- :meth:`StatisticsReader.read_hourly_mean` returns the hourly ``mean`` of a
  measurement sensor, e.g. house power in W.

Both run in the recorder executor.  An hour without a statistics row is left
out of the result; it is never reported as zero.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Literal

from homeassistant.components.recorder import (
    get_instance,  # pyright: ignore[reportPrivateImportUsage] — HA public API, not in stubs
)
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant

from custom_components.hsem.ml.slot_deltas import MAX_SLOT_KWH
from custom_components.hsem.utils.datetime_utils import normalize_slot_start, utc_key
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

type _StatisticType = Literal["mean", "sum"]

_HOUR = timedelta(hours=1)

#: Unit classes Home Assistant converts statistics by.
_UNIT_CLASS_ENERGY = "energy"
_UNIT_CLASS_POWER = "power"


class StatisticsReader:
    """Reads hourly long-term statistics from the HA recorder."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the reader.

        Args:
            hass: The Home Assistant instance.
        """
        self._hass = hass

    async def read_hourly_energy(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        unit: str | None = None,
    ) -> dict[datetime, float]:
        """Return the energy an accumulator gained in each hour.

        Args:
            entity_id: An energy accumulator with long-term statistics.
            start: Start of the first hour wanted.
            end: End of the window; hours starting at or after it are left out.
            unit: Energy unit to convert to (e.g. ``kWh``).  ``None`` keeps
                the unit the statistics are stored in.

        Returns:
            Energy per UTC hour start.  An hour is present only when both it
            and the hour before it have a ``sum``, so a gap in the statistics
            never becomes one oversized hour.  Negative differences are
            dropped.
        """
        # One hour earlier: the first wanted hour needs the sum before it.
        rows = await get_instance(self._hass).async_add_executor_job(
            self._hourly_values,
            entity_id,
            utc_key(start) - _HOUR,
            utc_key(end),
            "sum",
            None if unit is None else {_UNIT_CLASS_ENERGY: unit},
        )
        hours: dict[datetime, float] = {}
        for hour_start in sorted(rows):
            previous = rows.get(hour_start - _HOUR)
            if previous is None:
                continue
            energy = rows[hour_start] - previous
            if energy >= 0.0:
                hours[hour_start] = energy
        return hours

    async def read_hourly_mean(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        unit: str | None = None,
    ) -> dict[datetime, float]:
        """Return the hourly mean of a measurement sensor.

        Args:
            entity_id: A measurement sensor with long-term statistics.
            start: Start of the first hour wanted.
            end: End of the window; hours starting at or after it are left out.
            unit: Power unit to convert to (e.g. ``W``).  ``None`` keeps the
                unit the statistics are stored in.

        Returns:
            Mean value per UTC hour start, for the hours that have one.
        """
        return await get_instance(self._hass).async_add_executor_job(
            self._hourly_values,
            entity_id,
            utc_key(start),
            utc_key(end),
            "mean",
            None if unit is None else {_UNIT_CLASS_POWER: unit},
        )

    def _hourly_values(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        statistic_type: _StatisticType,
        units: dict[str, str] | None,
    ) -> dict[datetime, float]:
        """Query one statistic type per hour.  Blocking: recorder executor only."""
        rows = statistics_during_period(
            self._hass, start, end, {entity_id}, "hour", units, {statistic_type}
        ).get(entity_id, [])
        values: dict[datetime, float] = {}
        for row in rows:
            value = row.get(statistic_type)
            if value is None or not math.isfinite(value):
                continue
            values[datetime.fromtimestamp(row["start"], UTC)] = float(value)
        _LOGGER.debug(
            "Statistics: %d hourly %s rows for %s from %s to %s",
            len(values),
            statistic_type,
            entity_id,
            start.isoformat(),
            end.isoformat(),
        )
        return values


def hourly_energy_to_slots(
    hours: dict[datetime, float], slot_minutes: int
) -> list[tuple[datetime, int, float]]:
    """Spread hourly energy evenly over the slots of each hour.

    Hourly statistics carry no shape inside the hour, so every slot gets the
    same share.  The result has the form the history reader produces from raw
    states and follows the same rules: an hour whose share is zero, negative,
    non-finite or above the per-slot cap is left out.

    Args:
        hours: Energy in kWh per UTC hour start.
        slot_minutes: Slot width in minutes.  Must divide evenly into 60.

    Returns:
        ``(slot_start, slot_index, energy_kwh)`` tuples, oldest first.
    """
    slots_per_hour = 60 // slot_minutes
    samples: list[tuple[datetime, int, float]] = []
    for hour_start in sorted(hours):
        energy = hours[hour_start] / slots_per_hour
        if not math.isfinite(energy) or energy <= 0.0 or energy > MAX_SLOT_KWH:
            continue
        for position in range(slots_per_hour):
            slot_start = normalize_slot_start(
                hour_start + timedelta(minutes=position * slot_minutes), slot_minutes
            )
            slot_index = (slot_start.hour * 60 + slot_start.minute) // slot_minutes
            samples.append((slot_start, slot_index, round(energy, 4)))
    return samples

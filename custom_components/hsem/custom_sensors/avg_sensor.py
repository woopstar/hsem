"""Rolling N-day energy average sensor for HSEM hour-block consumption.

Computes a moving average of daily utility-meter energy readings (kWh)
over a configurable number of days.  Used by the planner to estimate
expected house consumption per hour block.

Created dynamically by :class:`HSEMHouseConsumptionPowerSensor` for each
hour block and average period (1, 3, 7, or 14 days).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, override

import homeassistant.util.dt as dt_util
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.ha_helpers import ha_get_entity_state_and_convert
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER


class HSEMAvgSensor(RestoreEntity, SensorEntity, HSEMEntity):
    """Rolling N-day average of a utility-meter energy reading (kWh)."""

    _attr_icon = "mdi:calculator"
    _attr_has_entity_name = True

    # Exclude all attributes from recording except state, last_updated and measurements
    _unrecorded_attributes = frozenset(
        ["tracked_entity", "average", "hour_start", "hour_end", "unique_id"]
    )

    def __init__(
        self,
        config_entry: ConfigEntry,
        hour_start: int,
        hour_end: int,
        avg: int,
        tracked_entity: str | None,
        name: str,
        unique_id: str,
        entity_id: str,
    ) -> None:
        """Initialize the rolling average sensor.

        Args:
            config_entry: The HSEM config entry.
            hour_start: Start hour (0-23) of the consumption block.
            hour_end: End hour (0-23) of the consumption block.
            avg: Number of days over which to compute the rolling average.
            tracked_entity: Entity ID of the utility meter to track.
            name: Display name for the sensor.
            unique_id: Unique ID for the HA entity registry.
            entity_id: Entity ID for the sensor.
        """
        super().__init__(config_entry)
        self._hour_start = hour_start
        self._hour_end = hour_end
        self._average = avg
        self._tracked_entity = tracked_entity
        self._attr_unique_id = unique_id
        self.entity_id = entity_id
        self._state: float | None = None
        self._last_updated: str | None = None
        self._config_entry = config_entry
        self._name = name
        self._measurements: dict[str, float] | None = None
        self._tracked_entities: set[str] = set()
        # Unsubscribe callbacks registered by async_track_* helpers.
        self._unsub_callbacks: list = []

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        return {
            "tracked_entity": self._tracked_entity,
            "average": self._average,
            "hour_start": self._hour_start,
            "hour_end": self._hour_end,
            "last_updated": self._last_updated,
            "unique_id": self._attr_unique_id,
            "measurements": self._measurements,
        }

    @property  # type: ignore[misc]  # HA stub declares state as @final
    @override
    def state(self) -> float | None:
        return self._state

    @property  # type: ignore[misc]  # HA stub declares unit_of_measurement as @final
    @override
    def unit_of_measurement(self) -> str:
        return UnitOfEnergy.KILO_WATT_HOUR

    @property
    @override
    def state_class(self) -> SensorStateClass:
        return SensorStateClass.MEASUREMENT

    @property
    @override
    def device_class(self) -> SensorDeviceClass:
        return SensorDeviceClass.ENERGY

    @property
    @override
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    @override
    def name(self) -> str:
        return self._name

    @property
    @override
    def should_poll(self) -> bool:
        return True

    @property
    @override
    def available(self) -> bool:
        """Return True when the sensor has a valid state."""
        return self._state is not None

    async def async_update(self, event: Any | None = None) -> None:
        """Manually trigger the sensor update."""
        return await self._async_handle_update(event)

    def parse_date(self, date_str: str) -> str:
        """Normalize an ISO date string to YYYY-MM-DD format.

        Args:
            date_str: ISO datetime string (e.g. ``"2025-01-15T12:00:00"``).

        Returns:
            Normalized date string in ``YYYY-MM-DD`` format.
        """
        # Strip any time component if it exists
        date_part = date_str.split("T")[0] if "T" in date_str else date_str
        return datetime.strptime(date_part, "%Y-%m-%d").date().isoformat()

    @override
    async def async_added_to_hass(self) -> None:
        """Handle when sensor is added to Home Assistant."""

        # Get the last state of the sensor
        old_state = await self.async_get_last_state()

        if old_state is not None:
            # A restored ``unknown``/``unavailable``/unparseable state must not
            # crash ``float()`` or be treated as a finite measurement.
            restored_state = convert_to_float(old_state.state)
            self._state = (
                restored_state
                if restored_state is not None and math.isfinite(restored_state)
                else None
            )

            restored_measurements = old_state.attributes.get("measurements", None)

            if restored_measurements is not None:
                self._measurements = {
                    self.parse_date(k): round(float(v), 2)
                    for k, v in restored_measurements.items()
                }

            self._last_updated = old_state.attributes.get("last_updated", None)

        # Register new timer — store unsub so it is cancelled on removal.
        self._unsub_callbacks.append(
            async_track_time_interval(
                self.hass, self._async_handle_update, timedelta(minutes=5)
            )
        )

        # Initial update
        await self._async_handle_update(None)

        await super().async_added_to_hass()

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Cancel all tracked listeners when the entity is removed."""
        for unsub in self._unsub_callbacks:
            unsub()
        self._unsub_callbacks.clear()
        await super().async_will_remove_from_hass()

    async def _async_track_entities(self) -> None:
        """Register a state-change listener for the tracked utility meter."""
        if self._tracked_entity:
            if self._tracked_entity not in self._tracked_entities:
                unsub = async_track_state_change_event(
                    self.hass,
                    [self._tracked_entity],
                    self._async_handle_update,
                )
                self._unsub_callbacks.append(unsub)
                self._tracked_entities.add(self._tracked_entity)

    async def _async_handle_update(self, event: Any | None = None) -> None:
        """Handle updates to the source sensor."""
        # No completed measurements means unavailable, not measured zero.
        # A non-empty measurement set whose average is genuinely 0.0 remains
        # available and is published as zero below.
        self._state = None

        now = dt_util.now()

        # Track state changes for the source sensors. Also if they change.
        await self._async_track_entities()

        await self._async_store_utility_meter_value()

        # Calculate the average value from `self._measurements`
        if self._measurements:
            total = sum(self._measurements.values())
            count = len(self._measurements)
            if count > 0:
                self._state = round(total / count, 2)

        self._last_updated = now.isoformat()

        # Trigger an update in Home Assistant
        self.async_write_ha_state()

    async def _async_store_utility_meter_value(self) -> None:
        """Store the utility meter's value for the current day after the hour is over.

        The tracked utility meter resets daily at ``hour_start`` and
        accumulates energy only during the ``hour_start`` → ``hour_end``
        block (the power sensor reports ``unknown`` outside that window, so
        the integral pauses).  Sampling the meter before ``hour_end``
        records a **partial** day as if it were complete, which inflates
        the rolling average once enough partial days accumulate (issue #720
        follow-up: a 14 kWh reading taken at 05:45 was averaged as a full
        day, producing ~4.7 kWh/h forecasts for a ~260 W house).

        The sample is therefore only persisted once the day's hour block is
        complete, i.e. when ``now.hour >= hour_end``.  For overnight blocks
        (``hour_end < hour_start``, e.g. 23→00) the block closes at
        midnight, so any time after the block started counts as complete.
        """
        now = dt_util.now()

        try:
            utility_meter_value = ha_get_entity_state_and_convert(
                self, self._tracked_entity, "float"
            )
        except (HomeAssistantError, ValueError, TypeError) as exc:
            _LOGGER.warning(
                "Sensor read failed for entity_id=%s (operation=_async_store_utility_meter_value): "
                "%s: %s",
                self._tracked_entity,
                type(exc).__name__,
                repr(exc),
            )
            utility_meter_value = None

        if self._measurements is None:
            self._measurements = {}

        if utility_meter_value is not None:
            # The block runs hour_start → hour_end (hour_end = hour_start+1,
            # wrapping to 0 for the 23→00 block).  It is complete once the
            # current hour reaches hour_end.  For the overnight 23→00 block
            # (hour_end == 0) the block closes at midnight, so any hour
            # except 23 itself counts as complete.
            if self._hour_end > self._hour_start:
                block_complete = now.hour >= self._hour_end
                measurement_date = now.date()
            else:
                # Overnight block (23→00): complete after midnight, i.e.
                # whenever the current hour is not the block's start hour.
                block_complete = now.hour != self._hour_start
                # After midnight the meter still holds energy produced on the
                # previous date — attribute the measurement to that date.
                measurement_date = (
                    now.date()
                    if now.hour >= self._hour_start
                    else (now - timedelta(days=1)).date()
                )

            if block_complete:
                self._measurements[measurement_date.isoformat()] = round(
                    float(utility_meter_value), 2
                )

        if self._measurements is not None and len(self._measurements) > self._average:
            await self._async_cleanup_old_measurements()

    async def _async_cleanup_old_measurements(self) -> None:
        """Cleanup old measurements."""

        if self._measurements is not None:
            sorted_dates = sorted(self._measurements.keys())

            if len(sorted_dates) > self._average:
                # Calculate how many items to remove
                items_to_remove = len(sorted_dates) - self._average

                # Remove the oldest dates (they come first in the sorted list)
                for date in sorted_dates[:items_to_remove]:
                    del self._measurements[date]

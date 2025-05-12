import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.misc import ha_get_entity_state_and_convert

_LOGGER = logging.getLogger(__name__)


class HSEMAvgSensor(SensorEntity, HSEMEntity, RestoreEntity):
    """A template sensor for Home Assistant.

    This sensor calculates the average value of a tracked entity over a specified period.
    """

    _attr_icon = "mdi:calculator"
    _attr_has_entity_name = True

    # Exclude all attributes from recording except state, last_updated and measurements
    _unrecorded_attributes = frozenset(
        ["tracked_entity", "average", "hour_start", "hour_end", "unique_id"]
    )

    def __init__(
        self,
        config_entry,
        hour_start,
        hour_end,
        avg,
        tracked_entity,
        name,
        unique_id,
        entity_id,
    ) -> None:
        super().__init__(config_entry)
        self._hour_start = hour_start
        self._hour_end = hour_end
        self._average = avg
        self._tracked_entity = tracked_entity
        self._attr_unique_id = unique_id
        self.entity_id = entity_id
        self._state = None
        self._last_updated = None
        self._config_entry = config_entry
        self._name = name
        self._measurements = None
        self._tracked_entities = set()

    @property
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

    @property
    def state(self) -> float | None:
        return self._state

    @property
    def unit_of_measurement(self) -> str:
        return UnitOfEnergy.KILO_WATT_HOUR

    @property
    def state_class(self) -> str:
        return SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def should_poll(self) -> bool:
        return True

    async def async_update(self, event=None) -> None:
        """Manually trigger the sensor update."""
        return await self._async_handle_update(None)

    def parse_date(self, date_str: str) -> str:
        """
        Parse a date string and return it in ISO format.

        Parameters:
        date_str (str): The date string to parse.

        Returns:
        str: The parsed date in ISO format.
        """
        # Strip any time component if it exists
        try:
            date_part = date_str.split("T")[0] if "T" in date_str else date_str
            return datetime.strptime(date_part, "%Y-%m-%d").date().isoformat()
        except ValueError:
            _LOGGER.warning("Invalid date string: %s", date_str)
            return datetime.now().date().isoformat()

    async def async_added_to_hass(self) -> None:
        """Handle when sensor is added to Home Assistant."""

        # Get the last state of the sensor
        old_state = await self.async_get_last_state()

        if old_state is not None:
            self._state = float(old_state.state)

            restored_measurements = old_state.attributes.get("measurements", None)

            if restored_measurements is not None:
                self._measurements = {
                    self.parse_date(k): round(float(v), 2)
                    for k, v in restored_measurements.items()
                }

            self._last_updated = old_state.attributes.get("last_updated", None)

        # Register new timer
        async_track_time_interval(
            self.hass, self._async_handle_update, timedelta(minutes=5)
        )

        # Initial update
        await self._async_handle_update(None)

        await super().async_added_to_hass()

    async def _async_track_entities(self) -> None:
        if self._tracked_entity:
            if self._tracked_entity not in self._tracked_entities:
                async_track_state_change_event(
                    self.hass,
                    [self._tracked_entity],
                    self._async_handle_update,
                )
                self._tracked_entities.add(self._tracked_entity)

    async def _async_handle_update(self, event: Any) -> None:
        """
        Handle updates to the source sensor.

        Parameters:
        event (Any): The event triggering the update.
        """
        self._state = 0.00

        now = datetime.now()

        # Track state changes for the source sensors. Also if they change.
        await self._async_track_entities()

        # Store the utility meter value
        await self._async_store_utility_meter_value()

        # Calculate the average value from `self._measurements`
        if self._measurements:
            total = sum(self._measurements.values())
            count = len(self._measurements)
            if count > 0:
                self._state = round(total / count, 2)
            else:
                _LOGGER.warning("No measurements available for averaging.")
        else:
            _LOGGER.warning("Measurements dictionary is empty.")

        self._last_updated = now.isoformat()

        # Trigger an update in Home Assistant
        self.async_write_ha_state()

    async def _async_cleanup_old_measurements(self) -> None:
        """
        Cleanup old measurements to maintain the specified average period.
        """
        if self._measurements:
            # Remove the oldest entries exceeding the average period
            sorted_dates = sorted(self._measurements.keys())
            excess_entries = len(sorted_dates) - self._average
            for date in sorted_dates[:excess_entries]:
                del self._measurements[date]
                _LOGGER.debug("Removed old measurement for date: %s", date)

    async def _async_store_utility_meter_value(self) -> None:
        """
        Store the utility meter's value for the current day after the hour is over.
        """
        now = datetime.now()
        current_date = now.date()

        try:
            utility_meter_value = ha_get_entity_state_and_convert(
                self, self._tracked_entity, "float"
            )
        except Exception as e:
            _LOGGER.error("Failed to fetch utility meter value: %s", e)
            utility_meter_value = None

        if self._measurements is None:
            self._measurements = {}

        if utility_meter_value is not None:
            self._measurements[current_date.isoformat()] = round(
                float(utility_meter_value), 2
            )
        else:
            _LOGGER.warning("Utility meter value is None.")

        # Cleanup old measurements if necessary
        if len(self._measurements) > self._average:
            await self._async_cleanup_old_measurements()

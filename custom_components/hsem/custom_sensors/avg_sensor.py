import logging
from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.misc import ha_get_entity_state_and_convert

_LOGGER = logging.getLogger(__name__)


class HSEMAvgSensor(SensorEntity, HSEMEntity, RestoreEntity):
    """A template sensor for Home Assistant."""

    _attr_icon = "mdi:calculator"
    _attr_has_entity_name = True

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
    ):
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
        self._measurements = {}
        self._tracked_entities = set()

    @property
    def extra_state_attributes(self):
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
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        return UnitOfEnergy.KILO_WATT_HOUR

    @property
    def state_class(self):
        return SensorStateClass.MEASUREMENT

    @property
    def unique_id(self):
        return self._attr_unique_id

    @property
    def name(self):
        return self._name

    @property
    def should_poll(self):
        return True

    async def async_update(self, event=None):
        """Manually trigger the sensor update."""
        return await self._async_handle_update(None)

    async def async_added_to_hass(self):
        # Get the last state of the sensor
        old_state = await self.async_get_last_state()

        if old_state is not None:
            self._state = float(old_state.state)

            restored_measurements = old_state.attributes.get("measurements", None)

            if isinstance(restored_measurements, dict):
                self._measurements = {
                    k: float(v) for k, v in restored_measurements.items()
                }
            else:
                self._measurements = {}

            self._last_updated = old_state.attributes.get("last_updated", None)

        # Initial update
        await self._async_handle_update(None)

        """Handle when sensor is added to Home Assistant."""
        return await super().async_added_to_hass()

    async def _async_track_entities(self):
        if self._tracked_entity:
            if self._tracked_entity not in self._tracked_entities:
                async_track_state_change_event(
                    self.hass,
                    [self._tracked_entity],
                    self._async_handle_update,
                )
                self._tracked_entities.add(self._tracked_entity)

    async def _async_handle_update(self, event):
        """Handle updates to the source sensor."""
        self._state = 0.00

        now = datetime.now()

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
        return self.async_write_ha_state()

    async def _async_store_utility_meter_value(self):
        """Store the utility meter's value for the current day after the hour is over."""
        now = datetime.now()
        current_date = now.date()

        try:
            utility_meter_value = ha_get_entity_state_and_convert(
                self, self._tracked_entity, "float"
            )
        except Exception:
            utility_meter_value = None

        if utility_meter_value is not None and utility_meter_value >= 0:
            self._measurements[current_date.isoformat()] = round(utility_meter_value, 2)
        else:
            self._measurements[current_date.isoformat()] = 0.00

        await self._async_cleanup_old_measurements()

    async def _async_cleanup_old_measurements(self):
        if self._measurements and self._average > 0:
            if len(self._measurements) > self._average:
                sorted_dates = sorted(self._measurements.keys())
                for date in sorted_dates[: self._average]:
                    del self._measurements[date]

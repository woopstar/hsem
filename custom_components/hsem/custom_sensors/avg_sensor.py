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
        self._values = {}
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
            "values": self._values,
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

    async def async_update(self, event=None):
        """Manually trigger the sensor update."""
        return await self._async_handle_update(None)

    async def async_added_to_hass(self):
        # Get the last state of the sensor
        old_state = await self.async_get_last_state()
        if old_state is not None:
            self._state = float(old_state.state)

            restored_values = old_state.attributes.get("values", {})
            if isinstance(restored_values, dict):
                self._values = {k: float(v) for k, v in restored_values.items()}
            else:
                self._values = {}

            self._last_updated = old_state.attributes.get("last_updated", None)
        else:
            self._state = 0.0
            self._values = {}

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
        self._state = 0.0

        now = datetime.now()

        # Track state changes for the source sensors. Also if they change.
        await self._async_track_entities()

        await self._async_store_utility_meter_value()
        await self._async_cleanup_old_values()

        # Calculate the average value from `self._values`
        if self._values:
            total = sum(self._values.values())
            count = len(self._values)
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
        except Exception as e:
            utility_meter_value = None

        if utility_meter_value is not None and utility_meter_value >= 0:
            self._values[current_date.isoformat()] = utility_meter_value
        else:
            self._values[current_date.isoformat()] = 0.0

    async def _async_cleanup_old_values(self):
        if self._values:
            if len(self._values) > self._average:
                sorted_dates = sorted(self._values.keys())
                for date in sorted_dates[: self._average]:
                    del self._values[date]

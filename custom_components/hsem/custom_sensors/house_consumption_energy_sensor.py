"""
HouseConsumptionEnergySensor is a custom sensor entity for Home Assistant that tracks the energy consumption of a house 
within a specified hourly range. It extends both SensorEntity and HSEMEntity.

Attributes:
    _attr_icon (str): Icon for the sensor.
    _attr_has_entity_name (bool): Indicates if the entity has a name.
    _hour_start (int): Start hour for the energy consumption tracking.
    _hour_end (int): End hour for the energy consumption tracking.
    _unique_id (str): Unique identifier for the sensor.
    _hsem_power_sensor_entity (str): Entity ID of the power sensor.
    _hsem_power_sensor_state (float): Current state of the power sensor.
    _config_entry (ConfigEntry): Configuration entry for the sensor.
    _state (float): Current state of the energy consumption.
    _last_updated (str): Timestamp of the last update.
    _entity_is_tracked (bool): Indicates if the entity is being tracked.
    _last_reset_date (date): Date of the last reset.

Properties:
    name (str): Name of the sensor.
    unique_id (str): Unique identifier for the sensor.
    state (float): Current state of the energy consumption.
    unit_of_measurement (str): Unit of measurement for the sensor (kWh).
    state_class (str): State class of the sensor (total).
    extra_state_attributes (dict): Additional state attributes for the sensor.

Methods:
    async_added_to_hass(): Handle when sensor is added to Home Assistant.
    _handle_update(event): Handle updates to the sensor state.
    async_update(event=None): Manually trigger the sensor update.
"""

import logging
from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import async_track_state_change_event

from ..const import DOMAIN, ICON
from ..entity import HSEMEntity
from ..utils.misc import async_resolve_entity_id_from_unique_id, convert_to_float

_LOGGER = logging.getLogger(__name__)


class HouseConsumptionEnergySensor(SensorEntity, HSEMEntity):
    _attr_icon = ICON
    _attr_has_entity_name = True

    def __init__(self, config_entry, hour_start, hour_end):
        super().__init__(config_entry)
        self._hour_start = hour_start
        self._hour_end = hour_end
        self._unique_id = (
            f"{DOMAIN}_house_consumption_energy_{hour_start:02d}_{hour_end:02d}"
        )
        self._hsem_power_sensor_entity = None
        self._hsem_power_sensor_state = 0.0
        self._config_entry = config_entry
        self._state = 0.0
        self._last_updated = None
        self._entity_is_tracked = False
        self._last_reset_date = None  # Keeps track of the last reset date

    @property
    def name(self):
        return f"House Consumption {self._hour_start:02d}-{self._hour_end:02d} Energy"

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        return "kWh"

    @property
    def state_class(self):
        return "total"

    @property
    def extra_state_attributes(self):
        return {
            "power_sensor_entity": self._hsem_power_sensor_entity,
            "power_sensor_state": self._hsem_power_sensor_state,
            "last_updated": self._last_updated,
            "unique_id": self._unique_id,
            "last_reset_date": self._last_reset_date,
        }

    async def async_added_to_hass(self):
        """Handle when sensor is added to Home Assistant."""
        await super().async_added_to_hass()

        old_state = await self.async_get_last_state()
        if old_state is not None:
            try:
                self._state = round(convert_to_float(old_state.state), 2)
                self._last_reset_date = datetime.strptime(
                    old_state.attributes.get("last_reset_date"), "%Y-%m-%d"
                ).date()
                self._last_updated = old_state.attributes.get("last_updated", None)
            except (ValueError, TypeError):
                _LOGGER.warning(f"Invalid old state value for {self.name}")
                self._state = 0.0
                self._last_reset_date = datetime.now().date()
                self._last_updated = None

    async def _handle_update(self, event):
        now = datetime.now()

        # Check if we need to reset (if the day has changed)
        if self._last_reset_date != now.date():
            _LOGGER.info(f"Nulstiller sensoren for en ny dag: {self.name}")
            self._state = 0.0  # Nulstil energiforbruget
            self._last_reset_date = now.date()

        # Find power sensor from unique id
        self._hsem_power_sensor_entity = await async_resolve_entity_id_from_unique_id(
            self,
            f"{DOMAIN}_house_consumption_power_{self._hour_start:02d}_{self._hour_end:02d}",
        )

        if not self._hsem_power_sensor_entity:
            _LOGGER.warning(f"Power sensor not found for {self.name}")
            return

        if self._hsem_power_sensor_entity and not self._entity_is_tracked:
            async_track_state_change_event(
                self.hass,
                [self._hsem_power_sensor_entity],
                self.async_update,
            )
            self._entity_is_tracked = True

        if self._hsem_power_sensor_entity:
            state = self.hass.states.get(self._hsem_power_sensor_entity)
            if state:
                self._hsem_power_sensor_state = round(convert_to_float(state.state), 2)
            else:
                _LOGGER.warning(f"Sensor {self._hsem_power_sensor_entity} not found.")
        state = None

        if self._last_updated and self._hsem_power_sensor_state:
            # Calculate the time interval in seconds
            time_diff = (
                now - datetime.fromisoformat(self._last_updated)
            ).total_seconds()

            # Convert power to energy (W to kWh)
            self._state += (self._hsem_power_sensor_state * time_diff) / 3600000
            # Divide by 1000 for kW and 3600 for kWh

            # Round state to two decimals
            self._state = round(self._state, 2)
        else:
            _LOGGER.debug(f"First update for {self.name}, skipping accumulation.")

        # Update last update time
        self._last_updated = now.isoformat()

        # Update Home Assistant state
        self.async_write_ha_state()

    async def async_update(self, event=None):
        """Manually trigger the sensor update."""
        await self._handle_update(event=None)

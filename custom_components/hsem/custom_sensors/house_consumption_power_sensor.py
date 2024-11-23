"""
Module for the HouseConsumptionPowerSensor class.

This module defines the HouseConsumptionPowerSensor class, which represents a sensor
that tracks power consumption per hour block in a Home Assistant environment.

Classes:
    HouseConsumptionPowerSensor: A sensor entity that tracks power consumption per hour block.

Functions:
    set_hsem_house_consumption_power(value): Sets the house consumption power entity.
    set_hsem_house_power_includes_ev_charger_power(value): Sets whether house power includes EV charger power.
    set_hsem_ev_charger_power(value): Sets the EV charger power entity.
    name: Returns the name of the sensor.
    unit_of_measurement: Returns the unit of measurement for the sensor.
    device_class: Returns the device class of the sensor.
    unique_id: Returns the unique ID of the sensor.
    state: Returns the current state of the sensor.
    extra_state_attributes: Returns the extra state attributes of the sensor.
    _update_settings(): Fetches updated settings from config_entry options.
    async_added_to_hass(): Handles when the sensor is added to Home Assistant.
    _handle_update(event): Handles updates to the source sensor.
    async_update(event=None): Manually triggers the sensor update.
"""

import logging
from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from custom_components.hsem.const import (
    DEFAULT_HSEM_HOUSE_POWER_INCLUDES_EV_CHARGER_POWER,
)
from custom_components.hsem.custom_sensors.ha_sensor_integral import add_integral_sensor
from custom_components.hsem.custom_sensors.ha_sensor_statistics import (
    add_energy_average_sensors,
)
from custom_components.hsem.custom_sensors.ha_sensor_utility_meter import (
    add_utility_meter_sensor,
)
from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.misc import (
    get_config_value,
    ha_get_entity_state_and_convert,
)
from custom_components.hsem.utils.sensornames import (
    get_house_consumption_power_sensor_name,
    get_house_consumption_power_sensor_unique_id,
)

_LOGGER = logging.getLogger(__name__)


class HouseConsumptionPowerSensor(SensorEntity, HSEMEntity):
    """Representation of a sensor that tracks power consumption per hour block."""

    _attr_icon = "mdi:flash"
    _attr_has_entity_name = True

    def __init__(self, config_entry, hour_start, hour_end, async_add_entities):
        super().__init__(config_entry)
        self._hsem_house_consumption_power = None
        self._hsem_house_consumption_power_state = 0.0
        self._hsem_ev_charger_power = None
        self._hsem_ev_charger_power_state = 0.0
        self._hsem_house_power_includes_ev_charger_power = None
        self._hsem_house_power_includes_ev_charger_power_state = (
            DEFAULT_HSEM_HOUSE_POWER_INCLUDES_EV_CHARGER_POWER
        )
        self._hour_start = hour_start
        self._hour_end = hour_end
        self._unique_id = get_house_consumption_power_sensor_unique_id(
            hour_start, hour_end
        )
        self._state = None
        self._config_entry = config_entry
        self._last_updated = None
        self._has_been_removed = []
        self._tracked_entities = set()
        self._async_add_entities = async_add_entities
        self._update_settings()

    @property
    def name(self):
        return get_house_consumption_power_sensor_name(self._hour_start, self._hour_end)

    @property
    def unit_of_measurement(self):
        return "W"

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def state(self):
        return self._state

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "house_consumption_power_entity": self._hsem_house_consumption_power,
            "house_consumption_power_state": self._hsem_house_consumption_power_state,
            "ev_charger_power_entity": self._hsem_ev_charger_power,
            "ev_charger_power_state": self._hsem_ev_charger_power_state,
            "house_power_includes_ev_charger_power": self._hsem_house_power_includes_ev_charger_power,
            "hour_start": self._hour_start,
            "hour_end": self._hour_end,
            "last_updated": self._last_updated,
            "unique_id": self._unique_id,
        }

    async def async_update(self, event=None):
        """Manually trigger the sensor update."""
        await self._handle_update(None)

    async def async_will_remove_from_hass(self):
        """Entity being removed from hass."""
        await super().async_will_remove_from_hass()

    async def async_added_to_hass(self):
        """Handle when sensor is added to Home Assistant."""
        await super().async_added_to_hass()

        # Get the last state of the sensor
        old_state = await self.async_get_last_state()
        if old_state is not None:
            self._state = old_state.state
            self._last_updated = old_state.attributes.get("last_updated", None)
        else:
            self._state = 0.0

        # Schedule a periodic update every minute
        # async_track_time_interval(self.hass, self._handle_update, timedelta(minutes=1))

        # Initial update
        await self._handle_update(None)

    def _update_settings(self):
        """Fetch updated settings from config_entry options."""
        self._hsem_house_consumption_power = get_config_value(
            self._config_entry, "hsem_house_consumption_power"
        )

        self._hsem_ev_charger_power = get_config_value(
            self._config_entry, "hsem_ev_charger_power"
        )

        self._hsem_house_power_includes_ev_charger_power = get_config_value(
            self._config_entry, "hsem_house_power_includes_ev_charger_power"
        )

    async def _handle_update(self, event):
        """Handle updates to the source sensor."""
        now = datetime.now()

        # Ensure config flow settings are reloaded if it changed.
        self._update_settings()

        # Track state changes for the source sensors. Also if they change.
        await self._track_entities()

        # Fetch the current state of the source sensors
        await self._fetch_sensor_states()

        # Calculate the state of the sensor based on the current hour and if ev charger power should be included
        if now.hour == self._hour_start:
            if isinstance(
                self._hsem_ev_charger_power_state, (int, float)
            ) and isinstance(self._hsem_house_consumption_power_state, (int, float)):
                if self._hsem_house_power_includes_ev_charger_power:
                    self._state = float(
                        self._hsem_house_consumption_power_state
                        - self._hsem_ev_charger_power_state
                    )
                else:
                    self._state = self._hsem_house_consumption_power_state
            else:
                self._state = 0.0
        else:
            self._state = 0.0

        # Add energy sensor to convert power to energy
        await add_integral_sensor(self)

        # Add utility meter sensor to reset consumed energy every day
        await add_utility_meter_sensor(self)

        # Add avg energy sensors for 1,3,7,14 days based on the utility meter sensor
        await add_energy_average_sensors(self, 1)
        await add_energy_average_sensors(self, 3)
        await add_energy_average_sensors(self, 7)
        await add_energy_average_sensors(self, 14)

        # Update last update time
        self._last_updated = now.isoformat()

        # Trigger an update in Home Assistant
        self.async_write_ha_state()

    async def _track_entities(self):
        # Track state changes for the source sensor
        if self._hsem_house_consumption_power:
            if self._hsem_house_consumption_power not in self._tracked_entities:
                _LOGGER.debug(
                    f"Starting to track state changes for {self._hsem_house_consumption_power}"
                )
                async_track_state_change_event(
                    self.hass, [self._hsem_house_consumption_power], self._handle_update
                )
                self._tracked_entities.add(self._hsem_house_consumption_power)

        if self._hsem_ev_charger_power:
            if self._hsem_ev_charger_power not in self._tracked_entities:
                _LOGGER.debug(
                    f"Starting to track state changes for {self._hsem_ev_charger_power}"
                )
                async_track_state_change_event(
                    self.hass, [self._hsem_ev_charger_power], self._handle_update
                )
                self._tracked_entities.add(self._hsem_ev_charger_power)

    async def _fetch_sensor_states(self):
        # Update the state of the sensor for house consumption power
        if self._hsem_house_consumption_power:
            self._hsem_house_consumption_power_state = ha_get_entity_state_and_convert(
                self, self._hsem_house_consumption_power, "float"
            )

        # Update the state of the sensor for EV charger power
        if self._hsem_ev_charger_power:
            self._hsem_ev_charger_power_state = ha_get_entity_state_and_convert(
                self, self._hsem_ev_charger_power, "float"
            )

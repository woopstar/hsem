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
    _async_handle_update(event): Handles updates to the source sensor.
    async_update(event=None): Manually triggers the sensor update.
"""

import logging
from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass
from homeassistant.components.utility_meter.const import (
    DATA_TARIFF_SENSORS,
    DATA_UTILITY,
)
from homeassistant.const import UnitOfPower, UnitOfTime
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.custom_sensors.integration_sensor import (
    HSEMIntegrationSensor,
)
from custom_components.hsem.custom_sensors.statistics_sensor import HSEMStatisticsSensor
from custom_components.hsem.custom_sensors.utility_meter_sensor import (
    HSEMUtilityMeterSensor,
)
from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.misc import (
    async_remove_entity_from_ha,
    async_resolve_entity_id_from_unique_id,
    get_config_value,
    ha_get_entity_state_and_convert,
)
from custom_components.hsem.utils.sensornames import (
    get_energy_average_sensor_entity_id,
    get_energy_average_sensor_name,
    get_energy_average_sensor_unique_id,
    get_house_consumption_power_sensor_entity_id,
    get_house_consumption_power_sensor_name,
    get_house_consumption_power_sensor_unique_id,
    get_integral_sensor_entity_id,
    get_integral_sensor_name,
    get_integral_sensor_unique_id,
    get_utility_meter_sensor_entity_id,
    get_utility_meter_sensor_name,
    get_utility_meter_sensor_unique_id,
)

_LOGGER = logging.getLogger(__name__)


class HSEMHouseConsumptionPowerSensor(SensorEntity, HSEMEntity, RestoreEntity):
    """Representation of a sensor that tracks power consumption per hour block."""

    _attr_icon = "mdi:flash"
    _attr_has_entity_name = True

    def __init__(self, config_entry, hour_start, hour_end, async_add_entities):
        super().__init__(config_entry)
        self._available = False
        self._missing_input_entities = True
        self._hsem_house_consumption_power = None
        self._hsem_house_consumption_power_state = 0.0
        self._hsem_ev_charger_power = None
        self._hsem_ev_charger_power_state = 0.0
        self._hsem_house_power_includes_ev_charger_power = None
        self._hsem_house_power_includes_ev_charger_power_state = None
        self._hour_start = hour_start
        self._hour_end = hour_end
        self._attr_unique_id = get_house_consumption_power_sensor_unique_id(
            hour_start, hour_end
        )
        self.entity_id = get_house_consumption_power_sensor_entity_id(
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
        return UnitOfPower.WATT

    @property
    def device_class(self):
        return SensorDeviceClass.POWER

    @property
    def unique_id(self):
        return self._attr_unique_id

    @property
    def state(self):
        return self._state

    @property
    def should_poll(self):
        return True

    @property
    def available(self):
        return self._available

    @property
    def extra_state_attributes(self):

        if self._missing_input_entities:
            return {
                "status": "error",
                "description": "Some of the required input sensors from the config flow is missing or not reporting a state. Check your configuration and make sure input sensors are configured correctly.",
                "last_updated": self._last_updated,
                "unique_id": self._attr_unique_id,
            }

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
            "unique_id": self._attr_unique_id,
        }

    async def async_update(self, event=None):
        """Manually trigger the sensor update."""
        return await self._async_handle_update(None)

    async def async_added_to_hass(self):
        # Get the last state of the sensor
        old_state = await self.async_get_last_state()
        if old_state is not None:
            self._state = old_state.state
            self._last_updated = old_state.attributes.get("last_updated", None)
        else:
            self._state = 0.0

        # Initial update
        await self._async_handle_update(None)

        # Schedule a periodic update every minute
        # async_track_time_interval(self.hass, self._async_handle_update, timedelta(minutes=1))

        """Handle when sensor is added to Home Assistant."""
        return await super().async_added_to_hass()

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

    async def _async_handle_update(self, event):
        """Handle updates to the source sensor."""
        now = datetime.now()

        # Ensure config flow settings are reloaded if it changed.
        self._update_settings()

        # Track state changes for the source sensors. Also if they change.
        await self._async_track_entities()

        # Calculate the state of the sensor based on the current hour and if ev charger power should be included
        if now.hour == self._hour_start:
            # Fetch the current state of the source sensors
            await self._async_fetch_sensor_states()

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
        await self._async_add_integral_sensor()

        # Add utility meter sensor to reset consumed energy every day
        await self._async_add_utility_meter_sensor()

        # Add avg energy sensors for 1,3,7,14 days based on the utility meter sensor
        await self._async_add_energy_average_sensors(1)
        await self._async_add_energy_average_sensors(3)
        await self._async_add_energy_average_sensors(7)
        await self._async_add_energy_average_sensors(14)

        # Update last update time
        self._last_updated = now.isoformat()
        self._available = True

        # Trigger an update in Home Assistant
        return self.async_write_ha_state()

    async def _async_track_entities(self):
        # Track state changes for the source sensor
        if self._hsem_house_consumption_power:
            if self._hsem_house_consumption_power not in self._tracked_entities:
                _LOGGER.debug(
                    f"Starting to track state changes for {self._hsem_house_consumption_power}"
                )
                async_track_state_change_event(
                    self.hass,
                    [self._hsem_house_consumption_power],
                    self._async_handle_update,
                )
                self._tracked_entities.add(self._hsem_house_consumption_power)

        if self._hsem_ev_charger_power:
            if self._hsem_ev_charger_power not in self._tracked_entities:
                _LOGGER.debug(
                    f"Starting to track state changes for {self._hsem_ev_charger_power}"
                )
                async_track_state_change_event(
                    self.hass, [self._hsem_ev_charger_power], self._async_handle_update
                )
                self._tracked_entities.add(self._hsem_ev_charger_power)

    async def _async_fetch_sensor_states(self):
        # Update the state of the sensor for house consumption power
        # Reset
        self._missing_input_entities = False

        try:
            if self._hsem_house_consumption_power:
                self._hsem_house_consumption_power_state = (
                    ha_get_entity_state_and_convert(
                        self, self._hsem_house_consumption_power, "float"
                    )
                )

            # Update the state of the sensor for EV charger power
            if self._hsem_ev_charger_power:
                self._hsem_ev_charger_power_state = ha_get_entity_state_and_convert(
                    self, self._hsem_ev_charger_power, "float"
                )

        except Exception as e:
            self._missing_input_entities = True

    async def _async_add_integral_sensor(self):
        """Add an integral sensor dynamically to convert power to energy."""

        # Create the name and unique id for the integral sensor
        integral_sensor_name = get_integral_sensor_name(
            self._hour_start, self._hour_end
        )
        integral_sensor_unique_id = get_integral_sensor_unique_id(
            self._hour_start, self._hour_end
        )
        integral_sensor_entity_id = get_integral_sensor_entity_id(
            self._hour_start, self._hour_end
        )
        power_sensor_unique_id = get_house_consumption_power_sensor_unique_id(
            self._hour_start, self._hour_end
        )

        # Resolve the source power sensor entity
        source_entity = await async_resolve_entity_id_from_unique_id(
            self, power_sensor_unique_id
        )

        if not source_entity:
            return

        # Check if the integral sensor already exists
        integral_sensor_exists = await async_resolve_entity_id_from_unique_id(
            self, integral_sensor_unique_id
        )

        if integral_sensor_exists:
            if integral_sensor_unique_id not in self._has_been_removed:
                if await async_remove_entity_from_ha(self, integral_sensor_unique_id):
                    self._has_been_removed.append(integral_sensor_unique_id)
                    _LOGGER.debug(f"Successfully removed '{integral_sensor_name}'.")
            return

        _LOGGER.debug(
            f"Adding integral sensor {integral_sensor_name} for {source_entity}"
        )

        # Create the integral sensor using the left Reimann method
        integral_sensor = HSEMIntegrationSensor(
            integration_method="left",
            name=integral_sensor_name,
            round_digits=2,
            source_entity=source_entity,
            unique_id=integral_sensor_unique_id,
            unit_prefix="k",
            unit_time=UnitOfTime.HOURS,
            max_sub_interval=timedelta(minutes=1),
            device_info=None,
            e_id=integral_sensor_entity_id,
            id=integral_sensor_unique_id,
            config_entry=self._config_entry,
        )

        # Add the integral sensor to Home Assistant
        self._async_add_entities([integral_sensor])

    async def _async_add_energy_average_sensors(self, avg=3):
        # Create the name and unique id for the avg sensor
        avg_energy_sensor_name = get_energy_average_sensor_name(
            self._hour_start, self._hour_end, avg
        )
        avg_energy_sensor_unique_id = get_energy_average_sensor_unique_id(
            self._hour_start, self._hour_end, avg
        )
        avg_energy_sensor_entity_id = get_energy_average_sensor_entity_id(
            self._hour_start, self._hour_end, avg
        )
        utility_meter_unique_id = get_utility_meter_sensor_unique_id(
            self._hour_start, self._hour_end
        )

        # find the energy sensor from the unique id
        source_entity = await async_resolve_entity_id_from_unique_id(
            self, utility_meter_unique_id
        )

        if not source_entity:
            return

        # Check if the avg sensor already exists
        avg_energy_sensor_exists = await async_resolve_entity_id_from_unique_id(
            self, avg_energy_sensor_unique_id
        )

        if avg_energy_sensor_exists:
            if avg_energy_sensor_unique_id not in self._has_been_removed:
                if await async_remove_entity_from_ha(self, avg_energy_sensor_unique_id):
                    _LOGGER.debug(
                        f"Successfully removed '{avg_energy_sensor_name}' before re-adding."
                    )
                    self._has_been_removed.append(avg_energy_sensor_unique_id)
            return

        _LOGGER.debug(
            f"Creating new average energy sensor '{avg_energy_sensor_name}' for '{source_entity}'."
        )

        avg_sensor = HSEMStatisticsSensor(
            hass=self.hass,
            source_entity_id=source_entity,
            name=avg_energy_sensor_name,
            unique_id=avg_energy_sensor_unique_id,
            state_characteristic="mean",
            samples_max_buffer_size=(24 * 60 * avg),  # Sampling size
            samples_max_age=timedelta(days=avg),  # Max age
            samples_keep_last=True,
            precision=2,
            percentile=50,
            id=avg_energy_sensor_unique_id,
            e_id=avg_energy_sensor_entity_id,
            config_entry=self._config_entry,
        )

        self._async_add_entities([avg_sensor])

    async def _async_add_utility_meter_sensor(self):
        """Add a utility meter sensor dynamically."""

        # Create the name and unique id for the avg sensor
        utility_meter_name = get_utility_meter_sensor_name(
            self._hour_start, self._hour_end
        )
        utility_meter_unique_id = get_utility_meter_sensor_unique_id(
            self._hour_start, self._hour_end
        )
        utility_meter_entity_id = get_utility_meter_sensor_entity_id(
            self._hour_start, self._hour_end
        )
        integral_sensor_unique_id = get_integral_sensor_unique_id(
            self._hour_start, self._hour_end
        )

        # Resolve the source entity (sensor) that the utility meter should track
        source_entity = await async_resolve_entity_id_from_unique_id(
            self, integral_sensor_unique_id
        )

        if not source_entity:
            return

        # Ensure DATA_UTILITY structure exists in hass.data
        if DATA_UTILITY not in self.hass.data:
            self.hass.data[DATA_UTILITY] = {}

        # Ensure the entry_id exists in DATA_UTILITY
        if source_entity not in self.hass.data[DATA_UTILITY]:
            self.hass.data[DATA_UTILITY][source_entity] = {DATA_TARIFF_SENSORS: []}

        # Check if the utility meter already exists
        utility_meter_exists = await async_resolve_entity_id_from_unique_id(
            self, utility_meter_unique_id
        )

        if utility_meter_exists:
            if utility_meter_unique_id not in self._has_been_removed:
                if await async_remove_entity_from_ha(self, utility_meter_unique_id):
                    _LOGGER.debug(
                        f"Successfully removed '{utility_meter_name}' before re-adding."
                    )
                    self._has_been_removed.append(utility_meter_unique_id)
            return

        _LOGGER.debug(
            f"Adding utility meter sensor {utility_meter_name} for {source_entity}"
        )

        # Create the utility meter sensor with the given cycle and source
        utility_meter_sensor = HSEMUtilityMeterSensor(
            cron_pattern=None,
            delta_values=False,
            meter_offset=timedelta(hours=0),
            meter_type="daily",
            name=utility_meter_name,
            net_consumption=True,
            parent_meter=source_entity,
            periodically_resetting=True,
            source_entity=source_entity,
            tariff_entity=None,
            tariff=None,
            unique_id=utility_meter_unique_id,
            device_info=None,
            sensor_always_available=True,
            id=utility_meter_unique_id,
            e_id=utility_meter_entity_id,
            config_entry=self._config_entry,
        )

        utility_meter_sensor.entity_id = utility_meter_entity_id

        # Add the utility meter to Home Assistant
        self._async_add_entities([utility_meter_sensor])

        # Append the newly created sensor to DATA_TARIFF_SENSORS
        self.hass.data[DATA_UTILITY][source_entity][DATA_TARIFF_SENSORS].append(
            utility_meter_sensor
        )

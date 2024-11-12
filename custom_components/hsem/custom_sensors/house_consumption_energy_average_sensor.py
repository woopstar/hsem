"""
HouseConsumptionEnergyAverageSensor is a custom sensor entity for Home Assistant that calculates the average energy consumption of a house over a specified period.

Attributes:
    _attr_icon (str): Icon for the sensor.
    _attr_has_entity_name (bool): Indicates if the entity has a name.
    _hour_start (int): The starting hour for the energy consumption calculation.
    _hour_end (int): The ending hour for the energy consumption calculation.
    _max_age (timedelta): The maximum age of the samples to consider for the average calculation.
    _unique_id (str): Unique identifier for the sensor.
    _hsem_energy_sensor_entity (str): Entity ID of the energy sensor.
    _hsem_energy_sensor_state (float): Current state of the energy sensor.
    _config_entry (ConfigEntry): Configuration entry for the sensor.
    _state (float): Current state of the sensor.
    _samples (deque): Deque to store the samples of energy consumption.
    _last_updated (str): Timestamp of the last update.
    _entity_is_tracked (bool): Indicates if the entity is being tracked.

Properties:
    name (str): Name of the sensor.
    unique_id (str): Unique identifier for the sensor.
    state (float): Current state of the sensor.
    unit_of_measurement (str): Unit of measurement for the sensor.
    state_class (str): State class of the sensor.
    extra_state_attributes (dict): Extra state attributes for the sensor.

Methods:
    __init__(self, config_entry, hour_start, hour_end, sampling_size=5040, max_age_days=7):
        Initializes the sensor with the given configuration entry, start and end hours, sampling size, and maximum age of samples.

    async _handle_update(self, event):
        Handles the update of the sensor state by fetching the energy sensor state, updating the samples, and calculating the average energy consumption.

    async async_added_to_hass(self):
        Handles the event when the sensor is added to Home Assistant. Restores the previous state if available.

    async async_update(self, event=None):
        Manually triggers the sensor update.
"""

import logging
import json
from collections import deque
from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from ..const import DOMAIN, ICON
from ..entity import HSEMEntity
from ..utils.misc import async_resolve_entity_id_from_unique_id, convert_to_float

_LOGGER = logging.getLogger(__name__)


class HouseConsumptionEnergyAverageSensor(SensorEntity, HSEMEntity):
    _attr_icon = ICON
    _attr_has_entity_name = True

    def __init__(
        self, config_entry, hour_start, hour_end, sampling_size=5040, max_age_days=7
    ):
        super().__init__(config_entry)
        self._hour_start = hour_start
        self._hour_end = hour_end
        self._max_age = timedelta(days=max_age_days)
        self._unique_id = f"{DOMAIN}_house_consumption_energy_avg_{hour_start:02d}_{hour_end:02d}_{self._max_age.days}d"
        self._hsem_energy_sensor_entity = None
        self._hsem_energy_sensor_state = 0.0
        self._config_entry = config_entry
        self._state = 0.0
        self._samples = []
        self._last_updated = None
        self._entity_is_tracked = False
        self._sampling_size = sampling_size  # Max sample size

    @property
    def name(self):
        return f"House Consumption {self._hour_start:02d}-{self._hour_end:02d} Energy Average {self._max_age.days}d"

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
        return "measurement"

    @property
    def device_class(self):
        return "energy"

    @property
    def extra_state_attributes(self):
        return {
            "last_updated": self._last_updated,
            "unique_id": self._unique_id,
            "energy_sensor_entity": self._hsem_energy_sensor_entity,
            "energy_sensor_state": self._hsem_energy_sensor_state,
            "sampling_size": len(self._samples),
            "max_age_days": self._max_age.days,
            "samples": json.dumps(self._samples),
        }

    async def _handle_update(self, event):

        # Find energy sensor from unique id
        self._hsem_energy_sensor_entity = await async_resolve_entity_id_from_unique_id(
            self,
            f"{DOMAIN}_house_consumption_energy_{self._hour_start:02d}_{self._hour_end:02d}",
        )

        if not self._hsem_energy_sensor_entity:
            _LOGGER.warning(f"Energy sensor not found for {self.name}")
            return

        if self._hsem_energy_sensor_entity and not self._entity_is_tracked:
            async_track_state_change_event(
                self.hass,
                [self._hsem_energy_sensor_entity],
                self.async_update,
            )
            self._entity_is_tracked = True

        if self._hsem_energy_sensor_entity:
            state = self.hass.states.get(self._hsem_energy_sensor_entity)
            if state:
                self._hsem_energy_sensor_state = round(convert_to_float(state.state), 2)
            else:
                self._hsem_energy_sensor_state = None
                _LOGGER.warning(f"Sensor {self._hsem_energy_sensor_entity} not found.")
        state = None

        now = datetime.now()

         # Add the new value and timestamp to samples
        if self._hsem_energy_sensor_state:
            self._samples.append((now.isoformat(), self._hsem_energy_sensor_state))

            # Remove old samples outside of max age
            self._samples = [
                (timestamp, value)
                for timestamp, value in self._samples
                if now - datetime.fromisoformat(timestamp) <= self._max_age
            ]

            # Limit to sampling size
            if len(self._samples) > self._sampling_size:
                self._samples = self._samples[-self._sampling_size:]

            # Calculate the average of the sample values
            if self._samples:
                values = [value for _, value in self._samples]
                self._state = round(sum(values) / len(values), 2)
            else:
                self._state = 0.0  # No samples, so no average

        # Update last update time
        self._last_updated = now.isoformat()

        # Trigger an update in Home Assistant
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        """Handle when sensor is added to Home Assistant."""
        await super().async_added_to_hass()

        old_state = await self.async_get_last_state()
        if old_state is not None:
            try:
                self._state = round(convert_to_float(old_state.state), 2)
                self._last_updated = old_state.attributes.get("last_updated", None)
                # Restore samples from JSON string
                samples_json = old_state.attributes.get("samples", "[]")
                self._samples = json.loads(samples_json)
            except (ValueError, TypeError):
                _LOGGER.warning(f"Invalid old state value for {self.name}")
                self._state = 0.0
                self._samples = []

        # Schedule a periodic update every minute
        async_track_time_interval(self.hass, self._handle_update, timedelta(minutes=1))

    async def async_update(self, event=None):
        """Manually trigger the sensor update."""
        await self._handle_update(event=None)

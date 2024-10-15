import logging
from datetime import datetime

# Importer den nye funktion
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import async_track_state_change_event

from ..entity import HSEMEntity
from ..const import (
    ICON,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_WORKING_MODE,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_STATE_OF_CAPACITY,
    DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL
)
from ..utils.workingmodes import WorkingModes

_LOGGER = logging.getLogger(__name__)

class WorkingModeSensor(SensorEntity, HSEMEntity):
    # Define the attributes of the entity
    _attr_icon = ICON
    _attr_has_entity_name = True

    def __init__(self,
        hsem_huawei_solar_device_id_inverter_1,
        hsem_huawei_solar_device_id_batteries,
        hsem_huawei_solar_batteries_working_mode,
        hsem_huawei_solar_batteries_state_of_capacity,
        hsem_huawei_solar_inverter_active_power_control,
        config_entry):
        super().__init__(config_entry)
        self._hsem_huawei_solar_device_id_inverter_1 = hsem_huawei_solar_device_id_inverter_1
        self._hsem_huawei_solar_device_id_inverter_1_current = None
        self._hsem_huawei_solar_device_id_batteries = hsem_huawei_solar_device_id_batteries
        self._hsem_huawei_solar_device_id_batteries_current = None
        self._hsem_huawei_solar_batteries_working_mode = hsem_huawei_solar_batteries_working_mode
        self._hsem_huawei_solar_batteries_working_mode_current = None
        self._hsem_huawei_solar_batteries_state_of_capacity = hsem_huawei_solar_batteries_state_of_capacity
        self._hsem_huawei_solar_batteries_state_of_capacity_current = None
        self._hsem_huawei_solar_inverter_active_power_control = hsem_huawei_solar_inverter_active_power_control
        self._hsem_huawei_solar_inverter_active_power_control_current = None
        self._state = None
        self._previous_value = None
        self._last_updated = None
        self._last_reset = None
        self._config_entry = config_entry
        self._unique_id = f"hsem_workingmode_sensor"
        self._update_interval = 1
        self._update_settings()

    def _update_settings(self):
        """Fetch updated settings from config_entry options."""
        self._hsem_huawei_solar_device_id_inverter_1 = self._config_entry.options.get(
            "hsem_huawei_solar_device_id_inverter_1"
        )
        self._hsem_huawei_solar_device_id_batteries = self._config_entry.options.get(
            "hsem_huawei_solar_device_id_batteries"
        )
        self._hsem_huawei_solar_batteries_working_mode = self._config_entry.options.get(
            "hsem_huawei_solar_batteries_working_mode", DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_WORKING_MODE
        )
        self._hsem_huawei_solar_batteries_state_of_capacity = self._config_entry.options.get(
            "hsem_huawei_solar_batteries_state_of_capacity", DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_STATE_OF_CAPACITY
        )
        self._hsem_huawei_solar_inverter_active_power_control = self._config_entry.options.get(
            "hsem_huawei_solar_inverter_active_power_control", DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL
        )

        # Log updated settings
        _LOGGER.debug(f"Updated settings: input_sensor={self._hsem_huawei_solar_batteries_working_mode}")

    @property
    def name(self):
        return f"Working Mode Sensor"

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
            "hsem_huawei_solar_device_id_inverter_1": self._hsem_huawei_solar_device_id_inverter_1,
            "hsem_huawei_solar_device_id_batteries": self._hsem_huawei_solar_device_id_batteries,
            "hsem_huawei_solar_batteries_working_mode": self._hsem_huawei_solar_batteries_working_mode,
            "hsem_huawei_solar_batteries_working_mode_current": self._hsem_huawei_solar_batteries_working_mode_current,
            "hsem_huawei_solar_batteries_state_of_capacity": self._hsem_huawei_solar_batteries_state_of_capacity,
            "hsem_huawei_solar_batteries_state_of_capacity_current": self._hsem_huawei_solar_batteries_state_of_capacity_current,
            "hsem_huawei_solar_inverter_active_power_control": self._hsem_huawei_solar_inverter_active_power_control,
            "hsem_huawei_solar_inverter_active_power_control_current": self._hsem_huawei_solar_inverter_active_power_control_current,
            "last_updated": self._last_updated,
            "previous_value": self._previous_value,
            "sensor_update_interval": self._update_interval,
            "unique_id": self._unique_id,
        }

    async def _handle_update(self, event):
        """Handle the sensor state update (for both manual and state change)."""

        # Get the current time
        now = datetime.now()

        # Calculate the update interval to be used
        if self._last_updated is not None:
            self._update_interval = (
                now - datetime.fromisoformat(self._last_updated)
            ).total_seconds()

        # Ensure settings are reloaded if config is changed.
        self._update_settings()

        # Fetch the current value from the input sensors
        input_hsem_huawei_solar_batteries_working_mode = self.hass.states.get(self._hsem_huawei_solar_batteries_working_mode)
        input_hsem_huawei_solar_batteries_state_of_capacity = self.hass.states.get(self._hsem_huawei_solar_batteries_state_of_capacity)
        input_hsem_huawei_solar_inverter_active_power_control = self.hass.states.get(self._hsem_huawei_solar_inverter_active_power_control)

        if input_hsem_huawei_solar_batteries_working_mode is None:
            _LOGGER.warning(f"Sensor {self._hsem_huawei_solar_batteries_working_mode} not found.")
            return

        if input_hsem_huawei_solar_batteries_state_of_capacity is None:
            _LOGGER.warning(f"Sensor {self._hsem_huawei_solar_batteries_state_of_capacity} not found.")
            return

        if input_hsem_huawei_solar_inverter_active_power_control is None:
            _LOGGER.warning(f"Sensor {self._hsem_huawei_solar_inverter_active_power_control} not found.")
            return

        try:
            value_hsem_huawei_solar_batteries_working_mode = input_hsem_huawei_solar_batteries_working_mode.state
        except ValueError:
            _LOGGER.warning(
                f"Invalid value from {self._hsem_huawei_solar_batteries_working_mode}: {input_hsem_huawei_solar_batteries_working_mode.state}"
            )
            return

        try:
            value_hsem_huawei_solar_batteries_state_of_capacity = input_hsem_huawei_solar_batteries_state_of_capacity.state
        except ValueError:
            _LOGGER.warning(
                f"Invalid value from {self._hsem_huawei_solar_batteries_state_of_capacity}: {input_hsem_huawei_solar_batteries_state_of_capacity.state}"
            )
            return

        try:
            value_hsem_huawei_solar_inverter_active_power_control = input_hsem_huawei_solar_inverter_active_power_control.state
        except ValueError:
            _LOGGER.warning(
                f"Invalid value from {self._hsem_huawei_solar_inverter_active_power_control}: {input_hsem_huawei_solar_inverter_active_power_control.state}"
            )
            return

        # Update the previous lowpass value to the new lowpass value
        self._previous_value = self._state

        # Set state to True if the export price is negative, otherwise False
        self._hsem_huawei_solar_batteries_working_mode_current = value_hsem_huawei_solar_batteries_working_mode
        self._hsem_huawei_solar_batteries_state_of_capacity_current = value_hsem_huawei_solar_batteries_state_of_capacity
        self._hsem_huawei_solar_inverter_active_power_control_current = value_hsem_huawei_solar_inverter_active_power_control

        # Start calculating the optiomal working mode for the batteries

        new_working_mode = WorkingModes.MaximizeSelfConsumption.value

        # Set the select sensor value to the working mode
        try:
            # await self.hass.services.async_call(
            #     "select",
            #     "select_option",
            #     {
            #         "entity_id": self._hsem_huawei_solar_batteries_working_mode,
            #         "option": new_working_mode,
            #     },
            # )
            self._state = new_working_mode
        except Exception as e:
            _LOGGER.error(f"Failed to set select sensor state: {e}")
            # Do not update self._state if the call fails

        # Update count and last update time
        self._last_updated = now.isoformat()

        # Trigger an update in Home Assistant
        self.async_write_ha_state()

    async def async_update(self):
        """Manually trigger the sensor update."""
        await self._handle_update(event=None)

    async def async_added_to_hass(self):
        """Handle the sensor being added to Home Assistant."""

        # Restore the previous state if available
        old_state = await self.async_get_last_state()
        if old_state is not None:
            _LOGGER.info(f"Restoring state for {self._unique_id}")
            try:
                self._state = old_state.state
                self._previous_value = self._state
            except (ValueError, TypeError):
                _LOGGER.warning(
                    f"Could not restore state for {self._unique_id}, invalid value: {old_state.state}"
                )
                self._state = None
                self._previous_value = None

            self._last_updated = old_state.attributes.get("last_updated", None)
            self._update_interval = old_state.attributes.get("update_interval", 1)
        else:
            _LOGGER.info(
                f"No previous state found for {self._unique_id}, starting fresh."
            )

        # Start listening for state changes of the input sensor
        if self._hsem_huawei_solar_batteries_working_mode:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_huawei_solar_batteries_working_mode}"
            )
            async_track_state_change_event(
                self.hass, [self._hsem_huawei_solar_batteries_working_mode], self._handle_update
            )
        else:
            _LOGGER.error(
                f"Failed to track state changes, input_sensor is not resolved."
            )

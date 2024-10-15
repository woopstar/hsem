import logging
from datetime import datetime

import voluptuous as vol
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

from ..const import (
    DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT,
    DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL,
    ICON,
)
from ..entity import HSEMEntity

_LOGGER = logging.getLogger(__name__)


async def async_set_grid_export_power_pct(self, device_id, power_percentage):
    """Set the maximum grid export power percentage and handle errors."""

    # Check if the service exists
    if not self.hass.services.has_service(
        "huawei_solar", "set_maximum_feed_grid_power_percent"
    ):
        _LOGGER.error(
            "Service huawei_solar.set_maximum_feed_grid_power_percent not found"
        )

    try:
        # Send the service call to set the maximum grid export power percentage
        await self.hass.services.async_call(
            "huawei_solar",  # Integration providing the service
            "set_maximum_feed_grid_power_percent",  # The action to set grid export power
            {
                "device_id": device_id,  # Device ID of the inverter
                "power_percentage": power_percentage,  # The power percentage to set
            },
            blocking=False,  # Non-blocking call to avoid performance issues
        )

        # Log success message
        _LOGGER.debug(
            f"Updated export power pct to: {power_percentage} for device id: {device_id}"
        )

    except vol.MultipleInvalid as err:
        # Handle validation errors (e.g., invalid device_id)
        _LOGGER.error(
            f"Invalid input data: {err}. Please check the device ID or power percentage."
        )
        raise HomeAssistantError(f"Invalid input data: {err}")

    except HomeAssistantError as err:
        # Handle general Home Assistant errors (e.g., service not found)
        _LOGGER.error(f"Home Assistant error while setting grid export power: {err}")
        raise

    except Exception as err:
        # Handle any other unexpected errors
        _LOGGER.error(f"An unexpected error occurred: {err}")
        raise HomeAssistantError(f"Unexpected error: {err}")


class ExportSensor(BinarySensorEntity, HSEMEntity):
    # Define the attributes of the entity
    _attr_icon = ICON
    _attr_has_entity_name = True

    def __init__(
        self,
        hsem_huawei_solar_device_id_inverter_1,
        hsem_huawei_solar_device_id_inverter_2,
        hsem_huawei_solar_inverter_active_power_control,
        hsem_energi_data_service_export,
        config_entry,
    ):
        super().__init__(config_entry)
        self._hsem_huawei_solar_device_id_inverter_1 = (
            hsem_huawei_solar_device_id_inverter_1
        )
        self._hsem_huawei_solar_device_id_inverter_2 = (
            hsem_huawei_solar_device_id_inverter_2
        )
        self._hsem_huawei_solar_inverter_active_power_control = (
            hsem_huawei_solar_inverter_active_power_control
        )
        self._hsem_huawei_solar_inverter_active_power_control_current = None
        self._price_sensor = hsem_energi_data_service_export
        self._export_price = None
        self._state = True
        self._last_updated = None
        self._last_reset = None
        self._config_entry = config_entry
        self._unique_id = f"hsem_export_sensor"
        self._update_settings()

    def _update_settings(self):
        """Fetch updated settings from config_entry options."""
        self._hsem_huawei_solar_device_id_inverter_1 = self._config_entry.options.get(
            "hsem_huawei_solar_device_id_inverter_1"
        )
        self._hsem_huawei_solar_device_id_inverter_2 = self._config_entry.options.get(
            "hsem_huawei_solar_device_id_inverter_2", None
        )
        self._hsem_huawei_solar_inverter_active_power_control = (
            self._config_entry.options.get(
                "hsem_huawei_solar_inverter_active_power_control",
                DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL,
            )
        )
        self._price_sensor = self._config_entry.options.get(
            "hsem_energi_data_service_export", DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT
        )

        # Log updated settings
        _LOGGER.debug(
            f"Updated settings for export sensor: {self._price_sensor}, {self._hsem_huawei_solar_device_id_inverter_1}, {self._hsem_huawei_solar_device_id_inverter_2}"
        )

    @property
    def name(self):
        return f"Export Sensor"

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def state(self):
        return self._state

    @property
    def is_on(self):
        """Return true if the binary sensor is on."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""

        return {
            "hsem_huawei_solar_device_id_inverter_1": self._hsem_huawei_solar_device_id_inverter_1,
            "hsem_huawei_solar_device_id_inverter_2": self._hsem_huawei_solar_device_id_inverter_2,
            "hsem_huawei_solar_inverter_active_power_control": self._hsem_huawei_solar_inverter_active_power_control,
            "hsem_huawei_solar_inverter_active_power_control_current": self._hsem_huawei_solar_inverter_active_power_control_current,
            "price_sensor": self._price_sensor,
            "export_price": self._export_price,
            "last_updated": self._last_updated,
            "unique_id": self._unique_id,
        }

    async def _handle_update(self, event):
        """Handle the sensor state update (for both manual and state change)."""

        # Ensure settings are reloaded if config is changed.
        self._update_settings()

        input_hsem_huawei_solar_inverter_active_power_control = self.hass.states.get(
            self._hsem_huawei_solar_inverter_active_power_control
        )
        if input_hsem_huawei_solar_inverter_active_power_control is None:
            _LOGGER.warning(
                f"Sensor {self._hsem_huawei_solar_inverter_active_power_control} not found."
            )
            return

        try:
            value_hsem_huawei_solar_inverter_active_power_control = (
                input_hsem_huawei_solar_inverter_active_power_control.state
            )
        except ValueError:
            _LOGGER.warning(
                f"Invalid value from {self._hsem_huawei_solar_inverter_active_power_control}: {input_hsem_huawei_solar_inverter_active_power_control.state}"
            )
            return

        self._hsem_huawei_solar_inverter_active_power_control_current = (
            value_hsem_huawei_solar_inverter_active_power_control
        )

        # Fetch the current value from the input sensor
        input_state = self.hass.states.get(self._price_sensor)
        if input_state is None:
            _LOGGER.warning(f"Sensor {self._price_sensor} not found.")
            return
        try:
            input_value = float(input_state.state)
        except ValueError:
            _LOGGER.warning(
                f"Invalid value from {self._price_sensor}: {input_state.state}"
            )
            return

        # Set state to True if the export price is negative, otherwise False
        self._export_price = input_value
        self._state = self._export_price > 0

        # Set the grid export power percentage based on the state
        if self._state:
            if self._hsem_huawei_solar_device_id_inverter_1:
                await async_set_grid_export_power_pct(
                    self, self._hsem_huawei_solar_device_id_inverter_1, 100
                )
            if self._hsem_huawei_solar_device_id_inverter_2:
                await async_set_grid_export_power_pct(
                    self, self._hsem_huawei_solar_device_id_inverter_2, 100
                )
        else:
            if self._hsem_huawei_solar_device_id_inverter_1:
                await async_set_grid_export_power_pct(
                    self, self._hsem_huawei_solar_device_id_inverter_1, 0
                )
            if self._hsem_huawei_solar_device_id_inverter_2:
                await async_set_grid_export_power_pct(
                    self, self._hsem_huawei_solar_device_id_inverter_2, 0
                )

        # Update the last updated timestamp
        self._last_updated = datetime.now().isoformat()

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
            except (ValueError, TypeError):
                _LOGGER.warning(f"Could not restore state for {self._unique_id}")
                self._state = None

            self._export_price = old_state.attributes.get("export_price", None)
            self._last_updated = old_state.attributes.get("last_updated", None)
        else:
            _LOGGER.info(
                f"No previous state found for {self._unique_id}, starting fresh."
            )

        # Start listening for state changes of the input sensor
        if self._hsem_huawei_solar_device_id_inverter_1:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_huawei_solar_device_id_inverter_1}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_huawei_solar_device_id_inverter_1],
                self._handle_update,
            )

        if self._hsem_huawei_solar_device_id_inverter_2:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_huawei_solar_device_id_inverter_2}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_huawei_solar_device_id_inverter_2],
                self._handle_update,
            )

        if self._hsem_huawei_solar_inverter_active_power_control:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_huawei_solar_inverter_active_power_control}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_huawei_solar_inverter_active_power_control],
                self._handle_update,
            )

        if self._price_sensor:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._price_sensor}"
            )
            async_track_state_change_event(
                self.hass, [self._price_sensor], self._handle_update
            )
        else:
            _LOGGER.error(
                f"Failed to track state changes, hsem_energi_data_service_export is not resolved."
            )

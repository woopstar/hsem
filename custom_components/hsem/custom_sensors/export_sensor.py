import logging
from datetime import datetime

import voluptuous as vol
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

from ..const import (
    DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT,
    DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL,
    DOMAIN,
    ICON,
)
from ..entity import HSEMEntity
from ..utils.huawei import async_set_grid_export_power_pct
from ..utils.misc import get_config_value

_LOGGER = logging.getLogger(__name__)


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
        self._hsem_huawei_solar_inverter_active_power_control_state = None
        self._hsem_energi_data_service_export = hsem_energi_data_service_export
        self._energi_data_service_export_value = None
        self._state = True
        self._last_updated = None
        self._config_entry = config_entry
        self._unique_id = f"{DOMAIN}_export_sensor"
        self._update_settings()

    def _update_settings(self):
        """Fetch updated settings from config_entry options."""
        self._hsem_huawei_solar_device_id_inverter_1 = get_config_value(
            self._config_entry, "hsem_huawei_solar_device_id_inverter_1"
        )
        self._hsem_huawei_solar_device_id_inverter_2 = get_config_value(
            self._config_entry, "hsem_huawei_solar_device_id_inverter_2"
        )
        self._hsem_huawei_solar_inverter_active_power_control = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_inverter_active_power_control",
            DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL,
        )
        self._hsem_energi_data_service_export = get_config_value(
            self._config_entry,
            "hsem_energi_data_service_export",
            DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT,
        )

        # Log updated settings
        _LOGGER.debug(
            f"Updated settings for export sensor: {self._hsem_energi_data_service_export}, {self._hsem_huawei_solar_device_id_inverter_1}, {self._hsem_huawei_solar_device_id_inverter_2}"
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
            "last_updated": self._last_updated,
            "unique_id": self._unique_id,
            "huawei_solar_device_id_inverter_1_id": self._hsem_huawei_solar_device_id_inverter_1,
            "huawei_solar_device_id_inverter_2_id": self._hsem_huawei_solar_device_id_inverter_2,
            "huawei_solar_inverter_active_power_control_entity": self._hsem_huawei_solar_inverter_active_power_control,
            "huawei_solar_inverter_active_power_control_state": self._hsem_huawei_solar_inverter_active_power_control_state,
            "energi_data_service_export_entity": self._hsem_energi_data_service_export,
            "energi_data_service_export_value": self._energi_data_service_export_value,
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

        self._hsem_huawei_solar_inverter_active_power_control_state = (
            value_hsem_huawei_solar_inverter_active_power_control
        )

        # Fetch the current value from the input sensor
        input_state = self.hass.states.get(self._hsem_energi_data_service_export)
        if input_state is None:
            _LOGGER.warning(
                f"Sensor {self._hsem_energi_data_service_export} not found."
            )
            return
        try:
            input_value = float(input_state.state)
        except ValueError:
            _LOGGER.warning(
                f"Invalid value from {self._hsem_energi_data_service_export}: {input_state.state}"
            )
            return

        # Set state to True if the export price is negative, otherwise False
        self._energi_data_service_export_value = input_value
        self._state = self._energi_data_service_export_value > 0

        # Determine the grid export power percentage based on the state
        power_percentage = 100 if self._state else 0

        # List of inverters to update
        inverters = [
            self._hsem_huawei_solar_device_id_inverter_1,
            self._hsem_huawei_solar_device_id_inverter_2,
        ]

        # Loop through the inverters and update the grid export power percentage
        if (
            self._hsem_huawei_solar_inverter_active_power_control_state
            == "Limited to 100.0%"
            and power_percentage != 100
        ) or (
            self._hsem_huawei_solar_inverter_active_power_control_state
            == "Limited to 0.0%"
            and power_percentage != 0
        ):
            for inverter_id in inverters:
                if inverter_id:  # Ensure inverter_id is not None
                    await async_set_grid_export_power_pct(
                        self, inverter_id, power_percentage
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

            self._energi_data_service_export_value = old_state.attributes.get(
                "energi_data_service_export_value", None
            )
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

        if self._hsem_energi_data_service_export:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_energi_data_service_export}"
            )
            async_track_state_change_event(
                self.hass, [self._hsem_energi_data_service_export], self._handle_update
            )
        else:
            _LOGGER.error(
                f"Failed to track state changes, hsem_energi_data_service_export is not resolved."
            )

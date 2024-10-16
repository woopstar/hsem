import logging
from datetime import datetime

import voluptuous as vol
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

from ..const import (
    DEFAULT_HSEM_ENERGI_DATA_SERVICE_IMPORT,
    DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL,
    ICON,
)
from ..entity import HSEMEntity
from ..utils.huawei import async_set_tou_periods
from ..utils.misc import get_config_value

_LOGGER = logging.getLogger(__name__)


class ImportSensor(BinarySensorEntity, HSEMEntity):
    # Define the attributes of the entity
    _attr_icon = ICON
    _attr_has_entity_name = True

    def __init__(
        self,
        hsem_huawei_solar_device_id_batteries,
        hsem_energi_data_service_import,
        config_entry,
    ):
        super().__init__(config_entry)
        self._hsem_huawei_solar_device_id_batteries = (
            hsem_huawei_solar_device_id_batteries
        )
        self._hsem_energi_data_service_import = hsem_energi_data_service_import
        self._import_price = None
        self._state = True
        self._last_updated = None
        self._last_reset = None
        self._config_entry = config_entry
        self._unique_id = f"hsem_import_sensor"
        self._update_settings()

    def _update_settings(self):
        """Fetch updated settings from config_entry options."""
        self._hsem_huawei_solar_device_id_batteries = get_config_value(
            self._config_entry, "hsem_huawei_solar_device_id_batteries"
        )
        self._hsem_energi_data_service_import = get_config_value(
            self._config_entry,
            "hsem_energi_data_service_import",
            DEFAULT_HSEM_ENERGI_DATA_SERVICE_IMPORT,
        )

        # Log updated settings
        _LOGGER.debug(
            f"Updated settings for import sensor: {self._hsem_energi_data_service_import}, {self._hsem_huawei_solar_device_id_batteries}"
        )

    @property
    def name(self):
        return f"Import Sensor"

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
            "huawei_solar_device_id_batteries_id": self._hsem_huawei_solar_device_id_batteries,
            "energi_data_service_import_entity_id": self._hsem_energi_data_service_import,
            "import_price": self._import_price,
            "last_updated": self._last_updated,
            "unique_id": self._unique_id,
        }

    async def _handle_update(self, event):
        """Handle the sensor state update (for both manual and state change)."""

        # Ensure settings are reloaded if config is changed.
        self._update_settings()

        # Fetch the current value from the input sensor
        input_state = self.hass.states.get(self._hsem_energi_data_service_import)
        if input_state is None:
            _LOGGER.warning(
                f"Sensor {self._hsem_energi_data_service_import} not found."
            )
            return
        try:
            input_value = float(input_state.state)
        except ValueError:
            _LOGGER.warning(
                f"Invalid value from {self._hsem_energi_data_service_import}: {input_state.state}"
            )
            return

        # Set state to True if the import price is negative, otherwise False
        self._import_price = input_value
        self._state = self._import_price < 0

        # Force charge the battery
        if self._state:
            tou_modes = ["00:00-23:59/1234567/+"]

            await async_set_tou_periods(
                self, self._hsem_huawei_solar_device_id_batteries, tou_modes
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

            self._import_price = old_state.attributes.get("import_price", None)
            self._last_updated = old_state.attributes.get("last_updated", None)
        else:
            _LOGGER.info(
                f"No previous state found for {self._unique_id}, starting fresh."
            )

        # Start listening for state changes of the input sensor
        if self._hsem_huawei_solar_device_id_batteries:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_huawei_solar_device_id_batteries}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_huawei_solar_device_id_batteries],
                self._handle_update,
            )

        if self._hsem_energi_data_service_import:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_energi_data_service_import}"
            )
            async_track_state_change_event(
                self.hass, [self._hsem_energi_data_service_import], self._handle_update
            )
        else:
            _LOGGER.error(
                f"Failed to track state changes, hsem_energi_data_service_import is not resolved."
            )

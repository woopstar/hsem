import logging
from datetime import datetime

# Importer den nye funktion
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.event import async_track_state_change_event

from ..entity import HSEMEntity
from ..const import (
    ICON,
    DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT
)

_LOGGER = logging.getLogger(__name__)

class ExportSensor(BinarySensorEntity, HSEMEntity):
    # Define the attributes of the entity
    _attr_icon = ICON
    _attr_has_entity_name = True

    def __init__(self, hsem_energi_data_service_export, config_entry):
        super().__init__(config_entry)
        self._price_sensor = hsem_energi_data_service_export
        self._export_price = None
        self._state = True
        self._previous_value = None
        self._last_updated = None
        self._last_reset = None
        self._config_entry = config_entry
        self._unique_id = f"hsem_export_sensor"
        self._update_interval = 1
        self._update_settings()

    def _update_settings(self):
        """Fetch updated settings from config_entry options."""
        self._price_sensor = self._config_entry.options.get(
            "hsem_energi_data_service_export", DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT
        )

        # Log updated settings
        _LOGGER.debug(f"Updated settings: input_sensor={self._price_sensor}")

    @property
    def name(self):
        return f"Export Sensor"

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def is_on(self):
        """Return true if the binary sensor is on."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""

        return {
            "price_sensor": self._price_sensor,
            "export_price": self._export_price,
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

        # Update the previous lowpass value to the new lowpass value
        self._previous_value = self._state

        # Set state to True if the export price is negative, otherwise False
        self._export_price = input_value
        self._state = self._export_price > 0

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
        if self._price_sensor:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._price_sensor}"
            )
            async_track_state_change_event(
                self.hass, [self._price_sensor], self._handle_update
            )
        else:
            _LOGGER.error(
                f"Failed to track state changes, input_sensor is not resolved."
            )

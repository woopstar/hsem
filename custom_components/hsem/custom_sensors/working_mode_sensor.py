import logging
from datetime import datetime

# Importer den nye funktion
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import async_track_state_change_event

from ..const import (
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_STATE_OF_CAPACITY,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_WORKING_MODE,
    ICON,
    DOMAIN
)
from ..entity import HSEMEntity
from ..utils.ha import async_set_select_option
from ..utils.misc import (
    async_resolve_entity_id_from_unique_id,
    convert_to_boolean,
    get_config_value,
)
from ..utils.workingmodes import WorkingModes

_LOGGER = logging.getLogger(__name__)


class WorkingModeSensor(SensorEntity, HSEMEntity):
    # Define the attributes of the entity
    _attr_icon = ICON
    _attr_has_entity_name = True

    def __init__(
        self,
        hsem_huawei_solar_device_id_inverter_1,
        hsem_huawei_solar_device_id_inverter_2,
        hsem_huawei_solar_device_id_batteries,
        hsem_huawei_solar_batteries_working_mode,
        hsem_huawei_solar_batteries_state_of_capacity,
        config_entry,
    ):
        super().__init__(config_entry)
        self._hsem_huawei_solar_device_id_inverter_1 = (
            hsem_huawei_solar_device_id_inverter_1
        )
        self._hsem_huawei_solar_device_id_inverter_2 = (
            hsem_huawei_solar_device_id_inverter_2
        )
        self._hsem_huawei_solar_device_id_batteries = (
            hsem_huawei_solar_device_id_batteries
        )
        self._hsem_huawei_solar_batteries_working_mode = (
            hsem_huawei_solar_batteries_working_mode
        )
        self._hsem_huawei_solar_batteries_working_mode_current = None
        self._hsem_huawei_solar_batteries_state_of_capacity = (
            hsem_huawei_solar_batteries_state_of_capacity
        )
        self._hsem_huawei_solar_batteries_state_of_capacity_current = None
        self._import_sensor = None
        self._state = None
        self._last_updated = None
        self._last_reset = None
        self._config_entry = config_entry
        self._unique_id = f"{DOMAIN}_workingmode_sensor"
        self._update_settings()

    def _update_settings(self):
        """Fetch updated settings from config_entry options."""
        self._hsem_huawei_solar_device_id_inverter_1 = get_config_value(
            self._config_entry, "hsem_huawei_solar_device_id_inverter_1"
        )
        self._hsem_huawei_solar_device_id_inverter_2 = get_config_value(
            self._config_entry, "hsem_huawei_solar_device_id_inverter_2"
        )
        self._hsem_huawei_solar_device_id_batteries = get_config_value(
            self._config_entry, "hsem_huawei_solar_device_id_batteries"
        )
        self._hsem_huawei_solar_batteries_working_mode = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_batteries_working_mode",
            DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_WORKING_MODE,
        )
        self._hsem_huawei_solar_batteries_state_of_capacity = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_batteries_state_of_capacity",
            DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_STATE_OF_CAPACITY,
        )

        # Log updated settings
        _LOGGER.debug(
            f"Updated settings: input_sensor={self._hsem_huawei_solar_batteries_working_mode}"
        )

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
            "huawei_solar_device_id_inverter_1_id": self._hsem_huawei_solar_device_id_inverter_1,
            "huawei_solar_device_id_inverter_2_id": self._hsem_huawei_solar_device_id_inverter_2,
            "huawei_solar_device_id_batteries_id": self._hsem_huawei_solar_device_id_batteries,
            "huawei_solar_batteries_working_mode__entity_id": self._hsem_huawei_solar_batteries_working_mode,
            "huawei_solar_batteries_working_mode_current": self._hsem_huawei_solar_batteries_working_mode_current,
            "huawei_solar_batteries_state_of_capacity__entity_id": self._hsem_huawei_solar_batteries_state_of_capacity,
            "huawei_solar_batteries_state_of_capacity_current": self._hsem_huawei_solar_batteries_state_of_capacity_current,
            "import_sensor_entity_id: ": self._import_sensor,
            "last_updated": self._last_updated,
            "unique_id": self._unique_id,
        }

    async def _handle_update(self, event):
        """Handle the sensor state update (for both manual and state change)."""

        # Ensure settings are reloaded if config is changed.
        self._update_settings()

        # Fetch the import sensor from the unique id of it.
        self._import_sensor = await async_resolve_entity_id_from_unique_id(
            self, "{DOMAIN}_import_sensor", "binary_sensor"
        )

        if not self._import_sensor:
            _LOGGER.warning("Entity with unique_id hsem_import_sensor not found.")
            return

        # Fetch the current value from the input sensor
        _import_sensor_state = self.hass.states.get(self._import_sensor).state
        if _import_sensor_state is None:
            _LOGGER.warning(
                f"Sensor {self._import_sensor} not ready or not found. Skipping update."
            )
            return

        import_sensor_boolean = convert_to_boolean(_import_sensor_state)

        # Fetch the current value from the input sensors
        input_hsem_huawei_solar_batteries_working_mode = self.hass.states.get(
            self._hsem_huawei_solar_batteries_working_mode
        )
        input_hsem_huawei_solar_batteries_state_of_capacity = self.hass.states.get(
            self._hsem_huawei_solar_batteries_state_of_capacity
        )

        if input_hsem_huawei_solar_batteries_working_mode is None:
            _LOGGER.warning(
                f"Sensor {self._hsem_huawei_solar_batteries_working_mode} not found."
            )
            return

        if input_hsem_huawei_solar_batteries_state_of_capacity is None:
            _LOGGER.warning(
                f"Sensor {self._hsem_huawei_solar_batteries_state_of_capacity} not found."
            )
            return

        try:
            value_hsem_huawei_solar_batteries_working_mode = (
                input_hsem_huawei_solar_batteries_working_mode.state
            )
        except ValueError:
            _LOGGER.warning(
                f"Invalid value from {self._hsem_huawei_solar_batteries_working_mode}: {input_hsem_huawei_solar_batteries_working_mode.state}"
            )
            return

        try:
            value_hsem_huawei_solar_batteries_state_of_capacity = (
                input_hsem_huawei_solar_batteries_state_of_capacity.state
            )
        except ValueError:
            _LOGGER.warning(
                f"Invalid value from {self._hsem_huawei_solar_batteries_state_of_capacity}: {input_hsem_huawei_solar_batteries_state_of_capacity.state}"
            )
            return

        # Set current values from input sensors
        self._hsem_huawei_solar_batteries_working_mode_current = (
            value_hsem_huawei_solar_batteries_working_mode
        )
        self._hsem_huawei_solar_batteries_state_of_capacity_current = (
            value_hsem_huawei_solar_batteries_state_of_capacity
        )

        # Start calculating the optiomal working mode for the batteries

        # If the import sensor is on, set the working mode to TimeOfUse to force charge the batteries
        if import_sensor_boolean:
            _LOGGER.warning(
                f"Import sensor is on. Set mode to TimeOfUse. _import_sensor_state={import_sensor_boolean}"
            )
            self._state = WorkingModes.TimeOfUse.value
            await async_set_select_option(
                self, self._hsem_huawei_solar_batteries_working_mode, self._state
            )
        else:
            _LOGGER.warning(f"Set default mode to MaximizeSelfConsumption.")
            new_working_mode = WorkingModes.MaximizeSelfConsumption.value

        # Update last update time
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
                _LOGGER.warning(
                    f"Could not restore state for {self._unique_id}, invalid value: {old_state.state}"
                )
                self._state = None

            self._last_updated = old_state.attributes.get("last_updated", None)
        else:
            _LOGGER.info(
                f"No previous state found for {self._unique_id}, starting fresh."
            )

        # Start listening for state changes of the input sensors
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

        if self._hsem_huawei_solar_device_id_batteries:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_huawei_solar_device_id_batteries}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_huawei_solar_device_id_batteries],
                self._handle_update,
            )

        if self._hsem_huawei_solar_batteries_working_mode:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_huawei_solar_batteries_working_mode}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_huawei_solar_batteries_working_mode],
                self._handle_update,
            )

        if self._hsem_huawei_solar_batteries_state_of_capacity:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_huawei_solar_batteries_state_of_capacity}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_huawei_solar_batteries_state_of_capacity],
                self._handle_update,
            )

        if self._import_sensor:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._import_sensor}"
            )
            async_track_state_change_event(
                self.hass,
                [self._import_sensor],
                self._handle_update,
            )

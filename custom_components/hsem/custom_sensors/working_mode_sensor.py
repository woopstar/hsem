import logging
from datetime import datetime, timedelta

# Importer den nye funktion
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from ..const import (
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_STATE_OF_CAPACITY,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_WORKING_MODE,
    DOMAIN,
    ICON,
)
from ..entity import HSEMEntity
from ..utils.ha import async_set_select_option
from ..utils.huawei import async_set_tou_periods
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
        hsem_ev_charger_status,
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
        self._hsem_ev_charger_status = hsem_ev_charger_status
        self._hsem_ev_charger_status_current = False
        self._import_sensor = None
        self._import_sensor_current = None
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
        self._hsem_ev_charger_status = get_config_value(
            self._config_entry, "hsem_ev_charger_status"
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
            "ev_charger_status_entity_id": self._hsem_ev_charger_status,
            "ev_charger_status_current": self._hsem_ev_charger_status_current,
            "import_sensor_entity_id: ": self._import_sensor,
            "import_sensor_current: ": self._import_sensor_current,
            "last_updated": self._last_updated,
            "unique_id": self._unique_id,
        }

    async def _handle_update(self, event):
        """Handle the sensor state update (for both manual and state change)."""

        # Ensure settings are reloaded if config is changed.
        self._update_settings()

        # Fetch the import sensor from the unique id of it.
        self._import_sensor = await async_resolve_entity_id_from_unique_id(
            self, f"{DOMAIN}_import_sensor", "binary_sensor"
        )

        # Fetch the current value from the import sensor
        if self._import_sensor:
            import_sensor_state = self.hass.states.get(self._import_sensor)
            if import_sensor_state:
                self._import_sensor_current = convert_to_boolean(
                    import_sensor_state.state
                )
            else:
                _LOGGER.warning(f"Import sensor {self._import_sensor} not found.")

        # Fetch the current value from the EV charger status sensor
        if self._hsem_ev_charger_status:
            ev_charger_state = self.hass.states.get(self._hsem_ev_charger_status)
            if ev_charger_state:
                self._hsem_ev_charger_status_current = convert_to_boolean(
                    ev_charger_state.state
                )
            else:
                _LOGGER.warning(
                    f"EV charger status sensor {self._hsem_ev_charger_status} not found."
                )

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

        # Set the working mode based on the input sensors
        await self.async_set_working_mode()

        # Update last update time
        self._last_updated = datetime.now().isoformat()

        # Trigger an update in Home Assistant
        self.async_write_ha_state()

    async def async_set_working_mode(self):
        # Define TOU modes for different scenarios
        import_sensor_tou_modes = ["00:00-23:59/1234567/+"]
        ev_charger_tou_modes = ["00:00-00:01/1234567/+"]
        default_tou_modes = [
            "00:01-05:59/1234567/+",
            "06:00-10:00/1234567/-",
            "17:00-23:59/1234567/-",
        ]

        # Determine the appropriate TOU modes and working mode state. In priority order:
        if self._import_sensor_current:
            tou_modes = import_sensor_tou_modes
            working_mode = WorkingModes.TimeOfUse.value
            _LOGGER.warning(
                f"Import sensor active. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}"
            )

        elif self._hsem_ev_charger_status_current:
            tou_modes = ev_charger_tou_modes
            working_mode = WorkingModes.TimeOfUse.value
            _LOGGER.warning(
                f"EV Charger active. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}"
            )

        else:
            tou_modes = default_tou_modes
            working_mode = WorkingModes.MaximizeSelfConsumption.value
            _LOGGER.warning(
                f"Default settings. TOU Periods: {tou_modes} and Working Mode: {working_mode}"
            )

        # Apply TOU periods and working mode
        await async_set_tou_periods(
            self, self._hsem_huawei_solar_device_id_batteries, tou_modes
        )

        if self._hsem_huawei_solar_batteries_working_mode_current != working_mode:
            await async_set_select_option(
                self, self._hsem_huawei_solar_batteries_working_mode, working_mode
            )

        self._state = working_mode

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

        if self._hsem_ev_charger_status:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_ev_charger_status}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_ev_charger_status],
                self._handle_update,
            )

        # Schedule a periodic update every 5 minutes
        async_track_time_interval(self.hass, self._handle_update, timedelta(minutes=5))

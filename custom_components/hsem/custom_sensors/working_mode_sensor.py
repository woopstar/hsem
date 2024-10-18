import logging
from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from ..const import (
    DEFAULT_HSEM_DEFAULT_TOU_MODES,
    DEFAULT_HSEM_EV_CHARGER_TOU_MODES,
    DEFAULT_HSEM_IMPORT_SENSOR_TOU_MODES,
    DEFAULT_HSEM_MONTHS_SUMMER,
    DEFAULT_HSEM_MONTHS_WINTER_SPRING,
    DOMAIN,
    HOUSE_CONSUMPTION_ENERGY_WEIGHT_3D,
    HOUSE_CONSUMPTION_ENERGY_WEIGHT_7D,
    HOUSE_CONSUMPTION_ENERGY_WEIGHT_14D,
    ICON,
)
from ..entity import HSEMEntity
from ..utils.ha import async_set_select_option
from ..utils.huawei import async_set_grid_export_power_pct, async_set_tou_periods
from ..utils.misc import (
    async_resolve_entity_id_from_unique_id,
    convert_to_boolean,
    convert_to_float,
    get_config_value,
)
from ..utils.workingmodes import WorkingModes

_LOGGER = logging.getLogger(__name__)


class WorkingModeSensor(SensorEntity, HSEMEntity):
    # Define the attributes of the entity
    _attr_icon = ICON
    _attr_has_entity_name = True

    def __init__(self, config_entry):
        super().__init__(config_entry)

        # set config entry and state
        self._config_entry = config_entry
        self._state = None

        # Initialize all attributes to None or some default value
        self._hsem_huawei_solar_device_id_inverter_1 = None
        self._hsem_huawei_solar_device_id_inverter_2 = None
        self._hsem_huawei_solar_device_id_batteries = None
        self._hsem_huawei_solar_batteries_working_mode = None
        self._hsem_huawei_solar_batteries_state_of_capacity = None
        self._hsem_house_consumption_power = None
        self._hsem_solar_production_power = None
        self._hsem_ev_charger_status = None
        self._hsem_ev_charger_power = None
        self._hsem_solcast_pv_forecast_forecast_today = None
        self._hsem_battery_max_capacity = None
        self._hsem_energi_data_service_import = None
        self._hsem_energi_data_service_export = None
        self._hsem_huawei_solar_inverter_active_power_control = None
        self._hsem_huawei_solar_batteries_working_mode_state = None
        self._hsem_huawei_solar_batteries_state_of_capacity_state = None
        self._hsem_house_power_includes_ev_charger_power = None
        self._hsem_ev_charger_status_state = False
        self._hsem_ev_charger_power_state = False
        self._hsem_house_consumption_power_state = 0.0
        self._hsem_solar_production_power_state = 0.0
        self._hsem_huawei_solar_inverter_active_power_control_state = None
        self._hsem_net_consumption = 0.0
        self._hsem_net_consumption_with_ev = 0.0
        self._hsem_huawei_solar_batteries_maximum_charging_power = None
        self._hsem_huawei_solar_batteries_maximum_charging_power_state = None
        self._hsem_battery_conversion_loss = None
        self._hsem_battery_remaining_charge = 0.0
        self._hsem_energi_data_service_import_state = 0.0
        self._hsem_energi_data_service_export_state = 0.0
        self._hsem_morning_energy_need = 0.0
        self._last_changed_mode = None
        self._last_updated = None

        self._hourly_calculations = {
            f"{hour:02d}-{(hour + 1) % 24:02d}": {
                "avg_house_consumption": 0.0,
                "solcast_pv_estimate": 0.0,
                "estimated_net_consumption": 0.0,
                "import_price": 0.0,
                "export_price": 0.0,
                "recommendation": None,
            }
            for hour in range(24)
        }
        self._unique_id = f"{DOMAIN}_workingmode_sensor"
        self._update_settings()

    def set_hsem_huawei_solar_device_id_inverter_1(self, value):
        self._hsem_huawei_solar_device_id_inverter_1 = value

    def set_hsem_huawei_solar_device_id_inverter_2(self, value):
        self._hsem_huawei_solar_device_id_inverter_2 = value

    def set_hsem_huawei_solar_device_id_batteries(self, value):
        self._hsem_huawei_solar_device_id_batteries = value

    def set_hsem_huawei_solar_batteries_working_mode(self, value):
        self._hsem_huawei_solar_batteries_working_mode = value

    def set_hsem_huawei_solar_batteries_state_of_capacity(self, value):
        self._hsem_huawei_solar_batteries_state_of_capacity = value

    def set_hsem_house_consumption_power(self, value):
        self._hsem_house_consumption_power = value

    def set_hsem_solar_production_power(self, value):
        self._hsem_solar_production_power = value

    def set_hsem_ev_charger_status(self, value):
        self._hsem_ev_charger_status = value

    def set_hsem_ev_charger_power(self, value):
        self._hsem_ev_charger_power = value

    def set_hsem_solcast_pv_forecast_forecast_today(self, value):
        self._hsem_solcast_pv_forecast_forecast_today = value

    def set_hsem_battery_max_capacity(self, value):
        self._hsem_battery_max_capacity = value

    def set_hsem_energi_data_service_import(self, value):
        self._hsem_energi_data_service_import = value

    def set_hsem_energi_data_service_export(self, value):
        self._hsem_energi_data_service_export = value

    def set_hsem_huawei_solar_inverter_active_power_control(self, value):
        self._hsem_huawei_solar_inverter_active_power_control = value

    def set_hsem_huawei_solar_batteries_working_mode_state(self, value):
        self._hsem_huawei_solar_batteries_working_mode_state = value

    def set_hsem_battery_conversion_loss(self, value):
        self._hsem_battery_conversion_loss = value

    def set_hsem_house_power_includes_ev_charger_power(self, value):
        self._hsem_house_power_includes_ev_charger_power = value

    def set_hsem_huawei_solar_batteries_maximum_charging_power(self, value):
        self._hsem_huawei_solar_batteries_maximum_charging_power = value

    def set_hsem_morning_energy_need(self, value):
        self._hsem_morning_energy_need = value

    def _update_settings(self):
        """Fetch updated settings from config_entry options."""
        self.set_hsem_huawei_solar_device_id_inverter_1(
            get_config_value(
                self._config_entry, "hsem_huawei_solar_device_id_inverter_1"
            )
        )
        self.set_hsem_huawei_solar_device_id_inverter_2(
            get_config_value(
                self._config_entry, "hsem_huawei_solar_device_id_inverter_2"
            )
        )
        self.set_hsem_huawei_solar_device_id_batteries(
            get_config_value(
                self._config_entry, "hsem_huawei_solar_device_id_batteries"
            )
        )
        self.set_hsem_huawei_solar_batteries_working_mode(
            get_config_value(
                self._config_entry, "hsem_huawei_solar_batteries_working_mode"
            )
        )
        self.set_hsem_huawei_solar_batteries_state_of_capacity(
            get_config_value(
                self._config_entry,
                "hsem_huawei_solar_batteries_state_of_capacity",
            )
        )
        self.set_hsem_house_consumption_power(
            get_config_value(
                self._config_entry,
                "hsem_house_consumption_power",
            )
        )
        self.set_hsem_solar_production_power(
            get_config_value(
                self._config_entry,
                "hsem_solar_production_power",
            )
        )
        self.set_hsem_ev_charger_status(
            get_config_value(self._config_entry, "hsem_ev_charger_status")
        )
        self.set_hsem_solcast_pv_forecast_forecast_today(
            get_config_value(
                self._config_entry,
                "hsem_solcast_pv_forecast_forecast_today",
            )
        )
        self.set_hsem_battery_max_capacity(
            get_config_value(
                self._config_entry,
                "hsem_battery_max_capacity",
            )
        )
        self.set_hsem_energi_data_service_import(
            get_config_value(
                self._config_entry,
                "hsem_energi_data_service_import",
            )
        )
        self.set_hsem_energi_data_service_export(
            get_config_value(
                self._config_entry,
                "hsem_energi_data_service_export",
            )
        )
        self.set_hsem_huawei_solar_inverter_active_power_control(
            get_config_value(
                self._config_entry,
                "hsem_huawei_solar_inverter_active_power_control",
            )
        )
        self.set_hsem_house_power_includes_ev_charger_power(
            get_config_value(
                self._config_entry,
                "hsem_house_power_includes_ev_charger_power",
            )
        )
        self.set_hsem_ev_charger_power(
            get_config_value(
                self._config_entry,
                "hsem_ev_charger_power",
            )
        )
        self.set_hsem_battery_conversion_loss(
            get_config_value(
                self._config_entry,
                "hsem_battery_conversion_loss",
            )
        )
        self.set_hsem_huawei_solar_batteries_maximum_charging_power(
            get_config_value(
                self._config_entry,
                "hsem_huawei_solar_batteries_maximum_charging_power",
            )
        )
        self.set_hsem_morning_energy_need(
            get_config_value(
                self._config_entry,
                "hsem_morning_energy_need",
            )
        )

        if self._hsem_huawei_solar_device_id_inverter_2 is not None:
            if len(self._hsem_huawei_solar_device_id_inverter_2) == 0:
                self.set_hsem_huawei_solar_device_id_inverter_2(None)

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
            "last_updated": self._last_updated,
            "last_changed_mode": self._last_changed_mode,
            "unique_id": self._unique_id,
            "huawei_solar_device_id_inverter_1_id": self._hsem_huawei_solar_device_id_inverter_1,
            "huawei_solar_device_id_inverter_2_id": self._hsem_huawei_solar_device_id_inverter_2,
            "huawei_solar_device_id_batteries_id": self._hsem_huawei_solar_device_id_batteries,
            "huawei_solar_batteries_working_mode_entity": self._hsem_huawei_solar_batteries_working_mode,
            "huawei_solar_batteries_working_mode_state": self._hsem_huawei_solar_batteries_working_mode_state,
            "huawei_solar_batteries_state_of_capacity_entity": self._hsem_huawei_solar_batteries_state_of_capacity,
            "huawei_solar_batteries_state_of_capacity_state": self._hsem_huawei_solar_batteries_state_of_capacity_state,
            "huawei_solar_inverter_active_power_control_state_entity": self._hsem_huawei_solar_inverter_active_power_control,
            "huawei_solar_inverter_active_power_control_state_state": self._hsem_huawei_solar_inverter_active_power_control_state,
            "house_consumption_power_entity": self._hsem_house_consumption_power,
            "house_consumption_power_state": self._hsem_house_consumption_power_state,
            "solar_production_power_entity": self._hsem_solar_production_power,
            "solar_production_power_state": self._hsem_solar_production_power_state,
            "net_consumption": self._hsem_net_consumption,
            "net_consumption_with_ev": self._hsem_net_consumption_with_ev,
            "energi_data_service_import_entity": self._hsem_energi_data_service_import,
            "energi_data_service_import_state": self._hsem_energi_data_service_import_state,
            "energi_data_service_export_entity": self._hsem_energi_data_service_export,
            "energi_data_service_export_value": self._hsem_energi_data_service_export_state,
            "battery_max_capacity": self._hsem_battery_max_capacity,
            "battery_remaining_charge": self._hsem_battery_remaining_charge,
            "battery_maximum_charging_power_entity": self._hsem_huawei_solar_batteries_maximum_charging_power,
            "battery_maximum_charging_power_state": self._hsem_huawei_solar_batteries_maximum_charging_power_state,
            "battery_conversion_loss": self._hsem_battery_conversion_loss,
            "ev_charger_status_entity": self._hsem_ev_charger_status,
            "ev_charger_status_state": self._hsem_ev_charger_status_state,
            "ev_charger_power_entity": self._hsem_ev_charger_power,
            "ev_charger_power_state": self._hsem_ev_charger_power_state,
            "house_power_includes_ev_charger_power": self._hsem_house_power_includes_ev_charger_power,
            "morning_energy_need": self._hsem_morning_energy_need,
            "solcast_pv_forecast_forecast_today_entity": self._hsem_solcast_pv_forecast_forecast_today,
            "hourly_calculations": self._hourly_calculations,
        }

    async def _handle_update(self, event):
        """Handle the sensor state update (for both manual and state change)."""

        # Get the current time
        now = datetime.now()

        # Ensure settings are reloaded if config is changed.
        self._update_settings()

        # Fetch the current value from the EV charger status sensor
        if self._hsem_ev_charger_status:
            state = self.hass.states.get(self._hsem_ev_charger_status)
            if state:
                self._hsem_ev_charger_status_state = convert_to_boolean(state.state)
            else:
                _LOGGER.warning(
                    f"EV charger status sensor {self._hsem_ev_charger_status} not found."
                )
        state = None

        # Fetch the current value from the house consumption power sensor
        if self._hsem_house_consumption_power:
            state = self.hass.states.get(self._hsem_house_consumption_power)
            if state:
                self._hsem_house_consumption_power_state = round(
                    convert_to_float(state.state), 2
                )
            else:
                _LOGGER.warning(
                    f"Sensor {self._hsem_house_consumption_power} not found."
                )
        state = None

        # Fetch the current value from the solar production power sensor
        if self._hsem_solar_production_power:
            state = self.hass.states.get(self._hsem_solar_production_power)
            if state:
                self._hsem_solar_production_power_state = round(
                    convert_to_float(state.state), 2
                )
            else:
                _LOGGER.warning(
                    f"Sensor {self._hsem_solar_production_power} not found."
                )
        state = None

        # fetch the current value from the working mode sensor
        if self._hsem_huawei_solar_batteries_working_mode:
            state = self.hass.states.get(self._hsem_huawei_solar_batteries_working_mode)
            if state:
                self._hsem_huawei_solar_batteries_working_mode_state = state.state
            else:
                _LOGGER.warning(
                    f"Sensor {self._hsem_huawei_solar_batteries_working_mode} not found."
                )
        state = None

        # Fetch the current value from the state of capacity sensor
        if self._hsem_huawei_solar_batteries_state_of_capacity:
            state = self.hass.states.get(
                self._hsem_huawei_solar_batteries_state_of_capacity
            )
            if state:
                self._hsem_huawei_solar_batteries_state_of_capacity_state = round(
                    convert_to_float(state.state), 0
                )
            else:
                _LOGGER.warning(
                    f"Sensor {self._hsem_huawei_solar_batteries_state_of_capacity} not found."
                )
        state = None

        # Fetch the current value from the energi data service import sensor
        if self._hsem_energi_data_service_import:
            state = self.hass.states.get(self._hsem_energi_data_service_import)
            if state:
                self._hsem_energi_data_service_import_state = round(
                    convert_to_float(state.state), 3
                )
            else:
                _LOGGER.warning(
                    f"Sensor {self._hsem_energi_data_service_import} not found."
                )
        state = None

        # Fetch the current value from the energi data service export sensor
        if self._hsem_energi_data_service_export:
            state = self.hass.states.get(self._hsem_energi_data_service_export)
            if state:
                self._hsem_energi_data_service_export_state = round(
                    convert_to_float(state.state), 3
                )
            else:
                _LOGGER.warning(
                    f"Sensor {self._hsem_energi_data_service_export} not found."
                )
        state = None

        # Fetch the current value from the energi data service export sensor
        if self._hsem_huawei_solar_inverter_active_power_control:
            state = self.hass.states.get(
                self._hsem_huawei_solar_inverter_active_power_control
            )
            if state:
                self._hsem_huawei_solar_inverter_active_power_control_state = (
                    state.state
                )
            else:
                _LOGGER.warning(
                    f"Sensor {self._hsem_huawei_solar_inverter_active_power_control} not found."
                )
        state = None

        # Fetch the current value from the battery maximum charging power sensor
        if self._hsem_huawei_solar_batteries_maximum_charging_power:
            state = self.hass.states.get(
                self._hsem_huawei_solar_batteries_maximum_charging_power
            )
            if state:
                self._hsem_huawei_solar_batteries_maximum_charging_power_state = round(convert_to_float(
                    state.state
                ), 0)
            else:
                _LOGGER.warning(
                    f"Sensor {self._hsem_huawei_solar_batteries_maximum_charging_power} not found."
                )
        state = None

        # Fetch the current value from the battery maximum charging power sensor
        if self._hsem_ev_charger_power:
            state = self.hass.states.get(
                self._hsem_ev_charger_power
            )
            if state:
                self._hsem_ev_charger_power_state = round(convert_to_float(
                    state.state
                ), 2)
            else:
                _LOGGER.warning(
                    f"Sensor {self._hsem_ev_charger_power} not found."
                )
        state = None

        # Calculate the net consumption without the EV charger power
        if self._hsem_house_power_includes_ev_charger_power:
            self._hsem_net_consumption_with_ev = (
                self._hsem_solar_production_power_state
                - (self._hsem_house_consumption_power_state)
            )
            self._hsem_net_consumption = (
                self._hsem_solar_production_power_state
                - (self._hsem_house_consumption_power_state - self._hsem_ev_charger_power_state)
            )
        else:
            self._hsem_net_consumption_with_ev = (
                self._hsem_solar_production_power_state
                - (self._hsem_house_consumption_power_state + self._hsem_ev_charger_power_state)
            )
            self._hsem_net_consumption = (
                self._hsem_solar_production_power_state
                - self._hsem_house_consumption_power_state
            )


        # Calculate the remaining battery capacity
        if self._hsem_battery_max_capacity is not None and self._hsem_huawei_solar_batteries_state_of_capacity_state is not None:
            self._hsem_battery_remaining_charge = round(
                (
                    (
                        100
                        -
                        self._hsem_huawei_solar_batteries_state_of_capacity_state

                    )
                    / 100
                    * self._hsem_battery_max_capacity
                ),
                2)

        # calculate the hourly data from power sensors
        await self.async_calculate_hourly_data()

        # calculate the solcast forecast for today
        await self.async_calculate_solcast_forecast()

        # calculate the hourly net consumption between house consumption and solar production
        await self.async_calculate_hourly_net_consumption()

        # calculate the hourly import price
        await self.async_calculate_hourly_import_price()

        # calculate the hourly export price
        await self.async_calculate_hourly_export_price()

        # calculate the optimization strategy
        await self.async_optimization_strategy()

        # Set the inverter power control mode
        if self._hsem_energi_data_service_export_state is not None:
            await self.async_set_inverter_power_control()

        # calculate the last time working mode was changed
        if self._last_changed_mode is not None:
            last_changed_mode_seconds = (
                now - datetime.fromisoformat(self._last_changed_mode)
            ).total_seconds()
        else:
            last_changed_mode_seconds = 0

        # Set the working mode
        if last_changed_mode_seconds > 300 or self._last_changed_mode is None:
            await self.async_set_working_mode()
            self._last_changed_mode = datetime.now().isoformat()

        # Update last update time
        self._last_updated = datetime.now().isoformat()

        # Trigger an update in Home Assistant
        self.async_write_ha_state()

    async def async_set_inverter_power_control(self):
        # Determine the grid export power percentage based on the state
        export_power_percentage = (
            100 if self._hsem_energi_data_service_export_state > 0 else 0
        )

        # List of inverters to update
        inverters = [
            self._hsem_huawei_solar_device_id_inverter_1,
            self._hsem_huawei_solar_device_id_inverter_2,
        ]

        if (
            self._hsem_huawei_solar_inverter_active_power_control_state
            == "Limited to 100.0%"
            and export_power_percentage != 100
        ) or (
            self._hsem_huawei_solar_inverter_active_power_control_state
            == "Limited to 0.0%"
            and export_power_percentage != 0
        ):
            for inverter_id in inverters:
                if inverter_id:
                    await async_set_grid_export_power_pct(
                        self, inverter_id, export_power_percentage
                    )

    async def async_set_working_mode(self):
        # Determine the current month and hour
        current_month = datetime.now().month

        # Determine the appropriate TOU modes and working mode state. In priority order:
        if self._hsem_energi_data_service_import_state < 0:
            # Negative import price. Force charge battery
            tou_modes = DEFAULT_HSEM_IMPORT_SENSOR_TOU_MODES
            working_mode = WorkingModes.TimeOfUse.value
            _LOGGER.debug(
                f"Import price is negative. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}"
            )

        elif self._hsem_ev_charger_status_state:
            # EV Charger is active. Disable battery discharge
            tou_modes = DEFAULT_HSEM_EV_CHARGER_TOU_MODES
            working_mode = WorkingModes.TimeOfUse.value
            _LOGGER.debug(
                f"EV Charger is active. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}"
            )
        elif self._hsem_net_consumption > 0:
            # Positive net consumption. Charge battery from Solar
            working_mode = WorkingModes.MaximizeSelfConsumption.value
            _LOGGER.debug(
                f"Positive net consumption. Working Mode: {working_mode}, Solar Production: {self._hsem_solar_production_power_state}, House Consumption: {self._hsem_house_consumption_power_state}, Net Consumption: {self._hsem_net_consumption}"
            )
        else:
            # Winter/Spring settings
            if current_month in DEFAULT_HSEM_MONTHS_WINTER_SPRING:
                tou_modes = DEFAULT_HSEM_DEFAULT_TOU_MODES
                working_mode = WorkingModes.TimeOfUse.value
                _LOGGER.debug(
                    f"Default winter/spring settings. TOU Periods: {tou_modes} and Working Mode: {working_mode}"
                )

            # Summer settings
            if current_month in DEFAULT_HSEM_MONTHS_SUMMER:
                working_mode = WorkingModes.MaximizeSelfConsumption.value
                _LOGGER.debug(f"Default summer settings. Working Mode: {working_mode}")

        # Apply TOU periods if working mode is TOU
        if working_mode == WorkingModes.TimeOfUse.value:
            await async_set_tou_periods(
                self, self._hsem_huawei_solar_device_id_batteries, tou_modes
            )

        # Only apply working mode if it has changed
        if self._hsem_huawei_solar_batteries_working_mode_state != working_mode:
            await async_set_select_option(
                self, self._hsem_huawei_solar_batteries_working_mode, working_mode
            )

        self._state = working_mode

    async def async_calculate_hourly_data(self):
        """Calculate the weighted hourly data for the sensor using both 3-day and 7-day HouseConsumptionEnergyAverageSensors."""

        for hour in range(24):
            hour_start = hour
            hour_end = (hour + 1) % 24
            time_range = f"{hour_start:02d}-{hour_end:02d}"

            # Construct unique_ids for the 3d, 7d, and 14d sensors
            unique_id_3d = f"{DOMAIN}_house_consumption_energy_avg_{hour_start:02d}_{hour_end:02d}_3d"
            unique_id_7d = f"{DOMAIN}_house_consumption_energy_avg_{hour_start:02d}_{hour_end:02d}_7d"
            unique_id_14d = f"{DOMAIN}_house_consumption_energy_avg_{hour_start:02d}_{hour_end:02d}_14d"

            # Resolve entity_ids for 3d, 7d, and 14d sensors
            entity_id_3d = await async_resolve_entity_id_from_unique_id(
                self, unique_id_3d
            )
            entity_id_7d = await async_resolve_entity_id_from_unique_id(
                self, unique_id_7d
            )
            entity_id_14d = await async_resolve_entity_id_from_unique_id(
                self, unique_id_14d
            )

            # Default values for sensors in case they are missing
            value_3d = 0.0
            value_7d = 0.0
            value_14d = 0.0

            # Fetch values for 3d, 7d, and 14d if available
            if entity_id_3d:
                entity_state_3d = self.hass.states.get(entity_id_3d)
                if entity_state_3d and entity_state_3d.state != "unknown":
                    try:
                        value_3d = convert_to_float(entity_state_3d.state)
                    except ValueError:
                        _LOGGER.warning(
                            f"Invalid state for entity {entity_id_3d}: {entity_state_3d.state}"
                        )

            if entity_id_7d:
                entity_state_7d = self.hass.states.get(entity_id_7d)
                if entity_state_7d and entity_state_7d.state != "unknown":
                    try:
                        value_7d = convert_to_float(entity_state_7d.state)
                    except ValueError:
                        _LOGGER.warning(
                            f"Invalid state for entity {entity_id_7d}: {entity_state_7d.state}"
                        )

            if entity_id_14d:
                entity_state_14d = self.hass.states.get(entity_id_14d)
                if entity_state_14d and entity_state_14d.state != "unknown":
                    try:
                        value_14d = convert_to_float(entity_state_14d.state)
                    except ValueError:
                        _LOGGER.warning(
                            f"Invalid state for entity {entity_id_14d}: {entity_state_14d.state}"
                        )

            # Calculate the weighted average house consumption for the hour
            weighted_value = round(
                (value_3d * HOUSE_CONSUMPTION_ENERGY_WEIGHT_3D)
                + (value_7d * HOUSE_CONSUMPTION_ENERGY_WEIGHT_7D)
                + (value_14d * HOUSE_CONSUMPTION_ENERGY_WEIGHT_14D),
                6,
            )

            # Only update "avg_house_consumption" in the existing dictionary entry
            if time_range in self._hourly_calculations:
                self._hourly_calculations[time_range]["avg_house_consumption"] = round(
                    weighted_value, 2
                )

        _LOGGER.debug(
            f"Hourly weighted calculations (avg_house_consumption): {self._hourly_calculations}"
        )

    async def async_calculate_solcast_forecast(self):
        """Calculate the hourly Solcast PV estimate and update self._hourly_calculations without resetting avg_house_consumption."""

        solcast_sensor = self.hass.states.get(
            self._hsem_solcast_pv_forecast_forecast_today
        )
        if not solcast_sensor:
            _LOGGER.warning("Solcast forecast sensor not found.")
            return

        detailed_forecast = solcast_sensor.attributes.get("detailedForecast", [])
        if not detailed_forecast:
            _LOGGER.warning("Detailed forecast data is missing or empty.")
            return

        for period in detailed_forecast:
            period_start = period.get("period_start")
            pv_estimate = period.get("pv_estimate", 0.0)
            time_range = f"{period_start.hour:02d}-{(period_start.hour + 1) % 24:02d}"

            # Only update "solcast_pv_estimate" in the existing dictionary entry
            if time_range in self._hourly_calculations:
                self._hourly_calculations[time_range]["solcast_pv_estimate"] = round(
                    pv_estimate, 2
                )

        _LOGGER.debug(
            f"Updated hourly calculations with Solcast PV estimates: {self._hourly_calculations}"
        )

    async def async_calculate_hourly_import_price(self):
        """Calculate the estimated import price for each hour of the day."""

        import_price_sensor = self.hass.states.get(
            self._hsem_energi_data_service_import
        )
        if not import_price_sensor:
            _LOGGER.warning("hsem_energi_data_service_import sensor not found.")
            return

        detailed_raw_today = import_price_sensor.attributes.get("raw_today", [])
        if not detailed_raw_today:
            _LOGGER.warning("Detailed raw data is missing or empty.")
            return

        for period in detailed_raw_today:
            period_start = period.get("hour")
            price = period.get("price", 0.0)
            time_range = f"{period_start.hour:02d}-{(period_start.hour + 1) % 24:02d}"

            # Only update "import_price" in the existing dictionary entry
            if time_range in self._hourly_calculations:
                self._hourly_calculations[time_range]["import_price"] = price

        _LOGGER.debug(
            f"Updated hourly calculations with import prices: {self._hourly_calculations}"
        )

    async def async_calculate_hourly_export_price(self):
        """Calculate the estimated import price for each hour of the day."""

        export_price_sensor = self.hass.states.get(
            self._hsem_energi_data_service_export
        )
        if not export_price_sensor:
            _LOGGER.warning("hsem_energi_data_service_import sensor not found.")
            return

        detailed_raw_today = export_price_sensor.attributes.get("raw_today", [])
        if not detailed_raw_today:
            _LOGGER.warning("Detailed raw data is missing or empty.")
            return

        for period in detailed_raw_today:
            period_start = period.get("hour")
            price = period.get("price", 0.0)
            time_range = f"{period_start.hour:02d}-{(period_start.hour + 1) % 24:02d}"

            # Only update "import_price" in the existing dictionary entry
            if time_range in self._hourly_calculations:
                self._hourly_calculations[time_range]["export_price"] = price

        _LOGGER.debug(
            f"Updated hourly calculations with import prices: {self._hourly_calculations}"
        )

    async def async_calculate_hourly_net_consumption(self):
        """Calculate the estimated net consumption for each hour of the day."""

        for hour in range(24):
            hour_start = hour
            hour_end = (hour + 1) % 24
            time_range = f"{hour_start:02d}-{hour_end:02d}"

            avg_house_consumption = self._hourly_calculations[time_range][
                "avg_house_consumption"
            ]

            solcast_pv_estimate = self._hourly_calculations[time_range][
                "solcast_pv_estimate"
            ]

            if avg_house_consumption is None or solcast_pv_estimate is None:
                estimated_net_consumption = 0.0
            else:
                estimated_net_consumption = round(
                    solcast_pv_estimate - avg_house_consumption, 2
                )

            # calculate the estimated net consumption
            if time_range in self._hourly_calculations:
                self._hourly_calculations[time_range]["estimated_net_consumption"] = (
                    round(estimated_net_consumption, 2)
                )

        _LOGGER.debug(
            f"Updated hourly calculations with Estimated Net Consumption: {self._hourly_calculations}"
        )

    async def async_optimization_strategy(self):
        """Calculate the optimization strategy for each hour of the day."""

        for hour, data in self._hourly_calculations.items():
            import_price = data["import_price"]
            export_price = data["export_price"]
            net_consumption = data["estimated_net_consumption"]
            start_hour = int(hour.split("-")[0])

            if data["recommendation"] is not None:
                continue

            # Fully Fed to Grid
            if export_price > import_price:
                data["recommendation"] = "Fully Fed to Grid"

            # Maximize Self Consumption
            if net_consumption > 0:
                data["recommendation"] = "Maximize Self Consumption"

            if 17 <= start_hour < 21:
                data["recommendation"] = "Time of Use: Discharge"

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

        if self._hsem_house_consumption_power:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_house_consumption_power}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_house_consumption_power],
                self._handle_update,
            )

        if self._hsem_solar_production_power:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_solar_production_power}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_solar_production_power],
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

        if self._hsem_solcast_pv_forecast_forecast_today:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_solcast_pv_forecast_forecast_today}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_solcast_pv_forecast_forecast_today],
                self._handle_update,
            )

        if self._hsem_energi_data_service_import:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_energi_data_service_import}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_energi_data_service_import],
                self._handle_update,
            )

        if self._hsem_energi_data_service_export:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_energi_data_service_export}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_energi_data_service_export],
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

        if self._hsem_ev_charger_power:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_ev_charger_power}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_ev_charger_power],
                self._handle_update,
            )

        if self._hsem_huawei_solar_batteries_maximum_charging_power:
            _LOGGER.info(
                f"Starting to track state changes for entity_id {self._hsem_huawei_solar_batteries_maximum_charging_power}"
            )
            async_track_state_change_event(
                self.hass,
                [self._hsem_huawei_solar_batteries_maximum_charging_power],
                self._handle_update,
            )

        # Schedule a periodic update every 5 minutes
        async_track_time_interval(self.hass, self._handle_update, timedelta(minutes=5))

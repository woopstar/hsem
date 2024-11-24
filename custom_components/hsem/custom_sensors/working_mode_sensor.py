"""
This module defines the WorkingModeSensor class, which is a custom sensor entity for Home Assistant.
The sensor monitors various attributes related to solar energy production, battery status, and energy consumption,
and calculates the optimal working mode for the system.

Classes:
    WorkingModeSensor(SensorEntity, HSEMEntity): Represents a custom sensor entity for monitoring and optimizing
    solar energy production and consumption.

Attributes:
    _attr_icon (str): The icon for the sensor entity.
    _attr_has_entity_name (bool): Indicates if the entity has a name.
    _config_entry (ConfigEntry): The configuration entry for the sensor.
    _state (str): The current state of the sensor.
    _hsem_huawei_solar_device_id_inverter_1 (str): The device ID for the first Huawei solar inverter.
    _hsem_huawei_solar_device_id_inverter_2 (str): The device ID for the second Huawei solar inverter.
    _hsem_huawei_solar_device_id_batteries (str): The device ID for the Huawei solar batteries.
    _hsem_huawei_solar_batteries_working_mode (str): The working mode for the Huawei solar batteries.
    _hsem_huawei_solar_batteries_state_of_capacity (str): The state of capacity for the Huawei solar batteries.
    _hsem_house_consumption_power (str): The power consumption of the house.
    _hsem_solar_production_power (str): The power production from solar panels.
    _hsem_ev_charger_status (str): The status of the EV charger.
    _hsem_ev_charger_power (str): The power consumption of the EV charger.
    _hsem_solcast_pv_forecast_forecast_today (str): The Solcast PV forecast for today.
    _hsem_battery_max_capacity (float): The maximum capacity of the battery.
    _hsem_energi_data_service_import (str): The energy data service import sensor.
    _hsem_energi_data_service_export (str): The energy data service export sensor.
    _hsem_huawei_solar_inverter_active_power_control (str): The active power control for the Huawei solar inverter.
    _hsem_huawei_solar_batteries_working_mode_state (str): The working mode state for the Huawei solar batteries.
    _hsem_huawei_solar_batteries_state_of_capacity_state (float): The state of capacity for the Huawei solar batteries.
    _hsem_house_power_includes_ev_charger_power (bool): Indicates if the house power includes EV charger power.
    _hsem_ev_charger_status_state (bool): The state of the EV charger status.
    _hsem_ev_charger_power_state (float): The state of the EV charger power.
    _hsem_house_consumption_power_state (float): The state of the house consumption power.
    _hsem_solar_production_power_state (float): The state of the solar production power.
    _hsem_huawei_solar_inverter_active_power_control_state (str): The active power control state for the Huawei solar inverter.
    _hsem_net_consumption (float): The net energy consumption.
    _hsem_net_consumption_with_ev (float): The net energy consumption including EV charger power.
    _hsem_huawei_solar_batteries_maximum_charging_power (float): The maximum charging power for the Huawei solar batteries.
    _hsem_huawei_solar_batteries_maximum_charging_power_state (float): The state of the maximum charging power for the Huawei solar batteries.
    _hsem_battery_conversion_loss (float): The battery conversion loss.
    _hsem_battery_remaining_charge (float): The remaining charge of the battery.
    _hsem_energi_data_service_import_state (float): The state of the energy data service import.
    _hsem_energi_data_service_export_state (float): The state of the energy data service export.
    _hsem_morning_energy_need (float): The morning energy need.
    _last_changed_mode (str): The last time the working mode was changed.
    _last_updated (str): The last time the sensor was updated.
    _hourly_calculations (dict): A dictionary containing hourly calculations for various attributes.
    _unique_id (str): The unique ID for the sensor entity.

Methods:
    __init__(self, config_entry): Initializes the WorkingModeSensor instance.
    set_hsem_huawei_solar_device_id_inverter_1(self, value): Sets the device ID for the first Huawei solar inverter.
    set_hsem_huawei_solar_device_id_inverter_2(self, value): Sets the device ID for the second Huawei solar inverter.
    set_hsem_huawei_solar_device_id_batteries(self, value): Sets the device ID for the Huawei solar batteries.
    set_hsem_huawei_solar_batteries_working_mode(self, value): Sets the working mode for the Huawei solar batteries.
    set_hsem_huawei_solar_batteries_state_of_capacity(self, value): Sets the state of capacity for the Huawei solar batteries.
    set_hsem_house_consumption_power(self, value): Sets the power consumption of the house.
    set_hsem_solar_production_power(self, value): Sets the power production from solar panels.
    set_hsem_ev_charger_status(self, value): Sets the status of the EV charger.
    set_hsem_ev_charger_power(self, value): Sets the power consumption of the EV charger.
    set_hsem_solcast_pv_forecast_forecast_today(self, value): Sets the Solcast PV forecast for today.
    set_hsem_battery_max_capacity(self, value): Sets the maximum capacity of the battery.
    set_hsem_energi_data_service_import(self, value): Sets the energy data service import sensor.
    set_hsem_energi_data_service_export(self, value): Sets the energy data service export sensor.
    set_hsem_huawei_solar_inverter_active_power_control(self, value): Sets the active power control for the Huawei solar inverter.
    set_hsem_huawei_solar_batteries_working_mode_state(self, value): Sets the working mode state for the Huawei solar batteries.
    set_hsem_battery_conversion_loss(self, value): Sets the battery conversion loss.
    set_hsem_house_power_includes_ev_charger_power(self, value): Sets if the house power includes EV charger power.
    set_hsem_huawei_solar_batteries_maximum_charging_power(self, value): Sets the maximum charging power for the Huawei solar batteries.
    set_hsem_morning_energy_need(self, value): Sets the morning energy need.
    _update_settings(self): Fetches updated settings from config_entry options.
    name(self): Returns the name of the sensor.
    unique_id(self): Returns the unique ID of the sensor.
    state(self): Returns the current state of the sensor.
    extra_state_attributes(self): Returns the state attributes of the sensor.
    _async_handle_update(self, event): Handles the sensor state update (for both manual and state change).
    async_set_inverter_power_control(self): Sets the inverter power control mode.
    async_set_working_mode(self): Sets the working mode for the system.
    async_calculate_hourly_data(self): Calculates the weighted hourly data for the sensor.
    async_calculate_solcast_forecast(self): Calculates the hourly Solcast PV estimate.
    async_calculate_hourly_import_price(self): Calculates the estimated import price for each hour of the day.
    async_calculate_hourly_export_price(self): Calculates the estimated export price for each hour of the day.
    async_calculate_hourly_net_consumption(self): Calculates the estimated net consumption for each hour of the day.
    async_optimization_strategy(self): Calculates the optimization strategy for each hour of the day.
    async_update(self): Manually triggers the sensor update.
    async_added_to_hass(self): Handles the sensor being added to Home Assistant.
"""

import logging
from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import State
from homeassistant.helpers.event import async_track_time_interval

from custom_components.hsem.const import (
    DEFAULT_HSEM_DEFAULT_TOU_MODES,
    DEFAULT_HSEM_EV_CHARGER_TOU_MODES,
    DEFAULT_HSEM_MONTHS_SUMMER,
    DEFAULT_HSEM_MONTHS_WINTER_SPRING,
    DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE,
    DEFAULT_HSEM_TOU_MODES_FORCE_DISCHARGE,
)
from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.huawei import (
    async_set_grid_export_power_pct,
    async_set_tou_periods,
)
from custom_components.hsem.utils.misc import (
    async_resolve_entity_id_from_unique_id,
    async_set_select_option,
    convert_to_float,
    generate_hash,
    get_config_value,
    ha_get_entity_state_and_convert,
)
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.sensornames import (
    get_energy_average_sensor_unique_id,
    get_working_mode_sensor_name,
    get_working_mode_sensor_unique_id,
)
from custom_components.hsem.utils.workingmodes import WorkingModes

_LOGGER = logging.getLogger(__name__)


class HSEMWorkingModeSensor(SensorEntity, HSEMEntity):
    # Define the attributes of the entity
    _attr_icon = "mdi:chart-timeline-variant"
    _attr_has_entity_name = True

    def __init__(self, config_entry):
        super().__init__(config_entry)

        # set config entry and state
        self._config_entry = config_entry
        self._state = None

        # Initialize all attributes to None or some default value
        self._read_only = False
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
        self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc = None
        self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state = None
        self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods = None
        self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_state = (
            None
        )
        self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_periods = (
            None
        )
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
        self._last_changed_mode = None
        self._last_updated = None
        self._hsem_house_consumption_energy_weight_1d = None
        self._hsem_house_consumption_energy_weight_3d = None
        self._hsem_house_consumption_energy_weight_7d = None
        self._hsem_house_consumption_energy_weight_14d = None
        self._hsem_batteries_enable_charge_hours_day = False
        self._hsem_batteries_enable_charge_hours_day_start = None
        self._hsem_batteries_enable_charge_hours_day_end = None
        self._hsem_batteries_enable_charge_hours_night = False
        self._hsem_batteries_enable_charge_hours_night_start = None
        self._hsem_batteries_enable_charge_hours_night_end = None
        self._hourly_calculations = {
            f"{hour:02d}-{(hour + 1) % 24:02d}": {
                "avg_house_consumption": 0.0,
                "solcast_pv_estimate": 0.0,
                "estimated_net_consumption": 0.0,
                "batteries_charged": 0.0,
                "import_price": 0.0,
                "export_price": 0.0,
                "recommendation": None,
            }
            for hour in range(24)
        }
        self._energy_needs = {
            "0am_6am": 0.0,
            "6am_10am": 0.0,
            "10am_5pm": 0.0,
            "5pm_9pm": 0.0,
            "9pm_midnight": 0.0,
        }
        self._unique_id = get_working_mode_sensor_unique_id()
        self._update_settings()

    def _update_settings(self):
        """Fetch updated settings from config_entry options."""
        self._read_only = get_config_value(self._config_entry, "hsem_read_only", False)

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
            self._config_entry, "hsem_huawei_solar_batteries_working_mode"
        )
        self._hsem_huawei_solar_batteries_state_of_capacity = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_batteries_state_of_capacity",
        )
        self._hsem_house_consumption_power = get_config_value(
            self._config_entry,
            "hsem_house_consumption_power",
        )
        self._hsem_solar_production_power = get_config_value(
            self._config_entry,
            "hsem_solar_production_power",
        )
        self._hsem_ev_charger_status = get_config_value(
            self._config_entry, "hsem_ev_charger_status"
        )
        self._hsem_solcast_pv_forecast_forecast_today = get_config_value(
            self._config_entry,
            "hsem_solcast_pv_forecast_forecast_today",
        )
        self._hsem_battery_max_capacity = get_config_value(
            self._config_entry,
            "hsem_battery_max_capacity",
        )
        self._hsem_energi_data_service_import = get_config_value(
            self._config_entry,
            "hsem_energi_data_service_import",
        )
        self._hsem_energi_data_service_export = get_config_value(
            self._config_entry,
            "hsem_energi_data_service_export",
        )
        self._hsem_huawei_solar_inverter_active_power_control = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_inverter_active_power_control",
        )
        self._hsem_house_power_includes_ev_charger_power = get_config_value(
            self._config_entry,
            "hsem_house_power_includes_ev_charger_power",
        )
        self._hsem_ev_charger_power = get_config_value(
            self._config_entry,
            "hsem_ev_charger_power",
        )
        self._hsem_battery_conversion_loss = get_config_value(
            self._config_entry,
            "hsem_battery_conversion_loss",
        )
        self._hsem_huawei_solar_batteries_maximum_charging_power = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_batteries_maximum_charging_power",
        )
        self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_batteries_grid_charge_cutoff_soc",
        )
        self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods = (
            get_config_value(
                self._config_entry,
                "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods",
            )
        )
        self._hsem_house_consumption_energy_weight_1d = get_config_value(
            self._config_entry, "hsem_house_consumption_energy_weight_1d"
        )
        self._hsem_house_consumption_energy_weight_3d = get_config_value(
            self._config_entry, "hsem_house_consumption_energy_weight_3d"
        )
        self._hsem_house_consumption_energy_weight_7d = get_config_value(
            self._config_entry, "hsem_house_consumption_energy_weight_7d"
        )
        self._hsem_house_consumption_energy_weight_14d = get_config_value(
            self._config_entry, "hsem_house_consumption_energy_weight_14d"
        )
        self._hsem_batteries_enable_charge_hours_day = get_config_value(
            self._config_entry, "hsem_batteries_enable_charge_hours_day"
        )
        self._hsem_batteries_enable_charge_hours_day_start = get_config_value(
            self._config_entry, "hsem_batteries_enable_charge_hours_day_start"
        )
        self._hsem_batteries_enable_charge_hours_day_end = get_config_value(
            self._config_entry, "hsem_batteries_enable_charge_hours_day_end"
        )
        self._hsem_batteries_enable_charge_hours_night = get_config_value(
            self._config_entry, "hsem_batteries_enable_charge_hours_night"
        )
        self._hsem_batteries_enable_charge_hours_night_start = get_config_value(
            self._config_entry, "hsem_batteries_enable_charge_hours_night_start"
        )
        self._hsem_batteries_enable_charge_hours_night_end = get_config_value(
            self._config_entry, "hsem_batteries_enable_charge_hours_night_end"
        )

        if self._hsem_huawei_solar_device_id_inverter_2 is not None:
            if len(self._hsem_huawei_solar_device_id_inverter_2) == 0:
                self._hsem_huawei_solar_device_id_inverter_2 = None

        # Log updated settings
        _LOGGER.debug(
            f"Updated settings: input_sensor={self._hsem_huawei_solar_batteries_working_mode}"
        )

    @property
    def name(self):
        return get_working_mode_sensor_name()

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
            "read_only": self._read_only,
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
            "huawei_solar_batteries_grid_charge_cutoff_soc_entity": self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc,
            "huawei_solar_batteries_grid_charge_cutoff_soc_state": self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state,
            "huawei_solar_batteries_maximum_charging_power_entity": self._hsem_huawei_solar_batteries_maximum_charging_power,
            "huawei_solar_batteries_maximum_charging_power_state": self._hsem_huawei_solar_batteries_maximum_charging_power_state,
            "huawei_solar_batteries_enable_charge_hours_day": self._hsem_batteries_enable_charge_hours_day,
            "huawei_solar_batteries_enable_charge_hours_day_start": self._hsem_batteries_enable_charge_hours_day_start,
            "huawei_solar_batteries_enable_charge_hours_day_end": self._hsem_batteries_enable_charge_hours_day_end,
            "huawei_solar_batteries_enable_charge_hours_night": self._hsem_batteries_enable_charge_hours_night,
            "huawei_solar_batteries_enable_charge_hours_night_start": self._hsem_batteries_enable_charge_hours_night_start,
            "huawei_solar_batteries_enable_charge_hours_night_end": self._hsem_batteries_enable_charge_hours_night_end,
            "huawei_solar_inverter_active_power_control_state_entity": self._hsem_huawei_solar_inverter_active_power_control,
            "huawei_solar_inverter_active_power_control_state_state": self._hsem_huawei_solar_inverter_active_power_control_state,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_entity": self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_state": self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_state,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_periods": self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_periods,
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
            "battery_conversion_loss": self._hsem_battery_conversion_loss,
            "ev_charger_status_entity": self._hsem_ev_charger_status,
            "ev_charger_status_state": self._hsem_ev_charger_status_state,
            "ev_charger_power_entity": self._hsem_ev_charger_power,
            "ev_charger_power_state": self._hsem_ev_charger_power_state,
            "house_power_includes_ev_charger_power": self._hsem_house_power_includes_ev_charger_power,
            "solcast_pv_forecast_forecast_today_entity": self._hsem_solcast_pv_forecast_forecast_today,
            "house_consumption_energy_weight_1d": self._hsem_house_consumption_energy_weight_1d,
            "house_consumption_energy_weight_3d": self._hsem_house_consumption_energy_weight_3d,
            "house_consumption_energy_weight_7d": self._hsem_house_consumption_energy_weight_7d,
            "house_consumption_energy_weight_14d": self._hsem_house_consumption_energy_weight_14d,
            "energy_needs": self._energy_needs,
            "hourly_calculations": self._hourly_calculations,
        }

    async def _async_handle_update(self, event):
        """Handle the sensor state update (for both manual and state change)."""

        # Get the current time
        now = datetime.now()

        # Ensure config flow settings are reloaded if it changed.
        self._update_settings()

        # Fetch the latest entity states
        await self._async_fetch_entity_states()

        # Calculate the net consumption
        await self._async_calculate_net_consumption()

        # Calculate remaining battery capacity and max allowed charge from grid if all necessary values are available
        if (
            isinstance(self._hsem_battery_max_capacity, (int, float))
            and isinstance(
                self._hsem_huawei_solar_batteries_state_of_capacity_state, (int, float)
            )
            and isinstance(
                self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state,
                (int, float),
            )
        ):
            # Calculate the remaining charge needed to reach full capacity (kWh)
            self._hsem_battery_remaining_charge = round(
                (100 - self._hsem_huawei_solar_batteries_state_of_capacity_state)
                / 100
                * self._hsem_battery_max_capacity,
                2,
            )

            # Calculate the maximum charge allowed from the grid based on cutoff SOC (kWh)
            max_allowed_grid_charge = (
                self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state
                * self._hsem_battery_max_capacity
                / 100
            )

            # Adjust remaining charge if it exceeds the max grid-allowed charge
            if self._hsem_battery_remaining_charge > max_allowed_grid_charge:
                self._hsem_battery_remaining_charge = max_allowed_grid_charge
        else:
            self._hsem_battery_remaining_charge = None

        # reset the recommendations
        await self._async_reset_recommendations()

        # calculate the hourly data from power sensors
        await self._async_calculate_hourly_data()

        # calculate the solcast forecast for today
        await self._async_calculate_solcast_forecast()

        # calculate the hourly net consumption between house consumption and solar production
        await self._async_calculate_hourly_net_consumption()

        # calculate the hourly import price
        await self._async_calculate_hourly_import_price()

        # calculate the hourly export price
        await self._async_calculate_hourly_export_price()

        # calculate the optimization strategy
        await self._async_optimization_strategy()

        # calculate the energy needs
        await self._async_calculate_energy_needs()

        # Force charge the batteries when needed
        await self._async_force_charge_batteries()

        # Set the inverter power control mode
        if (
            self._hsem_energi_data_service_export_state is not None
            and self._read_only is not True
        ):
            await self._async_set_inverter_power_control()

        # calculate the last time working mode was changed
        if self._last_changed_mode is not None:
            last_changed_mode_seconds = (
                now - datetime.fromisoformat(self._last_changed_mode)
            ).total_seconds()
        else:
            last_changed_mode_seconds = 0

        # Set the working mode
        if last_changed_mode_seconds > 100 or self._last_changed_mode is None:
            await self._async_set_working_mode()
            self._last_changed_mode = datetime.now().isoformat()

        # Update last update time
        self._last_updated = datetime.now().isoformat()

        # Trigger an update in Home Assistant
        self.async_write_ha_state()

    async def _async_force_charge_batteries(self):
        # Get the current time
        now = datetime.now()

        # Charge the battery when it's winter/spring and prices are high
        if now.month in DEFAULT_HSEM_MONTHS_WINTER_SPRING:
            if (
                self._hsem_batteries_enable_charge_hours_day_start
                and self._hsem_batteries_enable_charge_hours_day_end
                and self._hsem_batteries_enable_charge_hours_night_start
                and self._hsem_batteries_enable_charge_hours_night_end
                and isinstance(
                    self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state,
                    (int, float),
                )
                and isinstance(
                    self._hsem_huawei_solar_batteries_state_of_capacity_state,
                    (int, float),
                )
            ):
                # Charge the battery when the grid charge cutoff SOC is higher than the state of capacity with at least 5%
                if (
                    self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state
                    - self._hsem_huawei_solar_batteries_state_of_capacity_state
                ) > 5:
                    day_hour_start = datetime.strptime(
                        self._hsem_batteries_enable_charge_hours_day_start, "%H:%M:%S"
                    ).hour
                    day_hour_end = datetime.strptime(
                        self._hsem_batteries_enable_charge_hours_day_end, "%H:%M:%S"
                    ).hour

                    night_hour_start = datetime.strptime(
                        self._hsem_batteries_enable_charge_hours_night_start, "%H:%M:%S"
                    ).hour
                    night_hour_end = datetime.strptime(
                        self._hsem_batteries_enable_charge_hours_night_end, "%H:%M:%S"
                    ).hour

                    # find best time to charge the battery at night
                    if (
                        now.hour >= night_hour_start
                        and now.hour < night_hour_end
                        and self._hsem_batteries_enable_charge_hours_night
                    ):
                        await self._async_find_best_time_to_charge(
                            night_hour_start, night_hour_end
                        )

                    # find best time to charge the battery at day
                    if (
                        now.hour >= day_hour_start
                        and now.hour < day_hour_end
                        and self._hsem_batteries_enable_charge_hours_day
                    ):
                        await self._async_find_best_time_to_charge(
                            day_hour_start, day_hour_end
                        )

    async def _async_fetch_entity_states(self):
        # Fetch the current value from the EV charger status sensor
        if self._hsem_ev_charger_status:
            self._hsem_ev_charger_status_state = ha_get_entity_state_and_convert(
                self, self._hsem_ev_charger_status, "boolean"
            )

        # Fetch the current value from the battery maximum charging power sensor
        if self._hsem_ev_charger_power:
            self._hsem_ev_charger_power_state = ha_get_entity_state_and_convert(
                self, self._hsem_ev_charger_power, "float"
            )

        # Fetch the current value from the house consumption power sensor
        if self._hsem_house_consumption_power:
            self._hsem_house_consumption_power_state = ha_get_entity_state_and_convert(
                self, self._hsem_house_consumption_power, "float"
            )

        # Fetch the current value from the solar production power sensor
        if self._hsem_solar_production_power:
            self._hsem_solar_production_power_state = ha_get_entity_state_and_convert(
                self, self._hsem_solar_production_power, "float"
            )

        # fetch the current value from the working mode sensor
        if self._hsem_huawei_solar_batteries_working_mode:
            self._hsem_huawei_solar_batteries_working_mode_state = (
                ha_get_entity_state_and_convert(
                    self, self._hsem_huawei_solar_batteries_working_mode, "string"
                )
            )

        # Fetch the current value from the state of capacity sensor
        if self._hsem_huawei_solar_batteries_state_of_capacity:
            self._hsem_huawei_solar_batteries_state_of_capacity_state = (
                ha_get_entity_state_and_convert(
                    self,
                    self._hsem_huawei_solar_batteries_state_of_capacity,
                    "float",
                    0,
                )
            )

        # Fetch the current value from the energi data service import sensor
        if self._hsem_energi_data_service_import:
            self._hsem_energi_data_service_import_state = (
                ha_get_entity_state_and_convert(
                    self, self._hsem_energi_data_service_import, "float", 3
                )
            )

        # Fetch the current value from the energi data service export sensor
        if self._hsem_energi_data_service_export:
            self._hsem_energi_data_service_export_state = (
                ha_get_entity_state_and_convert(
                    self, self._hsem_energi_data_service_export, "float", 3
                )
            )

        # Fetch the current value from the energi data service export sensor
        if self._hsem_huawei_solar_inverter_active_power_control:
            self._hsem_huawei_solar_inverter_active_power_control_state = (
                ha_get_entity_state_and_convert(
                    self,
                    self._hsem_huawei_solar_inverter_active_power_control,
                    "string",
                )
            )

        # Fetch the current value from the battery maximum charging power sensor
        if self._hsem_huawei_solar_batteries_maximum_charging_power:
            self._hsem_huawei_solar_batteries_maximum_charging_power_state = (
                ha_get_entity_state_and_convert(
                    self,
                    self._hsem_huawei_solar_batteries_maximum_charging_power,
                    "float",
                    0,
                )
            )

        # Fetch the current value from the battery grid charge cutoff SOC sensor
        if self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc:
            self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state = (
                ha_get_entity_state_and_convert(
                    self,
                    self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc,
                    "float",
                    0,
                )
            )

        # Fetch the current value from the battery TOU charging and discharging periods sensor
        if self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods:
            entity_data = ha_get_entity_state_and_convert(
                self,
                self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods,
                None,
            )

            # Reset state and periods attributes
            self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_state = (
                None
            )
            self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_periods = (
                None
            )

            # Ensure entity_data is valid and a State object
            if isinstance(entity_data, State):
                # Set the state directly
                self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_state = (
                    entity_data.state
                )

                # Gather period values from attributes using a list comprehension
                self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_periods = [
                    entity_data.attributes[f"Period {i}"]
                    for i in range(1, 11)
                    if f"Period {i}" in entity_data.attributes
                ]

    async def _async_calculate_net_consumption(self):
        # Calculate the net consumption without the EV charger power
        if isinstance(
            self._hsem_solar_production_power_state, (int, float)
        ) and isinstance(self._hsem_house_consumption_power_state, (int, float)):

            # Treat EV charger power state as 0.0 if it's None
            ev_charger_power_state = (
                self._hsem_ev_charger_power_state
                if isinstance(self._hsem_ev_charger_power_state, (int, float))
                else 0.0
            )

            if self._hsem_house_power_includes_ev_charger_power is not None:
                self._hsem_net_consumption_with_ev = (
                    self._hsem_house_consumption_power_state
                    - self._hsem_solar_production_power_state
                )
                self._hsem_net_consumption = (
                    self._hsem_house_consumption_power_state
                    - (self._hsem_solar_production_power_state - ev_charger_power_state)
                )
            else:
                self._hsem_net_consumption_with_ev = (
                    self._hsem_house_consumption_power_state
                    - (self._hsem_solar_production_power_state + ev_charger_power_state)
                )
                self._hsem_net_consumption = (
                    self._hsem_house_consumption_power_state
                    - self._hsem_solar_production_power_state
                )

            self._hsem_net_consumption = round(self._hsem_net_consumption, 2)
        else:
            self._hsem_net_consumption = 0.0

    async def _async_set_inverter_power_control(self):
        # Determine the grid export power percentage based on the state
        if not isinstance(self._hsem_energi_data_service_export_state, (int, float)):
            return

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
                if inverter_id is not None:
                    await async_set_grid_export_power_pct(
                        self, inverter_id, export_power_percentage
                    )

    async def _async_set_working_mode(self):

        # Determine the current month and hour
        now = datetime.now()
        current_month = now.month
        current_hour_start = now.hour
        current_hour_end = (current_hour_start + 1) % 24
        current_time_range = f"{current_hour_start:02d}-{current_hour_end:02d}"
        tou_modes = None
        state = None

        # Determine the appropriate TOU modes and working mode state. In priority order:
        if (
            isinstance(self._hsem_energi_data_service_import_state, (int, float))
            and self._hsem_energi_data_service_import_state < 0
        ):
            # Negative import price. Force charge battery
            tou_modes = DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.ForceExport.value
            _LOGGER.warning(
                f"Import price is negative. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}"
            )
        elif (
            self._hourly_calculations.get(current_time_range, {}).get("recommendation")
            == Recommendations.ForceBatteriesCharge.value
        ):
            tou_modes = DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.ForceBatteriesCharge.value
            _LOGGER.warning(
                f"# Recommendation for {current_time_range} is to force charge the battery. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}"
            )
        elif self._hsem_ev_charger_status_state:
            # EV Charger is active. Disable battery discharge
            tou_modes = DEFAULT_HSEM_EV_CHARGER_TOU_MODES
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.EVSmartCharging.value
            _LOGGER.warning(
                f"EV Charger is active. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}"
            )
        elif (
            self._hourly_calculations.get(current_time_range, {}).get("recommendation")
            == Recommendations.ForceBatteriesDischarge.value
        ):
            tou_modes = DEFAULT_HSEM_TOU_MODES_FORCE_DISCHARGE
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.ForceBatteriesDischarge.value
            _LOGGER.warning(
                f"# Recommendation for {current_time_range} is to force discharge the battery. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}"
            )
        elif self._hsem_net_consumption < 0:
            # Positive net consumption. Charge battery from Solar
            working_mode = WorkingModes.MaximizeSelfConsumption.value
            state = Recommendations.MaximizeSelfConsumption.value
            _LOGGER.warning(
                f"Positive net consumption. Working Mode: {working_mode}, Solar Production: {self._hsem_solar_production_power_state}, House Consumption: {self._hsem_house_consumption_power_state}, Net Consumption: {self._hsem_net_consumption}"
            )
        elif (
            self._hourly_calculations.get(current_time_range, {}).get("recommendation")
            == Recommendations.MaximizeSelfConsumption.value
        ):
            working_mode = WorkingModes.MaximizeSelfConsumption.value
            state = Recommendations.MaximizeSelfConsumption.value
            _LOGGER.warning(
                f"# Recommendation for {current_time_range} is to set working mode to Maximize Self Consumption"
            )
        elif (
            self._hourly_calculations.get(current_time_range, {}).get("recommendation")
            == Recommendations.TimeOfUse.value
        ):
            # Winter/Spring settings
            if current_month in DEFAULT_HSEM_MONTHS_WINTER_SPRING:
                tou_modes = DEFAULT_HSEM_DEFAULT_TOU_MODES
                working_mode = WorkingModes.TimeOfUse.value
                state = Recommendations.TimeOfUse.value
                _LOGGER.warning(
                    f"Default winter/spring settings. TOU Periods: {tou_modes} and Working Mode: {working_mode}"
                )

            # Summer settings
            if current_month in DEFAULT_HSEM_MONTHS_SUMMER:
                working_mode = WorkingModes.MaximizeSelfConsumption.value
                state = Recommendations.MaximizeSelfConsumption.value
                _LOGGER.warning(
                    f"Default summer settings. Working Mode: {working_mode}"
                )

        # Apply TOU periods if working mode is TOU
        if working_mode == WorkingModes.TimeOfUse.value:
            new_tou_modes_hash = generate_hash(str(tou_modes))
            current_tou_modes_hash = generate_hash(
                str(
                    self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_periods
                )
            )

            if new_tou_modes_hash != current_tou_modes_hash:
                _LOGGER.warning(
                    f"New TOU Modes Hash: {new_tou_modes_hash}, Current TOU Modes Hash: {current_tou_modes_hash}"
                )

                if self._read_only is not True:
                    await async_set_tou_periods(
                        self, self._hsem_huawei_solar_device_id_batteries, tou_modes
                    )

        # Only apply working mode if it has changed
        if self._hsem_huawei_solar_batteries_working_mode_state != working_mode:
            if self._read_only is not True:
                await async_set_select_option(
                    self, self._hsem_huawei_solar_batteries_working_mode, working_mode
                )

        self._state = state

    async def _async_reset_recommendations(self):
        """Reset the recommendations for each hour of the day."""
        self._hourly_calculations = {
            f"{hour:02d}-{(hour + 1) % 24:02d}": {
                "avg_house_consumption": 0.0,
                "solcast_pv_estimate": 0.0,
                "estimated_net_consumption": 0.0,
                "batteries_charged": 0.0,
                "import_price": 0.0,
                "export_price": 0.0,
                "recommendation": None,
            }
            for hour in range(24)
        }

    async def _async_calculate_hourly_data(self):
        """Calculate the weighted hourly data for the sensor using both 3-day and 7-day HouseConsumptionEnergyAverageSensors."""

        if self._hsem_house_consumption_energy_weight_1d is None:
            return

        if self._hsem_house_consumption_energy_weight_3d is None:
            return

        if self._hsem_house_consumption_energy_weight_7d is None:
            return

        if self._hsem_house_consumption_energy_weight_14d is None:
            return

        for hour in range(24):
            hour_start = hour
            hour_end = (hour + 1) % 24
            time_range = f"{hour_start:02d}-{hour_end:02d}"

            # Construct unique_ids for the 3d, 7d, and 14d sensors
            unique_id_1d = get_energy_average_sensor_unique_id(hour_start, hour_end, 1)
            unique_id_3d = get_energy_average_sensor_unique_id(hour_start, hour_end, 3)
            unique_id_7d = get_energy_average_sensor_unique_id(hour_start, hour_end, 7)
            unique_id_14d = get_energy_average_sensor_unique_id(
                hour_start, hour_end, 14
            )

            # Resolve entity_ids for 3d, 7d, and 14d sensors
            entity_id_1d = await async_resolve_entity_id_from_unique_id(
                self, unique_id_1d
            )
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
            value_1d = 0.0
            value_3d = 0.0
            value_7d = 0.0
            value_14d = 0.0

            # Fetch values for 1d, 3d, 7d, and 14d if available
            if entity_id_1d:
                entity_state_1d = self.hass.states.get(entity_id_1d)
                if entity_state_1d and entity_state_1d.state != "unknown":
                    try:
                        value_1d = convert_to_float(entity_state_1d.state)
                    except ValueError:
                        _LOGGER.warning(
                            f"Invalid state for entity {entity_id_1d}: {entity_state_1d.state}"
                        )

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
            weighted_value = (
                value_1d * (self._hsem_house_consumption_energy_weight_1d / 100)
                + value_3d * (self._hsem_house_consumption_energy_weight_3d / 100)
                + value_7d * (self._hsem_house_consumption_energy_weight_7d / 100)
                + value_14d * (self._hsem_house_consumption_energy_weight_14d / 100)
            )

            # Only update "avg_house_consumption" in the existing dictionary entry
            if time_range in self._hourly_calculations:
                self._hourly_calculations[time_range]["avg_house_consumption"] = round(
                    weighted_value, 2
                )

        _LOGGER.debug(
            f"Hourly weighted calculations (avg_house_consumption): {self._hourly_calculations}"
        )

    async def _async_calculate_solcast_forecast(self):
        """Calculate the hourly Solcast PV estimate and update self._hourly_calculations without resetting avg_house_consumption."""
        if self._hsem_solcast_pv_forecast_forecast_today is None:
            return

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

    async def _async_calculate_hourly_import_price(self):
        """Calculate the estimated import price for each hour of the day."""
        if self._hsem_energi_data_service_import is None:
            return

        import_price_sensor = self.hass.states.get(
            self._hsem_energi_data_service_import
        )

        if not import_price_sensor:
            _LOGGER.warning("hsem_energi_data_service_import sensor not found.")
            return

        detailed_raw_today = import_price_sensor.attributes.get("raw_today", [])
        if not detailed_raw_today:
            _LOGGER.warning("Detailed raw data is missing or empty for import prices.")
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

    async def _async_calculate_hourly_export_price(self):
        """Calculate the estimated import price for each hour of the day."""
        if self._hsem_energi_data_service_export is None:
            return

        export_price_sensor = self.hass.states.get(
            self._hsem_energi_data_service_export
        )
        if not export_price_sensor:
            _LOGGER.warning("hsem_energi_data_service_export sensor not found.")
            return

        detailed_raw_today = export_price_sensor.attributes.get("raw_today", [])
        if not detailed_raw_today:
            _LOGGER.warning("Detailed raw data is missing or empty for export prices.")
            return

        for period in detailed_raw_today:
            period_start = period.get("hour")
            price = period.get("price", 0.0)
            time_range = f"{period_start.hour:02d}-{(period_start.hour + 1) % 24:02d}"

            # Only update "import_price" in the existing dictionary entry
            if time_range in self._hourly_calculations:
                self._hourly_calculations[time_range]["export_price"] = price

        _LOGGER.debug(
            f"Updated hourly calculations with export prices: {self._hourly_calculations}"
        )

    async def _async_calculate_hourly_net_consumption(self):
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
                    avg_house_consumption - solcast_pv_estimate, 2
                )

            # calculate the estimated net consumption
            if time_range in self._hourly_calculations:
                self._hourly_calculations[time_range]["estimated_net_consumption"] = (
                    round(estimated_net_consumption, 2)
                )

        _LOGGER.debug(
            f"Updated hourly calculations with Estimated Net Consumption: {self._hourly_calculations}"
        )

    async def _async_find_best_time_to_charge(self, start_hour=14, stop_hour=17):
        _LOGGER.debug(
            f"Calculating best time to charge battery between {start_hour} and {stop_hour}"
        )

        # Get the current time
        now = datetime.now()

        # Check if the necessary variables have valid numerical values
        if (
            self._hsem_battery_max_capacity is None
            or self._hsem_huawei_solar_batteries_maximum_charging_power_state is None
            or self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state is None
            or self._hsem_battery_remaining_charge is None
            or self._hsem_battery_conversion_loss is None
            or self._hsem_huawei_solar_batteries_state_of_capacity_state is None
        ):
            _LOGGER.debug(
                f"Missing necessary variables for calculating best time to charge battery: {self._hsem_battery_remaining_charge}, {self._hsem_battery_max_capacity}, {self._hsem_huawei_solar_batteries_maximum_charging_power_state}, {self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state}, {self._hsem_huawei_solar_batteries_state_of_capacity_state}"
            )
            return  # Wait for the next call until all values are available

        # Find the hours within the specified range when it is cheapest to charge
        hours_to_charge = []
        for hour_start in range(start_hour, stop_hour):

            # Skip hours that have already passed
            if hour_start < now.hour:
                continue

            hour_end = (hour_start + 1) % 24
            time_range = f"{hour_start:02d}-{hour_end:02d}"

            if time_range in self._hourly_calculations:
                hours_to_charge.append(
                    (time_range, self._hourly_calculations[time_range]["import_price"])
                )

        # Sort hours by lowest import_price
        hours_to_charge.sort(key=lambda x: x[1])

        # Calculate charging time and mark the hours for charging
        charged_energy = 0.0
        for time_range, price in hours_to_charge:
            if charged_energy >= self._hsem_battery_remaining_charge:
                _LOGGER.debug(
                    f"Charged energy exceeds remaining charge. Stopping charging. Charged Energy: {charged_energy} kWh. Remaining Charge: {self._hsem_battery_remaining_charge} kWh."
                )
                break

            ### Calculate how much energy we can charge in this hour (in kWh)

            # Calculate the conversion loss factor from AC to DC
            conversion_loss_factor = 1 - (self._hsem_battery_conversion_loss / 100)

            # Calculate the maximum possible charge in kWh for this hour, limited by charging power and conversion loss
            if isinstance(
                self._hsem_huawei_solar_batteries_maximum_charging_power_state,
                (int, float),
            ):
                max_charge_per_hour = (
                    self._hsem_huawei_solar_batteries_maximum_charging_power_state
                    / 1000
                ) * conversion_loss_factor
            else:
                max_charge_per_hour = 0.0

            # Calculate the remaining charge needed to fill the battery, adjusted for conversion loss
            remaining_charge_needed = (
                self._hsem_battery_remaining_charge - charged_energy
            )

            # Determine the energy to charge by taking the minimum of the maximum charge allowed and the remaining needed charge
            energy_to_charge = round(
                min(max_charge_per_hour, remaining_charge_needed), 2
            )

            # Mark this hour for charging and update the charged energy
            self._hourly_calculations[time_range][
                "recommendation"
            ] = Recommendations.ForceBatteriesCharge.value
            self._hourly_calculations[time_range][
                "batteries_charged"
            ] = energy_to_charge

            charged_energy += energy_to_charge

            _LOGGER.debug(
                f"Marked hour {time_range} for charging. Energy Charged: {energy_to_charge} kWh. Total Charged Energy: {charged_energy} kWh. Total Charge needed: {self._hsem_battery_remaining_charge} kWh."
            )

        _LOGGER.debug(
            f"Updated hourly calculations with when to charge battery: {self._hourly_calculations}"
        )

    async def _async_optimization_strategy(self):
        """Calculate the optimization strategy for each hour of the day."""

        now = datetime.now()
        current_month = now.month

        for hour, data in self._hourly_calculations.items():
            import_price = data["import_price"]
            export_price = data["export_price"]
            net_consumption = data["estimated_net_consumption"]
            start_hour = int(hour.split("-")[0])

            if data["recommendation"] is not None:
                continue

            # Fully Fed to Grid
            if export_price > import_price:
                data["recommendation"] = WorkingModes.FullyFedToGrid.value

            # Maximize Self Consumption
            elif net_consumption < 0:
                data["recommendation"] = WorkingModes.MaximizeSelfConsumption.value

            # Between 17 and 21 we always want to maximize self consumption
            elif 17 <= start_hour < 21:
                data["recommendation"] = WorkingModes.MaximizeSelfConsumption.value

            else:
                if current_month in DEFAULT_HSEM_MONTHS_WINTER_SPRING:
                    data["recommendation"] = WorkingModes.TimeOfUse.value

                if current_month in DEFAULT_HSEM_MONTHS_SUMMER:
                    data["recommendation"] = WorkingModes.MaximizeSelfConsumption.value

    async def _async_calculate_energy_needs(self):
        """Calculate the energy needs for the day."""

        # Define time ranges and labels
        time_ranges = {
            "0am_6am": ["00-01", "01-02", "02-03", "03-04", "04-05", "05-06"],
            "6am_10am": ["06-07", "07-08", "08-09", "09-10"],
            "10am_5pm": ["10-11", "11-12", "12-13", "13-14", "14-15", "15-16", "16-17"],
            "5pm_9pm": ["17-18", "18-19", "19-20", "20-21"],
            "9pm_midnight": ["21-22", "22-23", "23-00"],
        }

        # Calculate energy needs for each time range
        self._energy_needs = {
            label: round(
                sum(
                    self._hourly_calculations[hour]["estimated_net_consumption"]
                    for hour in hours
                ),
                2,
            )
            for label, hours in time_ranges.items()
        }

    async def async_update(self):
        """Manually trigger the sensor update."""
        await self._async_handle_update(event=None)

    async def async_added_to_hass(self):
        """Handle the sensor being added to Home Assistant."""
        await super().async_added_to_hass()

        # Schedule a periodic update every minute
        async_track_time_interval(
            self.hass, self._async_handle_update, timedelta(minutes=1)
        )

        # Initial update
        await self._async_handle_update(None)

    async def async_will_remove_from_hass(self):
        """Entity being removed from hass."""
        await super().async_will_remove_from_hass()

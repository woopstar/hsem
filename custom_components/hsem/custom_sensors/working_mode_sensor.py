"""
This module defines the WorkingModeSensor class, which is a custom sensor entity for Home Assistant.
The sensor monitors various attributes related to solar energy production, battery status, and energy consumption,
and calculates the optimal working mode for the system.

Classes:
    WorkingModeSensor(SensorEntity, HSEMEntity): Represents a custom sensor entity for monitoring and optimizing
    solar energy production and consumption.
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
    convert_to_int,
    generate_hash,
    get_config_value,
    ha_get_entity_state_and_convert,
)
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.sensornames import (
    get_energy_average_sensor_unique_id,
    get_working_mode_sensor_entity_id,
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
        self._available = False
        self._update_interval = 1
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
        self._hsem_batteries_conversion_loss = 0.0
        self._hsem_batteries_remaining_charge = 0.0
        self._hsem_batteries_usable_capacity = 0.0
        self._hsem_batteries_current_capacity = 0.0
        self._hsem_energi_data_service_import_state = 0.0
        self._hsem_energi_data_service_export_state = 0.0
        self._last_changed_mode = None
        self._last_updated = None
        self._next_update = None
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
        self._hsem_batteries_rated_capacity_min_state = None
        self._hsem_batteries_rated_capacity_max = None
        self._hsem_batteries_rated_capacity_max_state = None
        self._hourly_calculations = {
            f"{hour:02d}-{(hour + 1) % 24:02d}": {
                "avg_house_consumption": 0.0,
                "solcast_pv_estimate": 0.0,
                "estimated_net_consumption": 0.0,
                "estimated_cost": 0.0,
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
        self._hsem_is_night_price_lower_than_morning = False
        self._hsem_is_night_price_lower_than_afternoon = False
        self._hsem_is_night_price_lower_than_evening = False
        self._hsem_is_night_price_lower_than_late_evening = False
        self._hsem_is_afternoon_price_lower_than_evening = False
        self._hsem_is_afternoon_price_lower_than_late_evening = False
        self._hsem_ac_charge_cutoff_percentage = 0.0
        self._missing_input_entities = False
        self._attr_unique_id = get_working_mode_sensor_unique_id()
        self.entity_id = get_working_mode_sensor_entity_id()
        self._update_settings()

    def _update_settings(self):
        """Fetch updated settings from config_entry options."""
        self._read_only = get_config_value(self._config_entry, "hsem_read_only")

        self._update_interval = convert_to_int(
            get_config_value(self._config_entry, "hsem_update_interval")
        )

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
        self._hsem_batteries_conversion_loss = get_config_value(
            self._config_entry,
            "hsem_batteries_conversion_loss",
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
        self._hsem_batteries_rated_capacity_max = get_config_value(
            self._config_entry, "hsem_huawei_solar_batteries_rated_capacity"
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
        return self._attr_unique_id

    @property
    def state(self):
        return self._state

    @property
    def should_poll(self):
        return False

    @property
    def available(self):
        return self._available

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""

        if self._missing_input_entities:
            return {
                "error": "Some of the required input sensors from the config flow is missing or not reporting a state. Check your configuration and make sure input sensors are configured correctly.",
                "last_updated": self._last_updated,
                "unique_id": self._attr_unique_id,
            }

        return {
            "read_only": self._read_only,
            "last_updated": self._last_updated,
            "last_changed_mode": self._last_changed_mode,
            "next_update": self._next_update,
            "update_interval": self._update_interval,
            "unique_id": self._attr_unique_id,
            "batteries_conversion_loss": self._hsem_batteries_conversion_loss,
            "batteries_remaining_charge": self._hsem_batteries_remaining_charge,
            "batteries_usable_capacity": self._hsem_batteries_usable_capacity,
            "batteries_current_capacity": self._hsem_batteries_current_capacity,
            "energi_data_service_export_entity": self._hsem_energi_data_service_export,
            "energi_data_service_export_value": self._hsem_energi_data_service_export_state,
            "energi_data_service_import_entity": self._hsem_energi_data_service_import,
            "energi_data_service_import_state": self._hsem_energi_data_service_import_state,
            "energy_needs": self._energy_needs,
            "ac_charge_cutoff_percentage": self._hsem_ac_charge_cutoff_percentage,
            "is_night_price_lower_than_morning": self._hsem_is_night_price_lower_than_morning,
            "is_night_price_lower_than_afternoon": self._hsem_is_night_price_lower_than_afternoon,
            "is_night_price_lower_than_evening": self._hsem_is_night_price_lower_than_evening,
            "is_night_price_lower_than_late_evening": self._hsem_is_night_price_lower_than_late_evening,
            "is_afternoon_price_lower_than_evening": self._hsem_is_afternoon_price_lower_than_evening,
            "is_afternoon_price_lower_than_late_evening": self._hsem_is_afternoon_price_lower_than_late_evening,
            "ev_charger_power_entity": self._hsem_ev_charger_power,
            "ev_charger_power_state": self._hsem_ev_charger_power_state,
            "ev_charger_status_entity": self._hsem_ev_charger_status,
            "ev_charger_status_state": self._hsem_ev_charger_status_state,
            "house_consumption_energy_weight_14d": self._hsem_house_consumption_energy_weight_14d,
            "house_consumption_energy_weight_1d": self._hsem_house_consumption_energy_weight_1d,
            "house_consumption_energy_weight_3d": self._hsem_house_consumption_energy_weight_3d,
            "house_consumption_energy_weight_7d": self._hsem_house_consumption_energy_weight_7d,
            "house_consumption_power_entity": self._hsem_house_consumption_power,
            "house_consumption_power_state": self._hsem_house_consumption_power_state,
            "house_power_includes_ev_charger_power": self._hsem_house_power_includes_ev_charger_power,
            "huawei_solar_batteries_enable_charge_hours_day_end": self._hsem_batteries_enable_charge_hours_day_end,
            "huawei_solar_batteries_enable_charge_hours_day_start": self._hsem_batteries_enable_charge_hours_day_start,
            "huawei_solar_batteries_enable_charge_hours_day": self._hsem_batteries_enable_charge_hours_day,
            "huawei_solar_batteries_enable_charge_hours_night_end": self._hsem_batteries_enable_charge_hours_night_end,
            "huawei_solar_batteries_enable_charge_hours_night_start": self._hsem_batteries_enable_charge_hours_night_start,
            "huawei_solar_batteries_enable_charge_hours_night": self._hsem_batteries_enable_charge_hours_night,
            "huawei_solar_batteries_grid_charge_cutoff_soc_entity": self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc,
            "huawei_solar_batteries_grid_charge_cutoff_soc_state": self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state,
            "huawei_solar_batteries_maximum_charging_power_entity": self._hsem_huawei_solar_batteries_maximum_charging_power,
            "huawei_solar_batteries_maximum_charging_power_state": self._hsem_huawei_solar_batteries_maximum_charging_power_state,
            "huawei_solar_batteries_rated_capacity_max_state": self._hsem_batteries_rated_capacity_max_state,
            "huawei_solar_batteries_rated_capacity_max": self._hsem_batteries_rated_capacity_max,
            "huawei_solar_batteries_rated_capacity_min_state": self._hsem_batteries_rated_capacity_min_state,
            "huawei_solar_batteries_state_of_capacity_entity": self._hsem_huawei_solar_batteries_state_of_capacity,
            "huawei_solar_batteries_state_of_capacity_state": self._hsem_huawei_solar_batteries_state_of_capacity_state,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_entity": self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_periods": self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_periods,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_state": self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_state,
            "huawei_solar_batteries_working_mode_entity": self._hsem_huawei_solar_batteries_working_mode,
            "huawei_solar_batteries_working_mode_state": self._hsem_huawei_solar_batteries_working_mode_state,
            "huawei_solar_device_id_batteries_id": self._hsem_huawei_solar_device_id_batteries,
            "huawei_solar_device_id_inverter_1_id": self._hsem_huawei_solar_device_id_inverter_1,
            "huawei_solar_device_id_inverter_2_id": self._hsem_huawei_solar_device_id_inverter_2,
            "huawei_solar_inverter_active_power_control_state_entity": self._hsem_huawei_solar_inverter_active_power_control,
            "huawei_solar_inverter_active_power_control_state_state": self._hsem_huawei_solar_inverter_active_power_control_state,
            "net_consumption_with_ev": self._hsem_net_consumption_with_ev,
            "net_consumption": self._hsem_net_consumption,
            "solar_production_power_entity": self._hsem_solar_production_power,
            "solar_production_power_state": self._hsem_solar_production_power_state,
            "solcast_pv_forecast_forecast_today_entity": self._hsem_solcast_pv_forecast_forecast_today,
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
            isinstance(self._hsem_batteries_rated_capacity_max_state, (int, float))
            and isinstance(
                self._hsem_huawei_solar_batteries_state_of_capacity_state, (int, float)
            )
            and isinstance(
                self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state,
                (int, float),
            )
        ):
            self._hsem_batteries_rated_capacity_min_state = (
                self._hsem_batteries_rated_capacity_max_state * 0.05
            )

            # Calculate the remaining charge needed to reach full capacity (kWh)
            self._hsem_batteries_remaining_charge = (
                (100 - self._hsem_huawei_solar_batteries_state_of_capacity_state)
                / 100
                * (self._hsem_batteries_rated_capacity_max_state / 1000)
            )

            # Calculate the maximum charge allowed from the grid based on cutoff SOC (kWh)
            max_allowed_grid_charge = (
                self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state
                * (self._hsem_batteries_rated_capacity_max_state / 1000)
                / 100
            )

            # Adjust remaining charge if it exceeds the max grid-allowed charge
            if self._hsem_batteries_remaining_charge > max_allowed_grid_charge:
                self._hsem_batteries_remaining_charge = max_allowed_grid_charge

            # Calculate usable capacity (kWh)
            self._hsem_batteries_usable_capacity = (
                self._hsem_batteries_rated_capacity_max_state / 1000
            ) - (self._hsem_batteries_rated_capacity_min_state / 1000)

            # Calculate current capacity (kWh)
            self._hsem_batteries_current_capacity = (
                self._hsem_huawei_solar_batteries_state_of_capacity_state
                / 100
                * (self._hsem_batteries_rated_capacity_max_state / 1000)
            )

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

        # Calculate the price intervals and check
        await self._async_calculate_compare_price_intervals()

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

        # Set the working mode, but not too frequently
        if last_changed_mode_seconds > 100 or self._last_changed_mode is None:
            await self._async_set_working_mode()
            if self._last_changed_mode is None:
                self._last_changed_mode = datetime.now().isoformat()

        if self._missing_input_entities:
            self._state = Recommendations.MissingInputEntities.value

        # Update last update time
        self._last_updated = now.isoformat()
        self._next_update = (now + timedelta(minutes=self._update_interval)).isoformat()
        self._available = True

        # Trigger an update in Home Assistant
        return self.async_write_ha_state()

    async def _async_calculate_compare_price_intervals(self):
        self._hsem_is_night_price_lower_than_morning = (
            await self._async_compare_price_intervals(0, 6, 6, 10)
        )
        self._hsem_is_night_price_lower_than_afternoon = (
            await self._async_compare_price_intervals(0, 6, 10, 17)
        )
        self._hsem_is_night_price_lower_than_evening = (
            await self._async_compare_price_intervals(0, 6, 17, 21)
        )
        self._hsem_is_night_price_lower_than_late_evening = (
            await self._async_compare_price_intervals(0, 6, 21, 23)
        )
        self._hsem_is_afternoon_price_lower_than_evening = (
            await self._async_compare_price_intervals(10, 17, 17, 21)
        )
        self._hsem_is_afternoon_price_lower_than_late_evening = (
            await self._async_compare_price_intervals(10, 17, 21, 23)
        )

    async def _async_force_charge_batteries(self):
        """
        Force battery charging based on configured time intervals for winter/spring.
        """
        now = datetime.now()

        if now.month not in DEFAULT_HSEM_MONTHS_WINTER_SPRING:
            return

        # Charge the battery when it's winter/spring and prices are high
        if self._hsem_batteries_enable_charge_hours_day_start is None:
            return

        if self._hsem_batteries_enable_charge_hours_day_end is None:
            return

        if self._hsem_batteries_enable_charge_hours_night_start is None:
            return

        if self._hsem_batteries_enable_charge_hours_night_end is None:
            return

        if not isinstance(
            self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state, (int, float)
        ):
            return

        if not isinstance(
            self._hsem_huawei_solar_batteries_state_of_capacity_state, (int, float)
        ):
            return

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
        if self._hsem_batteries_enable_charge_hours_night and now.hour < night_hour_end:
            _LOGGER.debug(
                f"Checking night charging between {night_hour_start} and {night_hour_end}."
            )
            await self._async_find_best_time_to_charge(night_hour_start, night_hour_end)

        # find best time to charge the battery at day
        if self._hsem_batteries_enable_charge_hours_day and now.hour < day_hour_end:
            _LOGGER.debug(
                f"Checking day charging between {day_hour_start} and {day_hour_end}."
            )
            await self._async_find_best_time_to_charge(day_hour_start, day_hour_end)

    async def _async_fetch_entity_states(self):
        # Fetch the current value from the EV charger status sensor

        # Reset
        self._missing_input_entities = False

        try:
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
                self._hsem_house_consumption_power_state = (
                    ha_get_entity_state_and_convert(
                        self, self._hsem_house_consumption_power, "float"
                    )
                )
            else:
                self._missing_input_entities = True

            # Fetch the current value from the solar production power sensor
            if self._hsem_solar_production_power:
                self._hsem_solar_production_power_state = (
                    ha_get_entity_state_and_convert(
                        self, self._hsem_solar_production_power, "float"
                    )
                )
            else:
                self._missing_input_entities = True

            # fetch the current value from the working mode sensor
            if self._hsem_huawei_solar_batteries_working_mode:
                self._hsem_huawei_solar_batteries_working_mode_state = (
                    ha_get_entity_state_and_convert(
                        self, self._hsem_huawei_solar_batteries_working_mode, "string"
                    )
                )
            else:
                self._missing_input_entities = True

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
            else:
                self._missing_input_entities = True

            # Fetch the current value from the energi data service import sensor
            if self._hsem_energi_data_service_import:
                self._hsem_energi_data_service_import_state = (
                    ha_get_entity_state_and_convert(
                        self, self._hsem_energi_data_service_import, "float", 3
                    )
                )
            else:
                self._missing_input_entities = True

            # Fetch the current value from the energi data service export sensor
            if self._hsem_energi_data_service_export:
                self._hsem_energi_data_service_export_state = (
                    ha_get_entity_state_and_convert(
                        self, self._hsem_energi_data_service_export, "float", 3
                    )
                )
            else:
                self._missing_input_entities = True

            if self._hsem_huawei_solar_inverter_active_power_control:
                self._hsem_huawei_solar_inverter_active_power_control_state = (
                    ha_get_entity_state_and_convert(
                        self,
                        self._hsem_huawei_solar_inverter_active_power_control,
                        "string",
                    )
                )
            else:
                self._missing_input_entities = True

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
            else:
                self._missing_input_entities = True

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
            else:
                self._missing_input_entities = True

            # Fetch the current value from the battery rated capacity sensor
            if self._hsem_batteries_rated_capacity_max:
                self._hsem_batteries_rated_capacity_max_state = (
                    ha_get_entity_state_and_convert(
                        self, self._hsem_batteries_rated_capacity_max, "float", 0
                    )
                )
            else:
                self._missing_input_entities = True

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
                else:
                    self._missing_input_entities = True
            else:
                self._missing_input_entities = True

        except Exception as e:
            self._missing_input_entities = True
            _LOGGER.warning(
                "Failed to fetch state for one or more sensors. Error: %s", str(e)
            )

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
            _LOGGER.debug(
                f"Import price is negative. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}"
            )
        elif (
            self._hourly_calculations.get(current_time_range, {}).get("recommendation")
            == Recommendations.ForceBatteriesCharge.value
        ):
            tou_modes = DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.ForceBatteriesCharge.value
            _LOGGER.debug(
                f"# Recommendation for {current_time_range} is to force charge the battery. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}"
            )
        elif self._hsem_ev_charger_status_state:
            # EV Charger is active. Disable battery discharge
            tou_modes = DEFAULT_HSEM_EV_CHARGER_TOU_MODES
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.EVSmartCharging.value
            _LOGGER.debug(
                f"EV Charger is active. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}"
            )
        elif (
            self._hourly_calculations.get(current_time_range, {}).get("recommendation")
            == Recommendations.ForceBatteriesDischarge.value
        ):
            tou_modes = DEFAULT_HSEM_TOU_MODES_FORCE_DISCHARGE
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.ForceBatteriesDischarge.value
            _LOGGER.debug(
                f"# Recommendation for {current_time_range} is to force discharge the battery. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}"
            )
        elif self._hsem_net_consumption < 0:
            # Positive net consumption. Charge battery from Solar
            working_mode = WorkingModes.MaximizeSelfConsumption.value
            state = Recommendations.MaximizeSelfConsumption.value
            _LOGGER.debug(
                f"Positive net consumption. Working Mode: {working_mode}, Solar Production: {self._hsem_solar_production_power_state}, House Consumption: {self._hsem_house_consumption_power_state}, Net Consumption: {self._hsem_net_consumption}"
            )
        elif (
            self._hourly_calculations.get(current_time_range, {}).get("recommendation")
            == Recommendations.MaximizeSelfConsumption.value
        ):
            working_mode = WorkingModes.MaximizeSelfConsumption.value
            state = Recommendations.MaximizeSelfConsumption.value
            _LOGGER.debug(
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
                _LOGGER.debug(
                    f"Default winter/spring settings. TOU Periods: {tou_modes} and Working Mode: {working_mode}"
                )

            # Summer settings
            if current_month in DEFAULT_HSEM_MONTHS_SUMMER:
                working_mode = WorkingModes.MaximizeSelfConsumption.value
                state = Recommendations.MaximizeSelfConsumption.value
                _LOGGER.debug(f"Default summer settings. Working Mode: {working_mode}")

        # Apply TOU periods if working mode is TOU
        if working_mode == WorkingModes.TimeOfUse.value:
            new_tou_modes_hash = generate_hash(str(tou_modes))
            current_tou_modes_hash = generate_hash(
                str(
                    self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_periods
                )
            )

            if new_tou_modes_hash != current_tou_modes_hash:
                _LOGGER.debug(
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

        if self._state != state:
            self._last_changed_mode = datetime.now().isoformat()

        self._state = state

    async def _async_reset_recommendations(self):
        """Reset the recommendations for each hour of the day."""
        self._hourly_calculations = {
            f"{hour:02d}-{(hour + 1) % 24:02d}": {
                "avg_house_consumption": 0.0,
                "solcast_pv_estimate": 0.0,
                "estimated_net_consumption": 0.0,
                "estimated_cost": 0.0,
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

        for hour, data in self._hourly_calculations.items():
            hour_start = int(hour.split("-")[0])
            hour_end = int(hour.split("-")[1])
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

                if (
                    self._hourly_calculations[time_range]["estimated_net_consumption"]
                    > 0
                ):
                    self._hourly_calculations[time_range]["estimated_cost"] = round(
                        (
                            price
                            * self._hourly_calculations[time_range][
                                "estimated_net_consumption"
                            ]
                        ),
                        2,
                    )

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
                if (
                    self._hourly_calculations[time_range]["estimated_net_consumption"]
                    < 0
                ):
                    self._hourly_calculations[time_range]["estimated_cost"] = round(
                        (
                            -1
                            * price
                            * self._hourly_calculations[time_range][
                                "estimated_net_consumption"
                            ]
                        ),
                        2,
                    )

        _LOGGER.debug(
            f"Updated hourly calculations with export prices: {self._hourly_calculations}"
        )

    async def _async_calculate_hourly_net_consumption(self):
        """Calculate the estimated net consumption for each hour of the day."""

        for hour, data in self._hourly_calculations.items():
            hour_start = int(hour.split("-")[0])
            hour_end = int(hour.split("-")[1])
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
                estimated_net_consumption = avg_house_consumption - solcast_pv_estimate

            # calculate the estimated net consumption
            if time_range in self._hourly_calculations:
                self._hourly_calculations[time_range]["estimated_net_consumption"] = (
                    round(estimated_net_consumption, 2)
                )

        _LOGGER.debug(
            f"Updated hourly calculations with Estimated Net Consumption: {self._hourly_calculations}"
        )

    async def _async_find_best_time_to_charge(self, start_hour=14, stop_hour=17):
        # Get the current time
        now = datetime.now()

        # Skip if the current hour is outside the specified range
        if now.hour >= stop_hour:
            return

        # Validate required variables
        required_vars = [
            self._hsem_batteries_rated_capacity_max_state,
            self._hsem_huawei_solar_batteries_maximum_charging_power_state,
            self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state,
            self._hsem_batteries_remaining_charge,
            self._hsem_batteries_conversion_loss,
            self._hsem_huawei_solar_batteries_state_of_capacity_state,
        ]
        if not all(isinstance(var, (int, float)) for var in required_vars):
            _LOGGER.warning(
                f"Missing or invalid variables for calculation: {required_vars}"
            )
            return

        if self._hsem_batteries_remaining_charge <= 0:
            return

        # Calculate max charge per hour
        conversion_loss_factor = 1 - (
            convert_to_float(self._hsem_batteries_conversion_loss) / 100
        )
        max_charge_per_hour = (
            convert_to_float(
                self._hsem_huawei_solar_batteries_maximum_charging_power_state
            )
            / 1000
        ) * conversion_loss_factor

        if max_charge_per_hour <= 0:
            _LOGGER.warning("Invalid maximum charging power. Skipping calculation.")
            return

        _LOGGER.warning(
            f"Calculating best time to charge battery between {start_hour} and {stop_hour}"
        )

        # Collect hours for analysis
        charging_hours = []
        for hour_start in range(start_hour, stop_hour):
            # Skip hours that have already passed
            if hour_start < now.hour:
                continue

            hour_end = (hour_start + 1) % 24
            time_range = f"{hour_start:02d}-{hour_end:02d}"

            if time_range in self._hourly_calculations:
                net_consumption = self._hourly_calculations[time_range].get(
                    "estimated_net_consumption"
                )
                import_price = self._hourly_calculations[time_range].get("import_price")

                if net_consumption is None or import_price is None:
                    continue

                # Prioritize negative import price
                if import_price < 0:
                    charging_hours.append(
                        (time_range, import_price, net_consumption, "negative_import")
                    )
                # Use surplus power
                elif net_consumption < 0 and (
                    convert_to_float(
                        self._hsem_huawei_solar_batteries_state_of_capacity_state
                    )
                    < convert_to_float(
                        self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state
                    )
                ):
                    charging_hours.append(
                        (time_range, import_price, net_consumption, "surplus")
                    )
                # Otherwise, consider import price
                else:
                    charging_hours.append(
                        (time_range, import_price, net_consumption, "import")
                    )

        # Sort hours by priority
        charging_hours.sort(
            key=lambda x: (x[3] != "negative_import", x[3] != "surplus", x[1])
        )

        # Mark hours for charging
        charged_energy = 0.0
        for time_range, import_price, net_consumption, source in charging_hours:
            if charged_energy >= self._hsem_batteries_remaining_charge:
                break

            remaining_charge_needed = (
                self._hsem_batteries_remaining_charge - charged_energy
            )

            # Adjust energy to charge based on surplus power (net_consumption)
            available_surplus = abs(net_consumption) if net_consumption < 0 else 0
            max_available_energy = min(
                max_charge_per_hour, remaining_charge_needed + available_surplus
            )

            # Deduct surplus from the actual charge needed
            actual_energy_to_charge = max(0, max_available_energy - available_surplus)

            # Mark hour for charging
            self._mark_hour_for_charging(time_range, actual_energy_to_charge, source)
            charged_energy += actual_energy_to_charge + available_surplus

            _LOGGER.warning(
                f"Marked hour {time_range} for charging using {source}. "
                f"Surplus Used: {round(available_surplus,2)} kWh, Energy Charged: {round(actual_energy_to_charge,2)} kWh, "
                f"Total Charged: {round(charged_energy,2)} kWh."
            )

        # Calculate total solar surplus after the charging hours
        solar_surplus = self._calculate_solar_surplus(charging_hours)

        # Adjust the AC charge cutoff
        await self._async_adjust_ac_charge_cutoff_soc(charged_energy, solar_surplus)

        _LOGGER.debug(
            f"Updated hourly calculations with charging plan: {self._hourly_calculations}"
        )

    def _calculate_solar_surplus(self, charging_hours):
        """
        Calculate the solar surplus after the charging period.

        Args:
            charging_hours (list): List of tuples with charging hour information.

        Returns:
            float: Total solar surplus in kWh.
        """
        if not charging_hours:
            return 0.0

        # Get the last charging hour
        last_charging_hour = charging_hours[-1][0]
        last_hour_end = int(last_charging_hour.split("-")[1])

        solar_surplus = 0.0
        for hour, data in self._hourly_calculations.items():
            hour_start = int(hour.split("-")[0])

            # Only consider hours after the last charging hour
            if hour_start >= last_hour_end:
                net_consumption = data.get("estimated_net_consumption", 0.0)
                if net_consumption < 0:
                    solar_surplus += abs(net_consumption)

        _LOGGER.warning(
            f"Solar surplus after battery charge available: {solar_surplus} kWh"
        )

        return solar_surplus

    async def _async_adjust_ac_charge_cutoff_soc(self, charged_energy, solar_surplus):
        """
        Adjust the AC Grid Charge Cutoff SoC based on solar surplus and charged energy.

        Args:
            charged_energy (float): Total energy marked for charging in kWh.
            solar_surplus (float): Total expected solar surplus in kWh.
        """
        if not self._hsem_batteries_rated_capacity_max_state:
            _LOGGER.warning("Missing battery capacity for cutoff adjustment.")
            return

        max_battery_capacity_kwh = (
            convert_to_float(self._hsem_batteries_rated_capacity_max_state) / 1000
        )

        # Default to 100% if no surplus is expected
        if solar_surplus <= 0:
            adjusted_cutoff_soc = 100.0
        else:
            # Calculate proportional reduction
            min_cutoff_soc = (
                convert_to_float(self._hsem_batteries_rated_capacity_min_state)
                / convert_to_float(self._hsem_batteries_rated_capacity_max_state)
            ) * 100

            if solar_surplus >= max_battery_capacity_kwh:
                # Maximum surplus, reduce to minimum cutoff
                adjusted_cutoff_soc = min_cutoff_soc
            else:
                # Proportional adjustment between 100% and min cutoff
                surplus_ratio = solar_surplus / max_battery_capacity_kwh
                adjusted_cutoff_soc = 100 - (surplus_ratio * (100 - min_cutoff_soc))

        # Update internal state
        self._hsem_ac_charge_cutoff_percentage = adjusted_cutoff_soc

        _LOGGER.warning(
            f"Adjusted AC Grid Charge Cutoff SoC: {self._hsem_ac_charge_cutoff_percentage}% "
            f"(Solar Surplus: {solar_surplus} kWh, Max Capacity: {max_battery_capacity_kwh} kWh)"
        )

    def _mark_hour_for_charging(self, time_range, energy_to_charge, source):
        self._hourly_calculations[time_range][
            "recommendation"
        ] = Recommendations.ForceBatteriesCharge.value
        self._hourly_calculations[time_range]["batteries_charged"] = energy_to_charge

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
            elif net_consumption < -1:
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

    async def _async_compare_price_intervals(
        self, start_hour_1, end_hour_1, start_hour_2, end_hour_2
    ):
        """
        Compares the sum of import prices between two intervals.

        Args:
            start_hour_1 (int): Start hour of the first interval.
            end_hour_1 (int): End hour of the first interval.
            start_hour_2 (int): Start hour of the second interval.
            end_hour_2 (int): End hour of the second interval.

        Returns:
            bool: True if the sum of import prices in the first interval is less than the second interval, False otherwise.
        """

        def calculate_total_import_price(start_hour, end_hour):
            total_price = 0
            for hour_start in range(start_hour, end_hour):
                hour_end = (hour_start + 1) % 24
                time_range = f"{hour_start:02d}-{hour_end:02d}"
                if time_range in self._hourly_calculations:
                    import_price = self._hourly_calculations[time_range].get(
                        "import_price"
                    )
                    if import_price is not None:
                        total_price += import_price
            return total_price

        # Calculate the total import price for both intervals
        total_price_1 = calculate_total_import_price(start_hour_1, end_hour_1)
        total_price_2 = calculate_total_import_price(start_hour_2, end_hour_2)

        _LOGGER.debug(
            f"Interval 1 ({start_hour_1}-{end_hour_1}): {total_price_1}, "
            f"Interval 2 ({start_hour_2}-{end_hour_2}): {total_price_2}"
        )

        # Return True if interval 1's total price is less than interval 2's
        return total_price_1 < total_price_2

    async def async_update(self):
        """Manually trigger the sensor update."""
        return await self._async_handle_update(None)

    async def async_added_to_hass(self):
        # Initial update
        await self._async_handle_update(None)

        # Schedule a periodic update every minute
        async_track_time_interval(
            self.hass,
            self._async_handle_update,
            timedelta(minutes=self._update_interval),
        )

        """Handle the sensor being added to Home Assistant."""
        return await super().async_added_to_hass()

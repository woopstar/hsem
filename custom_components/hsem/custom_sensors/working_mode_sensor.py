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

import voluptuous as vol
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import State
from homeassistant.helpers.event import async_track_time_interval

from custom_components.hsem.const import (
    DEFAULT_HSEM_BATTERIES_WAIT_MODE,
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
    async_logger,
    async_resolve_entity_id_from_unique_id,
    async_set_select_option,
    convert_to_float,
    convert_to_int,
    convert_to_time,
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
        self._hsem_extended_attributes = False
        self._hsem_verbose_logging = False
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
                "batteries_ac_cut_off": 100.0,
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
        self._missing_input_entities = True
        self._hsem_batteries_enable_batteries_schedule_1 = False
        self._hsem_batteries_enable_batteries_schedule_1_start = None
        self._hsem_batteries_enable_batteries_schedule_1_end = None
        self._hsem_batteries_enable_batteries_schedule_1_avg_import_price = 0.0
        self._hsem_batteries_enable_batteries_schedule_1_needed_batteries_capacity = 0.0
        self._hsem_batteries_enable_batteries_schedule_2_needed_batteries_capacity_cost = (
            0.0
        )
        self._hsem_batteries_enable_batteries_schedule_1_min_price_difference = 0.0
        self._hsem_batteries_enable_batteries_schedule_2 = False
        self._hsem_batteries_enable_batteries_schedule_2_start = None
        self._hsem_batteries_enable_batteries_schedule_2_end = None
        self._hsem_batteries_enable_batteries_schedule_2_avg_import_price = 0.0
        self._hsem_batteries_enable_batteries_schedule_2_needed_batteries_capacity = 0.0
        self._hsem_batteries_enable_batteries_schedule_2_needed_batteries_capacity_cost = (
            0.0
        )
        self._hsem_batteries_enable_batteries_schedule_2_min_price_difference = 0.0
        self._hsem_batteries_enable_batteries_schedule_3 = False
        self._hsem_batteries_enable_batteries_schedule_3_start = None
        self._hsem_batteries_enable_batteries_schedule_3_end = None
        self._hsem_batteries_enable_batteries_schedule_3_avg_import_price = 0.0
        self._hsem_batteries_enable_batteries_schedule_3_needed_batteries_capacity = 0.0
        self._hsem_batteries_enable_batteries_schedule_3_needed_batteries_capacity_cost = (
            0.0
        )
        self._hsem_batteries_enable_batteries_schedule_3_min_price_difference = 0.0
        self._hsem_entity_id_cache = {}
        self._attr_unique_id = get_working_mode_sensor_unique_id()
        self.entity_id = get_working_mode_sensor_entity_id()
        self._update_settings()

    def _update_settings(self):
        """Fetch updated settings from config_entry options."""
        self._read_only = get_config_value(self._config_entry, "hsem_read_only")

        self._hsem_extended_attributes = get_config_value(
            self._config_entry, "hsem_extended_attributes"
        )

        self._hsem_verbose_logging = get_config_value(
            self._config_entry, "hsem_verbose_logging"
        )

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

        if self._hsem_ev_charger_status == vol.UNDEFINED:
            self._hsem_ev_charger_status = None

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

        if self._hsem_ev_charger_power == vol.UNDEFINED:
            self._hsem_ev_charger_power = None

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
        self._hsem_batteries_rated_capacity_max = get_config_value(
            self._config_entry, "hsem_huawei_solar_batteries_rated_capacity"
        )
        self._hsem_batteries_enable_batteries_schedule_1 = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_1"
        )
        self._hsem_batteries_enable_batteries_schedule_1_start = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_1_start"
        )
        self._hsem_batteries_enable_batteries_schedule_1_end = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_1_end"
        )
        self._hsem_batteries_enable_batteries_schedule_1_min_price_difference = (
            get_config_value(
                self._config_entry,
                "hsem_batteries_enable_batteries_schedule_1_min_price_difference",
            )
        )
        self._hsem_batteries_enable_batteries_schedule_2 = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_2"
        )
        self._hsem_batteries_enable_batteries_schedule_2_start = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_2_start"
        )
        self._hsem_batteries_enable_batteries_schedule_2_end = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_2_end"
        )
        self._hsem_batteries_enable_batteries_schedule_2_min_price_difference = (
            get_config_value(
                self._config_entry,
                "hsem_batteries_enable_batteries_schedule_2_min_price_difference",
            )
        )
        self._hsem_batteries_enable_batteries_schedule_3 = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_3"
        )
        self._hsem_batteries_enable_batteries_schedule_3_start = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_3_start"
        )
        self._hsem_batteries_enable_batteries_schedule_3_end = get_config_value(
            self._config_entry, "hsem_batteries_enable_batteries_schedule_3_end"
        )
        self._hsem_batteries_enable_batteries_schedule_3_min_price_difference = (
            get_config_value(
                self._config_entry,
                "hsem_batteries_enable_batteries_schedule_3_min_price_difference",
            )
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
                "status": "error",
                "description": "Some of the required input sensors from the config flow is missing or not reporting a state yet. Check your configuration and make sure input sensors are configured correctly.",
                "last_updated": self._last_updated,
                "next_update": self._next_update,
                "unique_id": self._attr_unique_id,
            }

        if not self._available:
            return {
                "status": "wait",
                "description": "Waiting for sensor to be available.",
                "last_updated": self._last_updated,
                "next_update": self._next_update,
                "unique_id": self._attr_unique_id,
            }

        extended_attributes = {}
        if self._hsem_extended_attributes:
            extended_attributes = {
                "energi_data_service_export_entity": self._hsem_energi_data_service_export,
                "energi_data_service_import_entity": self._hsem_energi_data_service_import,
                "ev_charger_power_entity": self._hsem_ev_charger_power,
                "ev_charger_status_entity": self._hsem_ev_charger_status,
                "house_consumption_power_entity": self._hsem_house_consumption_power,
                "huawei_solar_batteries_grid_charge_cutoff_soc_entity": self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc,
                "huawei_solar_batteries_maximum_charging_power_entity": self._hsem_huawei_solar_batteries_maximum_charging_power,
                "huawei_solar_batteries_rated_capacity_max_entity": self._hsem_batteries_rated_capacity_max,
                "huawei_solar_batteries_state_of_capacity_entity": self._hsem_huawei_solar_batteries_state_of_capacity,
                "huawei_solar_batteries_tou_charging_and_discharging_periods_entity": self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods,
                "huawei_solar_batteries_working_mode_entity": self._hsem_huawei_solar_batteries_working_mode,
                "huawei_solar_device_id_batteries_id": self._hsem_huawei_solar_device_id_batteries,
                "huawei_solar_device_id_inverter_1_id": self._hsem_huawei_solar_device_id_inverter_1,
                "huawei_solar_device_id_inverter_2_id": self._hsem_huawei_solar_device_id_inverter_2,
                "huawei_solar_inverter_active_power_control_state_entity": self._hsem_huawei_solar_inverter_active_power_control,
                "next_update": self._next_update,
                "read_only": self._read_only,
                "solar_production_power_entity": self._hsem_solar_production_power,
                "solcast_pv_forecast_forecast_today_entity": self._hsem_solcast_pv_forecast_forecast_today,
                "unique_id": self._attr_unique_id,
                "update_interval": self._update_interval,
            }

        attributes = {
            "ac_charge_cutoff_percentage": self._hsem_ac_charge_cutoff_percentage,
            "batteries_conversion_loss": self._hsem_batteries_conversion_loss,
            "batteries_current_capacity": self._hsem_batteries_current_capacity,
            "batteries_usable_capacity": self._hsem_batteries_usable_capacity,
            "energi_data_service_export_state": self._hsem_energi_data_service_export_state,
            "energi_data_service_import_state": self._hsem_energi_data_service_import_state,
            "energy_needs": self._energy_needs,
            "ev_charger_power_state": self._hsem_ev_charger_power_state,
            "ev_charger_status_state": self._hsem_ev_charger_status_state,
            "hourly_calculations": self._hourly_calculations,
            "house_consumption_energy_weight_14d": self._hsem_house_consumption_energy_weight_14d,
            "house_consumption_energy_weight_1d": self._hsem_house_consumption_energy_weight_1d,
            "house_consumption_energy_weight_3d": self._hsem_house_consumption_energy_weight_3d,
            "house_consumption_energy_weight_7d": self._hsem_house_consumption_energy_weight_7d,
            "house_consumption_power_state": self._hsem_house_consumption_power_state,
            "house_power_includes_ev_charger_power": self._hsem_house_power_includes_ev_charger_power,
            "huawei_solar_batteries_enable_batteries_schedule_1_end": self._hsem_batteries_enable_batteries_schedule_1_end,
            "huawei_solar_batteries_enable_batteries_schedule_1_needed_batteries_capacity": self._hsem_batteries_enable_batteries_schedule_1_needed_batteries_capacity,
            "huawei_solar_batteries_enable_batteries_schedule_1_needed_batteries_capacity_cost": self._hsem_batteries_enable_batteries_schedule_1_needed_batteries_capacity_cost,
            "huawei_solar_batteries_enable_batteries_schedule_1_start": self._hsem_batteries_enable_batteries_schedule_1_start,
            "huawei_solar_batteries_enable_batteries_schedule_1_avg_import_price": self._hsem_batteries_enable_batteries_schedule_1_avg_import_price,
            "huawei_solar_batteries_enable_batteries_schedule_1": self._hsem_batteries_enable_batteries_schedule_1,
            "huawei_solar_batteries_enable_batteries_schedule_1_min_price_difference": self._hsem_batteries_enable_batteries_schedule_1_min_price_difference,
            "huawei_solar_batteries_enable_batteries_schedule_2_end": self._hsem_batteries_enable_batteries_schedule_2_end,
            "huawei_solar_batteries_enable_batteries_schedule_2_needed_batteries_capacity": self._hsem_batteries_enable_batteries_schedule_2_needed_batteries_capacity,
            "huawei_solar_batteries_enable_batteries_schedule_2_needed_batteries_capacity_cost": self._hsem_batteries_enable_batteries_schedule_2_needed_batteries_capacity_cost,
            "huawei_solar_batteries_enable_batteries_schedule_2_start": self._hsem_batteries_enable_batteries_schedule_2_start,
            "huawei_solar_batteries_enable_batteries_schedule_2_avg_import_price": self._hsem_batteries_enable_batteries_schedule_2_avg_import_price,
            "huawei_solar_batteries_enable_batteries_schedule_2": self._hsem_batteries_enable_batteries_schedule_2,
            "huawei_solar_batteries_enable_batteries_schedule_2_min_price_difference": self._hsem_batteries_enable_batteries_schedule_2_min_price_difference,
            "huawei_solar_batteries_enable_batteries_schedule_3_end": self._hsem_batteries_enable_batteries_schedule_3_end,
            "huawei_solar_batteries_enable_batteries_schedule_3_needed_batteries_capacity": self._hsem_batteries_enable_batteries_schedule_3_needed_batteries_capacity,
            "huawei_solar_batteries_enable_batteries_schedule_3_needed_batteries_capacity_cost": self._hsem_batteries_enable_batteries_schedule_3_needed_batteries_capacity_cost,
            "huawei_solar_batteries_enable_batteries_schedule_3_start": self._hsem_batteries_enable_batteries_schedule_3_start,
            "huawei_solar_batteries_enable_batteries_schedule_3_avg_import_price": self._hsem_batteries_enable_batteries_schedule_3_avg_import_price,
            "huawei_solar_batteries_enable_batteries_schedule_3": self._hsem_batteries_enable_batteries_schedule_3,
            "huawei_solar_batteries_enable_batteries_schedule_3_min_price_difference": self._hsem_batteries_enable_batteries_schedule_3_min_price_difference,
            "huawei_solar_batteries_grid_charge_cutoff_soc_state": self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state,
            "huawei_solar_batteries_maximum_charging_power_state": self._hsem_huawei_solar_batteries_maximum_charging_power_state,
            "huawei_solar_batteries_rated_capacity_max_state": self._hsem_batteries_rated_capacity_max_state,
            "huawei_solar_batteries_rated_capacity_min_state": self._hsem_batteries_rated_capacity_min_state,
            "huawei_solar_batteries_state_of_capacity_state": self._hsem_huawei_solar_batteries_state_of_capacity_state,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_periods": self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_periods,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_state": self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_state,
            "huawei_solar_batteries_working_mode_state": self._hsem_huawei_solar_batteries_working_mode_state,
            "huawei_solar_inverter_active_power_control_state_state": self._hsem_huawei_solar_inverter_active_power_control_state,
            "is_afternoon_price_lower_than_evening": self._hsem_is_afternoon_price_lower_than_evening,
            "is_afternoon_price_lower_than_late_evening": self._hsem_is_afternoon_price_lower_than_late_evening,
            "is_night_price_lower_than_afternoon": self._hsem_is_night_price_lower_than_afternoon,
            "is_night_price_lower_than_evening": self._hsem_is_night_price_lower_than_evening,
            "is_night_price_lower_than_late_evening": self._hsem_is_night_price_lower_than_late_evening,
            "is_night_price_lower_than_morning": self._hsem_is_night_price_lower_than_morning,
            "last_changed_mode": self._last_changed_mode,
            "last_updated": self._last_updated,
            "net_consumption_with_ev": self._hsem_net_consumption_with_ev,
            "net_consumption": self._hsem_net_consumption,
            "solar_production_power_state": self._hsem_solar_production_power_state,
        }

        status = {
            "status": "ok",
        }
        if self._read_only:
            status = {
                "status": "read_only",
            }

        # Return sorted attributes
        return {
            key: value
            for key, value in sorted(
                {**attributes, **extended_attributes, **status}.items()
            )
        }

    async def _async_handle_update(self, event):
        """Handle the sensor state update (for both manual and state change)."""

        await async_logger(self, "------ Updating working mode sensor state...")

        # Get the current time
        now = datetime.now()

        # Ensure config flow settings are reloaded if it changed.
        self._update_settings()

        # Fetch the latest entity states
        await self._async_fetch_entity_states()

        if self._missing_input_entities:
            self._state = Recommendations.MissingInputEntities.value
        else:
            # Calculate the net consumption
            await self._async_calculate_net_consumption()

            # calculate the remaining battery capacity
            await self._async_calculate_remaining_battery_capacity()

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

            # Calculate the price intervals and check
            await self._async_calculate_compare_price_intervals()

            # calculate the batteries schedules
            await self._async_calculate_batteries_schedules()

            # calculate the best charge time for batteries
            await self._async_calculate_batteries_schedules_best_charge_time()

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

        # Update last update time
        self._last_updated = now.isoformat()
        self._next_update = (now + timedelta(minutes=self._update_interval)).isoformat()
        self._available = True

        await async_logger(
            self, "------ Completed updating working mode sensor state..."
        )

        # Trigger an update in Home Assistant
        return self.async_write_ha_state()

    async def _async_calculate_remaining_battery_capacity(self):
        # Calculate remaining battery capacity and max allowed charge from grid if all necessary values are available
        if isinstance(
            self._hsem_batteries_rated_capacity_max_state, (int, float)
        ) and isinstance(
            self._hsem_huawei_solar_batteries_state_of_capacity_state, (int, float)
        ):
            self._hsem_batteries_rated_capacity_min_state = (
                self._hsem_batteries_rated_capacity_max_state * 0.05
            )

            # Calculate usable capacity (kWh)
            self._hsem_batteries_usable_capacity = round(
                (self._hsem_batteries_rated_capacity_max_state / 1000)
                - (self._hsem_batteries_rated_capacity_min_state / 1000),
                2,
            )

            # Calculate the buffer in kWh
            buffer = self._hsem_batteries_rated_capacity_min_state / 1000

            # Calculate current capacity (kWh)
            self._hsem_batteries_current_capacity = round(
                max(
                    0,
                    (
                        self._hsem_huawei_solar_batteries_state_of_capacity_state
                        / 100
                        * (self._hsem_batteries_rated_capacity_max_state / 1000)
                    )
                    - buffer,
                ),
                2,
            )

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
        working_mode = None

        # Determine the appropriate TOU modes and working mode state. In priority order:
        if (
            isinstance(self._hsem_energi_data_service_import_state, (int, float))
            and self._hsem_energi_data_service_import_state < 0
        ):
            # Negative import price. Force charge battery
            tou_modes = DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.ForceExport.value
            await async_logger(
                self,
                f"Import price is negative. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}",
            )
        elif (
            self._hourly_calculations.get(current_time_range, {}).get("recommendation")
            == Recommendations.BatteriesChargeGrid.value
        ):
            tou_modes = DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.BatteriesChargeGrid.value
            await async_logger(
                self,
                f"# Recommendation for {current_time_range} is to force charge the batteries. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}",
            )
        elif self._hsem_ev_charger_status_state:
            # EV Charger is active. Disable battery discharge
            tou_modes = DEFAULT_HSEM_EV_CHARGER_TOU_MODES
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.EVSmartCharging.value
            await async_logger(
                self,
                f"EV Charger is active. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}",
            )
        elif (
            self._hourly_calculations.get(current_time_range, {}).get("recommendation")
            == Recommendations.ForceBatteriesDischarge.value
        ):
            tou_modes = DEFAULT_HSEM_TOU_MODES_FORCE_DISCHARGE
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.ForceBatteriesDischarge.value
            await async_logger(
                self,
                f"# Recommendation for {current_time_range} is to force discharge the batteries. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}",
            )
        elif self._hsem_net_consumption < 0:
            # Positive net consumption. Charge battery from Solar
            working_mode = WorkingModes.MaximizeSelfConsumption.value
            state = Recommendations.MaximizeSelfConsumption.value
            await async_logger(
                self,
                f"Positive net consumption. Working Mode: {working_mode}, Solar Production: {self._hsem_solar_production_power_state}, House Consumption: {self._hsem_house_consumption_power_state}, Net Consumption: {self._hsem_net_consumption}",
            )
        elif (
            self._hourly_calculations.get(current_time_range, {}).get("recommendation")
            == Recommendations.MaximizeSelfConsumption.value
        ):
            working_mode = WorkingModes.MaximizeSelfConsumption.value
            state = Recommendations.MaximizeSelfConsumption.value
            await async_logger(
                self,
                f"# Recommendation for {current_time_range} is to set working mode to Maximize Self Consumption",
            )
        elif (
            self._hourly_calculations.get(current_time_range, {}).get("recommendation")
            == Recommendations.BatteriesDischargeMode.value
        ):
            working_mode = WorkingModes.MaximizeSelfConsumption.value
            state = Recommendations.BatteriesDischargeMode.value
            await async_logger(
                self,
                f"# Recommendation for {current_time_range} is to set working mode to Maximize Self Consumption to enable batteries discharge to cover load",
            )
        elif (
            self._hourly_calculations.get(current_time_range, {}).get("recommendation")
            == Recommendations.BatteriesWaitMode.value
        ):
            # Winter/Spring settings
            if current_month in DEFAULT_HSEM_MONTHS_WINTER_SPRING:
                tou_modes = DEFAULT_HSEM_BATTERIES_WAIT_MODE
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
        if (
            self._hsem_huawei_solar_batteries_working_mode_state != working_mode
            and working_mode is not None
        ):
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
                "batteries_ac_cut_off": 100.0,
            }
            for hour in range(24)
        }

    async def _async_calculate_hourly_data(self):
        """Calculate the weighted hourly data for the sensor using both 3-day and 7-day HouseConsumptionEnergyAverageSensors."""

        if self._hsem_house_consumption_energy_weight_1d is None:
            await async_logger(
                self, "Weight for 1d is None. Skipping this calculation."
            )
            return

        if self._hsem_house_consumption_energy_weight_3d is None:
            await async_logger(
                self, "Weight for 3d is None. Skipping this calculation."
            )
            return

        if self._hsem_house_consumption_energy_weight_7d is None:
            await async_logger(
                self, "Weight for 7d is None. Skipping this calculation."
            )
            return

        if self._hsem_house_consumption_energy_weight_14d is None:
            await async_logger(
                self, "Weight for 14d is None. Skipping this calculation."
            )
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
            if unique_id_1d not in self._hsem_entity_id_cache:
                entity_id_1d = await async_resolve_entity_id_from_unique_id(
                    self, unique_id_1d
                )
                if entity_id_1d is not None:
                    self._hsem_entity_id_cache[unique_id_1d] = entity_id_1d
            else:
                entity_id_1d = self._hsem_entity_id_cache[unique_id_1d]

            if unique_id_3d not in self._hsem_entity_id_cache:
                entity_id_3d = await async_resolve_entity_id_from_unique_id(
                    self, unique_id_3d
                )
                if entity_id_3d is not None:
                    self._hsem_entity_id_cache[unique_id_3d] = entity_id_3d
            else:
                entity_id_3d = self._hsem_entity_id_cache[unique_id_3d]

            if unique_id_7d not in self._hsem_entity_id_cache:
                entity_id_7d = await async_resolve_entity_id_from_unique_id(
                    self, unique_id_7d
                )

                if entity_id_7d is not None:
                    self._hsem_entity_id_cache[unique_id_7d] = entity_id_7d
            else:
                entity_id_7d = self._hsem_entity_id_cache[unique_id_7d]

            if unique_id_14d not in self._hsem_entity_id_cache:
                entity_id_14d = await async_resolve_entity_id_from_unique_id(
                    self, unique_id_14d
                )

                if entity_id_14d is not None:
                    self._hsem_entity_id_cache[unique_id_14d] = entity_id_14d
            else:
                entity_id_14d = self._hsem_entity_id_cache[unique_id_14d]

            # Default values for sensors in case they are missing
            value_1d = None
            value_3d = None
            value_7d = None
            value_14d = None
            weighted_value_1d = None
            weighted_value_3d = None
            weighted_value_7d = None
            weighted_value_14d = None
            avg_house_consumption = None

            # Fetch values for 1d, 3d, 7d, and 14d if available
            try:
                if entity_id_1d is not None:
                    value_1d = ha_get_entity_state_and_convert(
                        self, entity_id_1d, "float", 3
                    )
                if entity_id_3d is not None:
                    value_3d = ha_get_entity_state_and_convert(
                        self, entity_id_3d, "float", 3
                    )

                if entity_id_7d is not None:
                    value_7d = ha_get_entity_state_and_convert(
                        self, entity_id_7d, "float", 3
                    )

                if entity_id_14d is not None:
                    value_14d = ha_get_entity_state_and_convert(
                        self, entity_id_14d, "float", 3
                    )
            except ValueError:
                value_1d = None
                value_3d = None
                value_7d = None
                value_14d = None
                avg_house_consumption = None

            if (
                value_1d is not None
                and value_3d is not None
                and value_7d is not None
                and value_14d is not None
                and entity_id_1d is not None
                and entity_id_3d is not None
                and entity_id_7d is not None
                and entity_id_14d is not None
            ):
                weighted_value_1d = value_1d * (
                    self._hsem_house_consumption_energy_weight_1d / 100
                )
                weighted_value_3d = value_3d * (
                    self._hsem_house_consumption_energy_weight_3d / 100
                )
                weighted_value_7d = value_7d * (
                    self._hsem_house_consumption_energy_weight_7d / 100
                )
                weighted_value_14d = value_14d * (
                    self._hsem_house_consumption_energy_weight_14d / 100
                )

                avg_house_consumption = round(
                    (
                        weighted_value_1d
                        + weighted_value_3d
                        + weighted_value_7d
                        + weighted_value_14d
                    ),
                    3,
                )

            self._hourly_calculations[time_range][
                "avg_house_consumption"
            ] = avg_house_consumption

            # await async_logger(
            #     self,
            #     f"time_range: {time_range}, "
            #     f"avg_house_consumption: {round(avg_house_consumption, 3)}, "
            #     f"value_1d: {round(value_1d, 3)}, "
            #     f"value_3d: {round(value_3d, 3)}, "
            #     f"value_7d: {round(value_7d, 3)}, "
            #     f"value_14d: {round(value_14d, 3)}, "
            #     f"weighted_value_1d: {round(weighted_value_1d, 3)}, "
            #     f"weighted_value_3d: {round(weighted_value_3d, 3)}, "
            #     f"weighted_value_7d: {round(weighted_value_7d, 3)}, "
            #     f"weighted_value_14d: {round(weighted_value_14d, 3)}, "
            #     f"entity_id_1d: {entity_id_1d}, "
            #     f"entity_id_3d: {entity_id_3d}, "
            #     f"entity_id_7d: {entity_id_7d}, "
            #     f"entity_id_14d: {entity_id_14d}, ",
            # )

    async def _async_calculate_solcast_forecast(self):
        """Calculate the hourly Solcast PV estimate and update self._hourly_calculations without resetting avg_house_consumption."""
        solcast_sensor = self.hass.states.get(
            self._hsem_solcast_pv_forecast_forecast_today
        )
        if not solcast_sensor:
            _LOGGER.debug("Solcast forecast sensor not found.")
            return

        detailed_forecast = solcast_sensor.attributes.get("detailedForecast", [])
        if not detailed_forecast:
            _LOGGER.debug("Detailed forecast data is missing or empty.")
            return

        for period in detailed_forecast:
            period_start = period.get("period_start")
            pv_estimate = period.get("pv_estimate", 0.0)
            time_range = f"{period_start.hour:02d}-{(period_start.hour + 1) % 24:02d}"

            # Only update "solcast_pv_estimate" in the existing dictionary entry
            if time_range in self._hourly_calculations:
                self._hourly_calculations[time_range]["solcast_pv_estimate"] = round(
                    pv_estimate, 3
                )

    async def _async_calculate_hourly_import_price(self):
        """Calculate the estimated import price for each hour of the day."""
        if self._hsem_energi_data_service_import is None:
            return

        import_price_sensor = self.hass.states.get(
            self._hsem_energi_data_service_import
        )

        if not import_price_sensor:
            _LOGGER.debug("hsem_energi_data_service_import sensor not found.")
            return

        detailed_raw_today = import_price_sensor.attributes.get("raw_today", [])
        if not detailed_raw_today:
            _LOGGER.debug("Detailed raw data is missing or empty for import prices.")
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
            _LOGGER.debug("hsem_energi_data_service_export sensor not found.")
            return

        detailed_raw_today = export_price_sensor.attributes.get("raw_today", [])
        if not detailed_raw_today:
            _LOGGER.debug("Detailed raw data is missing or empty for export prices.")
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

            if solcast_pv_estimate is None:
                solcast_pv_estimate = 0.0

            if avg_house_consumption is None:
                estimated_net_consumption = None
            else:
                estimated_net_consumption = round(
                    avg_house_consumption - solcast_pv_estimate, 3
                )

            # calculate the estimated net consumption
            if time_range in self._hourly_calculations:
                self._hourly_calculations[time_range][
                    "estimated_net_consumption"
                ] = estimated_net_consumption

        _LOGGER.debug(
            f"Updated hourly calculations with Estimated Net Consumption: {self._hourly_calculations}"
        )

    async def _async_find_best_time_to_charge(
        self,
        start_hour=14,
        stop_hour=17,
        required_charge=0,
        min_price_diff=0,
        avg_import_price=0,
    ):
        """Find best time to charge based on prioritized conditions."""
        now = datetime.now()

        # Skip if current hour is outside range
        if now.hour >= stop_hour:
            return

        # Validate required variables
        required_vars = [
            self._hsem_batteries_rated_capacity_max_state,
            self._hsem_huawei_solar_batteries_maximum_charging_power_state,
            self._hsem_batteries_conversion_loss,
        ]
        if not all(isinstance(var, (int, float)) for var in required_vars):
            await async_logger(
                self, f"Missing or invalid variables for calculation: {required_vars}"
            )
            return

        # Calculate max charge per hour with conversion loss
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
            await async_logger(
                self, "Invalid maximum charging power. Skipping calculation."
            )
            return

        await async_logger(
            self,
            f"Planning of charging the batteries started. "
            f"Time range: {start_hour} and {stop_hour}. "
            f"Max batteries capacity: {round(self._hsem_batteries_rated_capacity_max_state / 1000, 2)} kWh. "
            f"Conversion loss: {round(self._hsem_batteries_conversion_loss, 2)}%. "
            f"Max charge per hour: {round(max_charge_per_hour, 2)} kWh. ",
        )

        # Collect all valid hours
        available_hours = []
        for hour_start in range(start_hour, stop_hour):
            if hour_start < now.hour:
                continue

            hour_end = (hour_start + 1) % 24
            time_range = f"{hour_start:02d}-{hour_end:02d}"

            if time_range not in self._hourly_calculations:
                continue

            data = self._hourly_calculations[time_range]
            net_consumption = data.get("estimated_net_consumption")
            import_price = data.get("import_price")

            if net_consumption is None or import_price is None:
                continue

            available_hours.append((time_range, import_price, net_consumption))

        # First priority: Negative import prices
        charged_energy = 0.0

        for time_range, price, net_consumption in sorted(
            available_hours, key=lambda x: x[1]
        ):
            if price >= 0 or charged_energy >= required_charge:
                break

            energy_to_charge = min(
                max_charge_per_hour, required_charge - charged_energy
            )

            if energy_to_charge > 0:
                self._hourly_calculations[time_range][
                    "recommendation"
                ] = Recommendations.BatteriesChargeGrid.value
                self._hourly_calculations[time_range][
                    "batteries_charged"
                ] = energy_to_charge
                charged_energy += energy_to_charge

                await async_logger(
                    self,
                    f"Hour: {time_range}. "
                    f"Charging from grid due to negative import price. "
                    f"Import Price: {price}"
                    f"Energy charged: {round(energy_to_charge, 2)} kWh. "
                    f"Total energy charged: {round(charged_energy, 2)} kWh. ",
                )

        # Second priority: Solar surplus (negative net consumption)
        if charged_energy < required_charge:
            solar_hours = [(t, p, nc) for t, p, nc in available_hours if nc < 0]
            solar_hours.sort(
                key=lambda x: x[2]
            )  # Sort by most negative net consumption

            for time_range, price, net_consumption in solar_hours:
                if charged_energy >= required_charge:
                    break

                available_solar = abs(net_consumption)
                energy_to_charge = min(
                    max_charge_per_hour,
                    required_charge - charged_energy,
                    available_solar,
                )

                if energy_to_charge > 0:
                    self._hourly_calculations[time_range][
                        "recommendation"
                    ] = Recommendations.BatteriesChargeSolar.value
                    self._hourly_calculations[time_range][
                        "batteries_charged"
                    ] = energy_to_charge
                    charged_energy += energy_to_charge

                    await async_logger(
                        self,
                        f"Hour: {time_range}. "
                        f"Charging from solar. "
                        f"Energy charged: {round(energy_to_charge, 2)} kWh. "
                        f"Total energy charged: {round(charged_energy, 2)} kWh. "
                        f"Available Solar: {round(available_solar, 2)} kWh. "
                        f"Net Consumption: {round(net_consumption, 2)} kWh. ",
                    )

        # Third priority: Cheapest remaining hours considering partial solar contribution
        await async_logger(self, "Finding cheapest hours to import energy. ")

        charged_energy_before = charged_energy
        if charged_energy < required_charge:
            remaining_hours = [(t, p, nc) for t, p, nc in available_hours]
            remaining_hours.sort(key=lambda x: x[1])  # Sort by price

            avg_charge_import_price = 0.0
            avg_charge_import_count = 0

            for time_range, price, net_consumption in remaining_hours:
                if charged_energy >= required_charge:
                    break

                available_solar = abs(net_consumption) if net_consumption < 0 else 0
                grid_energy_needed = min(
                    max_charge_per_hour - available_solar,
                    required_charge - charged_energy - available_solar,
                )

                energy_to_charge = available_solar + grid_energy_needed

                if energy_to_charge > 0:
                    avg_charge_import_count += 1
                    avg_charge_import_price += price
                    charged_energy += energy_to_charge

                    await async_logger(
                        self,
                        f"Hour: {time_range}. Import Price: {round(price, 3)}",
                    )

            avg_charge_import = avg_charge_import_price / avg_charge_import_count
            avg_charge_diff = avg_import_price - avg_charge_import

            if avg_charge_import_count > 0:
                await async_logger(
                    self,
                    f"Charging from grid cost calculation. "
                    f"Average Charge Import Price: {round(avg_charge_import, 2)}, "
                    f"Average Usage Price: {round(avg_import_price, 2)}, "
                    f"Charge Price Difference: {round(avg_charge_diff, 2)}, "
                    f"Min Price Difference: {round(min_price_diff, 2)}, ",
                )

        # Lets charge if price diff is enough
        charged_energy = charged_energy_before

        min_price_check = True
        if min_price_diff != 0 and avg_charge_diff < min_price_diff:
            min_price_check = False

        if charged_energy < required_charge and min_price_check:
            remaining_hours = [(t, p, nc) for t, p, nc in available_hours]
            remaining_hours.sort(key=lambda x: x[1])  # Sort by price

            for time_range, price, net_consumption in remaining_hours:
                if charged_energy >= required_charge:
                    break

                available_solar = abs(net_consumption) if net_consumption < 0 else 0
                grid_energy_needed = min(
                    max_charge_per_hour - available_solar,
                    required_charge - charged_energy - available_solar,
                )

                energy_to_charge = available_solar + grid_energy_needed

                if energy_to_charge > 0:
                    self._hourly_calculations[time_range][
                        "recommendation"
                    ] = Recommendations.BatteriesChargeGrid.value
                    self._hourly_calculations[time_range][
                        "batteries_charged"
                    ] = energy_to_charge
                    charged_energy += energy_to_charge

                    await async_logger(
                        self,
                        f"Hour: {time_range}. "
                        f"Charging from grid. "
                        f"Energy charged: {round(energy_to_charge, 2)} kWh. "
                        f"Total energy charged: {round(charged_energy, 2)} kWh. "
                        f"Available Solar: {round(available_solar, 2)} kWh. "
                        f"Net Consumption: {round(net_consumption, 2)} kWh. "
                        f"Import Price: {price}",
                    )

        await async_logger(
            self,
            f"Planning of charging the batteries completed. "
            f"Total energy charged: {round(charged_energy, 2)} kWh. ",
        )

    async def _async_calculate_batteries_schedules_best_charge_time(self):
        """
        Calculate the best times to charge batteries based on active schedules.
        Identifies the cheapest charging times to meet the combined energy needs of all schedules,
        while respecting battery capacity and current charge.
        """
        now = datetime.now().time()

        await async_logger(
            self,
            f"Calculating best time to charge batteries based on active schedules at {now.strftime('%H:%M:%S')} ",
        )

        # Gather all active schedules
        schedules = []
        if self._hsem_batteries_enable_batteries_schedule_1:
            schedules.append(
                {
                    "start": convert_to_time(
                        self._hsem_batteries_enable_batteries_schedule_1_start
                    ),
                    "end": convert_to_time(
                        self._hsem_batteries_enable_batteries_schedule_1_end
                    ),
                    "required_charge": self._hsem_batteries_enable_batteries_schedule_1_needed_batteries_capacity,
                    "min_price_diff": self._hsem_batteries_enable_batteries_schedule_1_min_price_difference,
                    "avg_import_price": self._hsem_batteries_enable_batteries_schedule_1_avg_import_price,
                }
            )
        if self._hsem_batteries_enable_batteries_schedule_2:
            schedules.append(
                {
                    "start": convert_to_time(
                        self._hsem_batteries_enable_batteries_schedule_2_start
                    ),
                    "end": convert_to_time(
                        self._hsem_batteries_enable_batteries_schedule_2_end
                    ),
                    "required_charge": self._hsem_batteries_enable_batteries_schedule_2_needed_batteries_capacity,
                    "min_price_diff": self._hsem_batteries_enable_batteries_schedule_2_min_price_difference,
                    "avg_import_price": self._hsem_batteries_enable_batteries_schedule_2_avg_import_price,
                }
            )
        if self._hsem_batteries_enable_batteries_schedule_3:
            schedules.append(
                {
                    "start": convert_to_time(
                        self._hsem_batteries_enable_batteries_schedule_3_start
                    ),
                    "end": convert_to_time(
                        self._hsem_batteries_enable_batteries_schedule_3_end
                    ),
                    "required_charge": self._hsem_batteries_enable_batteries_schedule_3_needed_batteries_capacity,
                    "min_price_diff": self._hsem_batteries_enable_batteries_schedule_3_min_price_difference,
                    "avg_import_price": self._hsem_batteries_enable_batteries_schedule_3_avg_import_price,
                }
            )

        await async_logger(self, f"Found {len(schedules)} active schedules.")

        # Filter schedules that are still relevant
        schedules = [s for s in schedules if s["start"] > now]
        await async_logger(
            self,
            f"{len(schedules)} schedules remain after filtering based on the current time.",
        )

        if not schedules:
            await async_logger(self, "No schedules to process. Exiting calculation.")
            return

        # Sort schedules by start time
        schedules.sort(key=lambda s: s["start"])
        await async_logger(self, "Schedules sorted by start time.")

        # Calculate the total required charge across all schedules
        total_required_charge = sum(s["required_charge"] for s in schedules)

        await async_logger(
            self,
            f"Total Required Charge: {round(total_required_charge, 2)} kWh. "
            f"Current Useable Batteries Capacity: {self._hsem_batteries_current_capacity} kWh.",
        )

        # Subtract current battery usable capacity
        total_required_charge = max(
            0, total_required_charge - self._hsem_batteries_current_capacity
        )

        # Respect battery capacity
        max_battery_capacity_kwh = self._hsem_batteries_rated_capacity_max_state / 1000
        total_required_charge = min(total_required_charge, max_battery_capacity_kwh)

        await async_logger(
            self,
            f"Total required charge accounting for useable batteries capacity: {round(total_required_charge, 2)} kWh. ",
        )

        if total_required_charge <= 0:
            await async_logger(
                self,
                f"Skipping charge as the batteries already has sufficient capacity. ",
            )
            return

        # Find the best time to charge before the first schedule starts
        first_schedule_start_hour = schedules[0]["start"].hour
        await async_logger(
            self,
            f"Finding the best time to charge {round(total_required_charge, 2)} kWh "
            f"before {first_schedule_start_hour}:00.",
        )
        await self._async_find_best_time_to_charge(
            now.hour,
            first_schedule_start_hour,
            total_required_charge,
            schedules[0]["min_price_diff"],
            schedules[0]["avg_import_price"],
        )

        # Calculate remaining charge needs after each schedule window
        current_capacity = self._hsem_batteries_current_capacity
        for i, schedule in enumerate(schedules):
            if current_capacity >= schedule["required_charge"]:
                await async_logger(
                    self,
                    f"Schedule {i + 1} already covered by current usable batteries capacity. "
                    f"Required: {schedule['required_charge']} kWh, Available: {current_capacity} kWh.",
                )
                current_capacity -= schedule["required_charge"]
            else:
                # Remaining charge needed for this schedule
                remaining_charge = schedule["required_charge"] - current_capacity
                current_capacity = 0  # After this schedule, no charge is left

                # Determine next charging window
                next_schedule_start_hour = schedule["end"].hour
                if i + 1 < len(schedules):
                    next_schedule_start_hour = schedules[i + 1]["start"].hour

                if next_schedule_start_hour == schedule["end"].hour:
                    continue

                await async_logger(
                    self,
                    f"Finding the best time to charge {round(remaining_charge, 2)} kWh "
                    f"between {schedule['end'].hour}:00 and {next_schedule_start_hour}:00.",
                )
                await self._async_find_best_time_to_charge(
                    schedule["end"].hour,
                    next_schedule_start_hour,
                    remaining_charge,
                    schedule["min_price_diff"],
                    schedule["avg_import_price"],
                )

    async def _async_calculate_batteries_schedules(self):
        await async_logger(self, "Setting up batteries discharging schedules. ")

        if self._hsem_batteries_enable_batteries_schedule_1:
            start = datetime.strptime(
                str(self._hsem_batteries_enable_batteries_schedule_1_start), "%H:%M:%S"
            ).hour
            end = datetime.strptime(
                str(self._hsem_batteries_enable_batteries_schedule_1_end), "%H:%M:%S"
            ).hour

            if start > end:
                await async_logger(
                    self, "Invalid schedule 1. Start time is greater than end time. "
                )
            else:
                needed_batteries_capacity = 0.0
                needed_batteries_capacity_cost = 0.0
                avg_import_price = 0.0
                hours_count = 0

                for hour_start in range(start, end):
                    hour_end = (hour_start + 1) % 24
                    time_range = f"{hour_start:02d}-{hour_end:02d}"

                    if time_range in self._hourly_calculations:
                        self._hourly_calculations[time_range][
                            "recommendation"
                        ] = Recommendations.BatteriesDischargeMode.value
                        import_price = self._hourly_calculations[time_range][
                            "import_price"
                        ]
                        estimated_net_consumption = self._hourly_calculations[
                            time_range
                        ]["estimated_net_consumption"]

                        avg_import_price += import_price
                        needed_batteries_capacity += estimated_net_consumption
                        needed_batteries_capacity_cost += (
                            estimated_net_consumption * import_price
                        )
                        hours_count += 1

                self._hsem_batteries_enable_batteries_schedule_1_needed_batteries_capacity = (
                    needed_batteries_capacity
                )

                self._hsem_batteries_enable_batteries_schedule_1_needed_batteries_capacity_cost = (
                    needed_batteries_capacity_cost
                )
                self._hsem_batteries_enable_batteries_schedule_1_avg_import_price = (
                    avg_import_price / hours_count if hours_count > 0 else 0.0
                )

                await async_logger(
                    self,
                    f"Enabling batteries discharging schedule 1. "
                    f"Start: {convert_to_time(self._hsem_batteries_enable_batteries_schedule_1_start)}, "
                    f"End: {convert_to_time(self._hsem_batteries_enable_batteries_schedule_1_end)}, "
                    f"Average Import Price: {round(avg_import_price / hours_count, 2)} DKK, "
                    f"Needed Batteries Capacity: {round(needed_batteries_capacity, 2)} kWh, "
                    f"Needed Batteries Capacity Cost: {round(needed_batteries_capacity_cost, 2)} DKK, ",
                )

        if self._hsem_batteries_enable_batteries_schedule_2:
            start = datetime.strptime(
                str(self._hsem_batteries_enable_batteries_schedule_2_start), "%H:%M:%S"
            ).hour
            end = datetime.strptime(
                str(self._hsem_batteries_enable_batteries_schedule_2_end), "%H:%M:%S"
            ).hour

            if start > end:
                await async_logger(
                    self, "Invalid schedule 2. Start time is greater than end time. "
                )
            else:
                needed_batteries_capacity = 0.0
                needed_batteries_capacity_cost = 0.0
                avg_import_price = 0.0
                hours_count = 0

                for hour_start in range(start, end):
                    hour_end = (hour_start + 1) % 24
                    time_range = f"{hour_start:02d}-{hour_end:02d}"

                    if time_range in self._hourly_calculations:
                        self._hourly_calculations[time_range][
                            "recommendation"
                        ] = Recommendations.BatteriesDischargeMode.value
                        import_price = self._hourly_calculations[time_range][
                            "import_price"
                        ]
                        estimated_net_consumption = self._hourly_calculations[
                            time_range
                        ]["estimated_net_consumption"]

                        avg_import_price += import_price
                        needed_batteries_capacity += estimated_net_consumption
                        needed_batteries_capacity_cost += (
                            estimated_net_consumption * import_price
                        )
                        hours_count += 1

                self._hsem_batteries_enable_batteries_schedule_2_needed_batteries_capacity = (
                    needed_batteries_capacity
                )
                self._hsem_batteries_enable_batteries_schedule_2_needed_batteries_capacity_cost = (
                    needed_batteries_capacity_cost
                )
                self._hsem_batteries_enable_batteries_schedule_2_avg_import_price = (
                    avg_import_price / hours_count if hours_count > 0 else 0.0
                )

                await async_logger(
                    self,
                    "Enabling batteries discharging schedule 2. "
                    f"Start: {convert_to_time(self._hsem_batteries_enable_batteries_schedule_2_start)}, "
                    f"End: {convert_to_time(self._hsem_batteries_enable_batteries_schedule_2_end)}, "
                    f"Average Import Price: {round(avg_import_price / hours_count, 2)} DKK, "
                    f"Needed Batteries Capacity: {round(needed_batteries_capacity, 2)} kWh, "
                    f"Needed Batteries Capacity Cost: {round(needed_batteries_capacity_cost, 2)} DKK, ",
                )

        if self._hsem_batteries_enable_batteries_schedule_3:
            start = datetime.strptime(
                str(self._hsem_batteries_enable_batteries_schedule_3_start), "%H:%M:%S"
            ).hour
            end = datetime.strptime(
                str(self._hsem_batteries_enable_batteries_schedule_3_end), "%H:%M:%S"
            ).hour

            if start > end:
                await async_logger(
                    self, "Invalid schedule 3. Start time is greater than end time. "
                )
            else:
                needed_batteries_capacity = 0.0
                needed_batteries_capacity_cost = 0.0
                avg_import_price = 0.0
                hours_count = 0

                for hour_start in range(start, end):
                    hour_end = (hour_start + 1) % 24
                    time_range = f"{hour_start:02d}-{hour_end:02d}"

                    if time_range in self._hourly_calculations:
                        self._hourly_calculations[time_range][
                            "recommendation"
                        ] = Recommendations.BatteriesDischargeMode.value
                        import_price = self._hourly_calculations[time_range][
                            "import_price"
                        ]
                        estimated_net_consumption = self._hourly_calculations[
                            time_range
                        ]["estimated_net_consumption"]

                        avg_import_price += import_price
                        needed_batteries_capacity += estimated_net_consumption
                        needed_batteries_capacity_cost += (
                            estimated_net_consumption * import_price
                        )
                        hours_count += 1

                self._hsem_batteries_enable_batteries_schedule_3_needed_batteries_capacity = (
                    needed_batteries_capacity
                )
                self._hsem_batteries_enable_batteries_schedule_3_needed_batteries_capacity_cost = (
                    needed_batteries_capacity_cost
                )
                self._hsem_batteries_enable_batteries_schedule_3_avg_import_price = (
                    avg_import_price / hours_count if hours_count > 0 else 0.0
                )

                await async_logger(
                    self,
                    "Enabling batteries discharging schedule 3. "
                    f"Start: {convert_to_time(self._hsem_batteries_enable_batteries_schedule_3_start)}, "
                    f"End: {convert_to_time(self._hsem_batteries_enable_batteries_schedule_3_end)}, "
                    f"Average Import Price: {round(avg_import_price / hours_count, 2)} DKK, "
                    f"Needed Batteries Capacity: {round(needed_batteries_capacity, 2)} kWh, "
                    f"Needed Batteries Capacity Cost: {round(needed_batteries_capacity_cost, 2)} DKK, ",
                )

    async def _async_optimization_strategy(self):
        """Calculate the optimization strategy for each hour of the day."""

        now = datetime.now()
        current_month = now.month

        for hour, data in self._hourly_calculations.items():
            import_price = data["import_price"]
            export_price = data["export_price"]
            net_consumption = data["estimated_net_consumption"]

            if data["recommendation"] is not None:
                continue

            # Fully Fed to Grid
            if export_price > import_price:
                data["recommendation"] = Recommendations.FullyFedToGrid.value

            # Maximize Self Consumption
            elif net_consumption < 0:
                data["recommendation"] = Recommendations.MaximizeSelfConsumption.value

            else:
                if current_month in DEFAULT_HSEM_MONTHS_WINTER_SPRING:
                    data["recommendation"] = Recommendations.BatteriesWaitMode.value

                if current_month in DEFAULT_HSEM_MONTHS_SUMMER:
                    data["recommendation"] = (
                        Recommendations.MaximizeSelfConsumption.value
                    )

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

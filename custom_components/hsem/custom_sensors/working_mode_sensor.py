"""
This module defines the WorkingModeSensor class, which is a custom sensor entity for Home Assistant.
The sensor monitors various attributes related to solar energy production, battery status, and energy consumption,
and calculates the optimal working mode for the system.

Classes:
    WorkingModeSensor(SensorEntity, HSEMEntity): Represents a custom sensor entity for monitoring and optimizing
    solar energy production and consumption.
"""

import logging
import math
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import MATCH_ALL
from homeassistant.core import State
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)

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
    async_set_number_value,
    async_set_select_option,
    convert_to_boolean,
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

    # Exclude all attributes from recording except standard ones
    _unrecorded_attributes = frozenset([MATCH_ALL])

    def __init__(self, config_entry) -> None:
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
        self._hsem_energi_data_service_export_min_price = None
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
        self._hsem_ev_charger_force_max_discharge_power = None
        self._hsem_ev_charger_max_discharge_power = None
        self._hsem_house_consumption_power_state = 0.0
        self._hsem_solar_production_power_state = 0.0
        self._hsem_huawei_solar_inverter_active_power_control_state = None
        self._hsem_net_consumption = 0.0
        self._hsem_net_consumption_with_ev = 0.0
        self._hsem_huawei_solar_batteries_maximum_charging_power = None
        self._hsem_huawei_solar_batteries_maximum_charging_power_state = None
        self._hsem_huawei_solar_batteries_maximum_discharging_power = None
        self._hsem_huawei_solar_batteries_maximum_discharging_power_state = None
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
        self._hsem_batteries_rated_capacity_max_state = 0.0
        self._hourly_calculations = {
            f"{hour:02d}-{(hour + 1) % 24:02d}": {
                "avg_house_consumption": 0.0,
                "solcast_pv_estimate": 0.0,
                "estimated_net_consumption": None,
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

        self._missing_input_entities = True
        self._hsem_batteries_enable_batteries_schedule_1 = False
        self._hsem_batteries_enable_batteries_schedule_1_start = None
        self._hsem_batteries_enable_batteries_schedule_1_end = None
        self._hsem_batteries_enable_batteries_schedule_1_avg_import_price = 0.0
        self._hsem_batteries_enable_batteries_schedule_1_needed_batteries_capacity = 0.0
        self._hsem_batteries_enable_batteries_schedule_1_needed_batteries_capacity_cost = (
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
        self._hsem_force_working_mode = None
        self._hsem_force_working_mode_state = "auto"
        self._hsem_entity_id_cache = {}
        self._tracked_entities = set()
        self._timer = None
        self._attr_unique_id = get_working_mode_sensor_unique_id()
        self.entity_id = get_working_mode_sensor_entity_id()
        self._update_settings()

    def _update_settings(self) -> None:
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
        self._hsem_energi_data_service_export_min_price = convert_to_float(
            get_config_value(
                self._config_entry,
                "hsem_energi_data_service_export_min_price",
            )
        )
        self._hsem_huawei_solar_inverter_active_power_control = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_inverter_active_power_control",
        )
        self._hsem_house_power_includes_ev_charger_power = convert_to_boolean(
            get_config_value(
                self._config_entry,
                "hsem_house_power_includes_ev_charger_power",
            )
        )
        self._hsem_ev_charger_force_max_discharge_power = convert_to_boolean(
            get_config_value(
                self._config_entry,
                "hsem_ev_charger_force_max_discharge_power",
            )
        )
        self._hsem_ev_charger_max_discharge_power = convert_to_int(
            get_config_value(
                self._config_entry,
                "hsem_ev_charger_max_discharge_power",
            )
        )

        self._hsem_ev_charger_power = get_config_value(
            self._config_entry,
            "hsem_ev_charger_power",
        )

        if self._hsem_ev_charger_power == vol.UNDEFINED:
            self._hsem_ev_charger_power = None

        self._hsem_batteries_conversion_loss = convert_to_float(
            get_config_value(
                self._config_entry,
                "hsem_batteries_conversion_loss",
            )
        )

        self._hsem_huawei_solar_batteries_maximum_charging_power = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_batteries_maximum_charging_power",
        )
        self._hsem_huawei_solar_batteries_maximum_discharging_power = get_config_value(
            self._config_entry,
            "hsem_huawei_solar_batteries_maximum_discharging_power",
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
        self._hsem_house_consumption_energy_weight_1d = convert_to_int(
            get_config_value(
                self._config_entry, "hsem_house_consumption_energy_weight_1d"
            )
        )
        self._hsem_house_consumption_energy_weight_3d = convert_to_int(
            get_config_value(
                self._config_entry, "hsem_house_consumption_energy_weight_3d"
            )
        )
        self._hsem_house_consumption_energy_weight_7d = convert_to_int(
            get_config_value(
                self._config_entry, "hsem_house_consumption_energy_weight_7d"
            )
        )
        self._hsem_house_consumption_energy_weight_14d = convert_to_int(
            get_config_value(
                self._config_entry, "hsem_house_consumption_energy_weight_14d"
            )
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
    def name(self) -> str:
        return get_working_mode_sensor_name()

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    def state(self) -> str | None:
        return self._state

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""

        if self._missing_input_entities:
            return {
                "status": "error",
                "description": "Some of the required input sensors from the config flow is missing or not reporting a state yet. Check your configuration and make sure input sensors are configured correctly.",
                "force_working_mode_entity": self._hsem_force_working_mode,
                "force_working_mode_state": self._hsem_force_working_mode_state,
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
                "force_working_mode_entity": self._hsem_force_working_mode,
                "house_consumption_power_entity": self._hsem_house_consumption_power,
                "huawei_solar_batteries_grid_charge_cutoff_soc_entity": self._hsem_huawei_solar_batteries_grid_charge_cutoff_soc,
                "huawei_solar_batteries_maximum_charging_power_entity": self._hsem_huawei_solar_batteries_maximum_charging_power,
                "huawei_solar_batteries_maximum_discharging_power_entity": self._hsem_huawei_solar_batteries_maximum_discharging_power,
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
            "batteries_conversion_loss": self._hsem_batteries_conversion_loss,
            "batteries_current_capacity": self._hsem_batteries_current_capacity,
            "batteries_usable_capacity": self._hsem_batteries_usable_capacity,
            "energi_data_service_export_state": self._hsem_energi_data_service_export_state,
            "energi_data_service_import_state": self._hsem_energi_data_service_import_state,
            "energi_data_service_export_min_price": self._hsem_energi_data_service_export_min_price,
            "energy_needs": self._energy_needs,
            "ev_charger_power_state": self._hsem_ev_charger_power_state,
            "ev_charger_status_state": self._hsem_ev_charger_status_state,
            "ev_charger_max_discharge_power_state": self._hsem_ev_charger_max_discharge_power,
            "ev_charger_force_max_discharge_power": self._hsem_ev_charger_force_max_discharge_power,
            "force_working_mode_state": self._hsem_force_working_mode_state,
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
            "huawei_solar_batteries_maximum_discharging_power_state": self._hsem_huawei_solar_batteries_maximum_discharging_power_state,
            "huawei_solar_batteries_rated_capacity_max_state": self._hsem_batteries_rated_capacity_max_state,
            "huawei_solar_batteries_rated_capacity_min_state": self._hsem_batteries_rated_capacity_min_state,
            "huawei_solar_batteries_state_of_capacity_state": self._hsem_huawei_solar_batteries_state_of_capacity_state,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_periods": self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_periods,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_state": self._hsem_huawei_solar_batteries_tou_charging_and_discharging_periods_state,
            "huawei_solar_batteries_working_mode_state": self._hsem_huawei_solar_batteries_working_mode_state,
            "huawei_solar_inverter_active_power_control_state_state": self._hsem_huawei_solar_inverter_active_power_control_state,
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

    async def _async_handle_update(self, event) -> None:
        """Handle the sensor state update (for both manual and state change)."""

        await async_logger(self, "------ Updating working mode sensor state...")

        # Get the current time
        now = datetime.now()

        # Ensure config flow settings are reloaded if it changed.
        self._update_settings()

        # Fetch the latest entity states
        await self._async_fetch_entity_states()

        if (
            self._missing_input_entities
            and self._hsem_force_working_mode_state == "auto"
        ):
            self._state = Recommendations.MissingInputEntities.value
        else:
            if self._hsem_force_working_mode_state == "auto":
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
            if (
                last_changed_mode_seconds > 100
                or self._last_changed_mode is None
                or self._hsem_force_working_mode_state != "auto"
            ):
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
        self.async_write_ha_state()

    async def _async_calculate_remaining_battery_capacity(self) -> None:
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

    async def _async_fetch_entity_states(self) -> None:
        # Fetch the current value from the EV charger status sensor

        # Reset
        self._missing_input_entities = False

        if self._hsem_force_working_mode is None:
            self._hsem_force_working_mode = (
                await async_resolve_entity_id_from_unique_id(
                    self, "hsem_force_working_mode", "select"
                )
            )

        try:
            if self._hsem_force_working_mode:
                self._hsem_force_working_mode_state = ha_get_entity_state_and_convert(
                    self, self._hsem_force_working_mode, "string"
                )

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
                    convert_to_float(
                        ha_get_entity_state_and_convert(
                            self,
                            self._hsem_huawei_solar_batteries_state_of_capacity,
                            "float",
                            0,
                        )
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

            # Fetch the current value from the battery maximum discharging power sensor
            if self._hsem_huawei_solar_batteries_maximum_discharging_power:
                self._hsem_huawei_solar_batteries_maximum_discharging_power_state = (
                    ha_get_entity_state_and_convert(
                        self,
                        self._hsem_huawei_solar_batteries_maximum_discharging_power,
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
                self._hsem_batteries_rated_capacity_max_state = convert_to_float(
                    ha_get_entity_state_and_convert(
                        self, self._hsem_batteries_rated_capacity_max, "float", 0
                    )
                )
                self._hsem_batteries_rated_capacity_max_state = convert_to_float(
                    self._hsem_batteries_rated_capacity_max_state
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

        except Exception:
            self._missing_input_entities = True

        if self._hsem_ev_charger_status:
            if self._hsem_ev_charger_status not in self._tracked_entities:
                await async_logger(
                    self,
                    f"Starting to track state changes for {self._hsem_ev_charger_status}",
                )

                async_track_state_change_event(
                    self.hass,
                    [self._hsem_ev_charger_status],
                    self._async_handle_ev_charger_status_update,
                )
                self._tracked_entities.add(self._hsem_ev_charger_status)

        if self._hsem_force_working_mode:
            if self._hsem_force_working_mode not in self._tracked_entities:
                await async_logger(
                    self,
                    f"Starting to track state changes for {self._hsem_force_working_mode}",
                )

                async_track_state_change_event(
                    self.hass,
                    [self._hsem_force_working_mode],
                    self._async_handle_update,
                )
                self._tracked_entities.add(self._hsem_force_working_mode)

    async def _async_calculate_net_consumption(self) -> None:
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

            if self._hsem_house_power_includes_ev_charger_power:
                self._hsem_net_consumption_with_ev = (
                    self._hsem_house_consumption_power_state
                    - self._hsem_solar_production_power_state
                )
                self._hsem_net_consumption = (
                    self._hsem_house_consumption_power_state
                    - self._hsem_solar_production_power_state
                    - ev_charger_power_state
                )
            else:
                self._hsem_net_consumption_with_ev = (
                    self._hsem_house_consumption_power_state
                    - self._hsem_solar_production_power_state
                    + ev_charger_power_state
                )
                self._hsem_net_consumption = (
                    self._hsem_house_consumption_power_state
                    - self._hsem_solar_production_power_state
                )

            await async_logger(
                self,
                f"Net consumption calculated: {self._hsem_net_consumption}, with EV: {self._hsem_net_consumption_with_ev}, hsem_house_power_includes_ev_charger_power: {self._hsem_house_power_includes_ev_charger_power}, hsem_house_consumption_power_state: {self._hsem_house_consumption_power_state}, hsem_solar_production_power_state: {self._hsem_solar_production_power_state}, ev_charger_power_state: {ev_charger_power_state}",
            )
        else:
            self._hsem_net_consumption = 0.0

    async def _async_handle_ev_charger_status_update(self, event) -> None:
        """Handle state change event for EV charger status."""
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        if old_state is None or new_state is None:
            return  # Ignore invalid states

        old_value = ha_get_entity_state_and_convert(
            self, old_state.entity_id, "boolean"
        )

        new_value = ha_get_entity_state_and_convert(
            self, new_state.entity_id, "boolean"
        )

        if old_value != new_value:
            self._hsem_ev_charger_status_state = new_value
            await async_logger(
                self,
                f"EV charger status changed from {old_value} to {new_value}, triggering update.",
            )
            await self._async_handle_update(event)

    async def _async_set_inverter_power_control(self) -> None:
        # Determine the grid export power percentage based on the state

        if not isinstance(self._hsem_energi_data_service_export_state, (int, float)):
            return

        if not isinstance(
            self._hsem_energi_data_service_export_min_price, (int, float)
        ):
            return

        export_power_percentage = (
            100
            if self._hsem_energi_data_service_export_state
            > self._hsem_energi_data_service_export_min_price
            else 0
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

    async def _async_set_working_mode(self) -> None:

        # Determine the current month and hour
        now = datetime.now()
        current_month = now.month
        current_hour_start = now.hour
        current_hour_end = (current_hour_start + 1) % 24
        current_time_range = f"{current_hour_start:02d}-{current_hour_end:02d}"
        tou_modes = None
        state = None
        working_mode = None
        needed_batteries_capacity = round(
            await self._async_find_next_batteries_schedule_capacity(current_hour_start),
            2,
        )

        # Set maximum discharging power for batteries if EV charger is not active
        if not self._hsem_ev_charger_status_state:
            if (
                self._hsem_huawei_solar_batteries_maximum_discharging_power_state
                != 5000
            ):
                await async_set_number_value(
                    self,
                    self._hsem_huawei_solar_batteries_maximum_discharging_power,
                    5000,
                )

        # Determine the appropriate TOU modes and working mode state. In priority order:
        if (
            (
                isinstance(self._hsem_energi_data_service_import_state, (int, float))
                and self._hsem_energi_data_service_import_state < 0
                and self._hsem_force_working_mode_state == "auto"
            )
            or self._hsem_force_working_mode_state == Recommendations.ForceExport.value
        ):
            # Negative import price. Force charge battery
            tou_modes = DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.ForceExport.value
            await async_logger(
                self,
                f"# Recommendation for {current_time_range} is that Import price is negative. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}",
            )
            self._hourly_calculations[current_time_range][
                "recommendation"
            ] = Recommendations.ForceExport.value
        elif (
            (
                self._hourly_calculations.get(current_time_range, {}).get(
                    "recommendation"
                )
                == Recommendations.BatteriesChargeGrid.value
                and self._hsem_force_working_mode_state == "auto"
            )
            or self._hsem_force_working_mode_state
            == Recommendations.BatteriesChargeGrid.value
        ):
            tou_modes = DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.BatteriesChargeGrid.value
            await async_logger(
                self,
                f"# Recommendation for {current_time_range} is to force charge the batteries. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}",
            )
        elif (
            (
                self._hsem_ev_charger_status_state
                and self._hsem_force_working_mode_state == "auto"
            )
            or self._hsem_force_working_mode_state
            == Recommendations.EVSmartCharging.value
        ):
            if self._hsem_ev_charger_force_max_discharge_power:
                working_mode = WorkingModes.MaximizeSelfConsumption.value
                state = Recommendations.EVSmartCharging.value
                # Set maximum discharging power for batteries
                if (
                    self._hsem_huawei_solar_batteries_maximum_discharging_power_state
                    != self._hsem_ev_charger_max_discharge_power
                ):
                    await async_set_number_value(
                        self,
                        self._hsem_huawei_solar_batteries_maximum_discharging_power,
                        self._hsem_ev_charger_max_discharge_power,
                    )

            elif (
                self._hsem_ev_charger_power_state is not None
                and convert_to_int(self._hsem_ev_charger_power_state) > 0
                and not self._hsem_ev_charger_force_max_discharge_power
            ):
                working_mode = WorkingModes.MaximizeSelfConsumption.value
                state = Recommendations.EVSmartCharging.value
                house_power = convert_to_int(self._hsem_net_consumption)

                # Calculate discharge power to match house consumption and round up to nearest 150W
                batteries_discharge_power = convert_to_int(
                    150 * math.ceil(float(house_power) / 150)
                )

                if batteries_discharge_power < 50:
                    batteries_discharge_power = 50

                # Set maximum discharging power for batteries
                if (
                    self._hsem_huawei_solar_batteries_maximum_discharging_power_state
                    != batteries_discharge_power
                ):
                    await async_set_number_value(
                        self,
                        self._hsem_huawei_solar_batteries_maximum_discharging_power,
                        batteries_discharge_power,
                    )
            else:
                # EV Charger is active. Disable battery discharge
                tou_modes = DEFAULT_HSEM_EV_CHARGER_TOU_MODES
                working_mode = WorkingModes.TimeOfUse.value
                state = Recommendations.EVSmartCharging.value

            await async_logger(
                self,
                f"# Recommendation for {current_time_range} is EV Charger is active. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}, House Power: {house_power}, Batteries Discharge Power: {batteries_discharge_power}",
            )
            self._hourly_calculations[current_time_range][
                "recommendation"
            ] = Recommendations.EVSmartCharging.value
        elif (
            (
                self._hsem_batteries_current_capacity > needed_batteries_capacity
                and needed_batteries_capacity > 0
                and self._hsem_force_working_mode_state == "auto"
            )
            or self._hsem_force_working_mode_state
            == Recommendations.BatteriesDischargeMode.value
        ):
            working_mode = WorkingModes.MaximizeSelfConsumption.value
            state = Recommendations.BatteriesDischargeMode.value
            await async_logger(
                self,
                f"# Recommendation for {current_time_range} is more batteries capacity that needed for battery schedule.. Working Mode: {working_mode} and recommended state: {state}. Needed Batteries Capacity: {needed_batteries_capacity}, Current Batteries Capacity: {self._hsem_batteries_current_capacity}",
            )
            self._hourly_calculations[current_time_range][
                "recommendation"
            ] = Recommendations.BatteriesDischargeMode.value
        elif (
            (
                self._hourly_calculations.get(current_time_range, {}).get(
                    "recommendation"
                )
                == Recommendations.ForceBatteriesDischarge.value
                and self._hsem_force_working_mode_state == "auto"
            )
            or self._hsem_force_working_mode_state
            == Recommendations.ForceBatteriesDischarge.value
        ):
            tou_modes = DEFAULT_HSEM_TOU_MODES_FORCE_DISCHARGE
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.ForceBatteriesDischarge.value
            await async_logger(
                self,
                f"# Recommendation for {current_time_range} is to force discharge the batteries. Setting TOU Periods: {tou_modes} and Working Mode: {working_mode}",
            )
        elif (
            self._hsem_net_consumption < 0
            and self._hsem_force_working_mode_state == "auto"
        ):
            # Positive net consumption. Charge battery from Solar
            working_mode = WorkingModes.MaximizeSelfConsumption.value
            state = Recommendations.BatteriesChargeSolar.value
            await async_logger(
                self,
                f"# Recommendation for {current_time_range} is due to positive net consumption. Working Mode: {working_mode}, Solar Production: {self._hsem_solar_production_power_state}, House Consumption: {self._hsem_house_consumption_power_state}, Net Consumption: {self._hsem_net_consumption}",
            )
            self._hourly_calculations[current_time_range][
                "recommendation"
            ] = Recommendations.BatteriesChargeSolar.value
        elif (
            (
                self._hourly_calculations.get(current_time_range, {}).get(
                    "recommendation"
                )
                == Recommendations.BatteriesChargeSolar.value
                and self._hsem_force_working_mode_state == "auto"
            )
            or self._hsem_force_working_mode_state
            == Recommendations.BatteriesChargeSolar.value
        ):
            working_mode = WorkingModes.MaximizeSelfConsumption.value
            state = Recommendations.BatteriesChargeSolar.value
            await async_logger(
                self,
                f"# Recommendation for {current_time_range} is to set working mode to Maximize Self Consumption to charge batteries from solar",
            )
        elif (
            (
                self._hourly_calculations.get(current_time_range, {}).get(
                    "recommendation"
                )
                == Recommendations.BatteriesDischargeMode.value
                and self._hsem_force_working_mode_state == "auto"
            )
            or self._hsem_force_working_mode_state
            == Recommendations.BatteriesDischargeMode.value
        ):
            working_mode = WorkingModes.MaximizeSelfConsumption.value
            state = Recommendations.BatteriesDischargeMode.value
            await async_logger(
                self,
                f"# Recommendation for {current_time_range} is to set working mode to Maximize Self Consumption to enable batteries discharge to cover load",
            )
        elif (
            (
                self._hourly_calculations.get(current_time_range, {}).get(
                    "recommendation"
                )
                == Recommendations.BatteriesWaitMode.value
                and self._hsem_force_working_mode_state == "auto"
            )
            or self._hsem_force_working_mode_state
            == Recommendations.BatteriesWaitMode.value
        ):
            # Winter/Spring settings
            # if current_month in DEFAULT_HSEM_MONTHS_WINTER_SPRING:
            tou_modes = DEFAULT_HSEM_BATTERIES_WAIT_MODE
            working_mode = WorkingModes.TimeOfUse.value
            state = Recommendations.TimeOfUse.value
            _LOGGER.debug(
                f"Default winter/spring settings. TOU Periods: {tou_modes} and Working Mode: {working_mode}"
            )

            # Summer settings
            # if current_month in DEFAULT_HSEM_MONTHS_SUMMER:
            #    working_mode = WorkingModes.MaximizeSelfConsumption.value
            #    state = Recommendations.BatteriesChargeSolar.value
            #    _LOGGER.debug(f"Default summer settings. Working Mode: {working_mode}")

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

    async def _async_reset_recommendations(self) -> None:
        """Reset the recommendations for each hour of the day."""
        self._hourly_calculations = {
            f"{hour:02d}-{(hour + 1) % 24:02d}": {
                "avg_house_consumption": 0.0,
                "solcast_pv_estimate": 0.0,
                "estimated_net_consumption": None,
                "estimated_cost": 0.0,
                "batteries_charged": 0.0,
                "import_price": 0.0,
                "export_price": 0.0,
                "recommendation": None,
            }
            for hour in range(24)
        }

    async def _async_calculate_hourly_data(self) -> None:
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
                    value_1d = convert_to_float(
                        ha_get_entity_state_and_convert(self, entity_id_1d, "float", 3)
                    )
                if entity_id_3d is not None:
                    value_3d = convert_to_float(
                        ha_get_entity_state_and_convert(self, entity_id_3d, "float", 3)
                    )

                if entity_id_7d is not None:
                    value_7d = convert_to_float(
                        ha_get_entity_state_and_convert(self, entity_id_7d, "float", 3)
                    )

                if entity_id_14d is not None:
                    value_14d = convert_to_float(
                        ha_get_entity_state_and_convert(self, entity_id_14d, "float", 3)
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

    async def _async_calculate_solcast_forecast(self) -> None:
        """Calculate the hourly Solcast PV estimate and update self._hourly_calculations without resetting avg_house_consumption."""
        if not self._hsem_solcast_pv_forecast_forecast_today:
            return

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

    async def _async_calculate_hourly_import_price(self) -> None:
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
                    is not None
                    and self._hourly_calculations[time_range][
                        "estimated_net_consumption"
                    ]
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

    async def _async_calculate_hourly_export_price(self) -> None:
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
                    is not None
                    and self._hourly_calculations[time_range][
                        "estimated_net_consumption"
                    ]
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

    async def _async_calculate_hourly_net_consumption(self) -> None:
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
        required_charge=0.0,
        min_price_diff=0.0,
        avg_import_price=0.0,
    ) -> None:
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
                self._hourly_calculations[time_range]["batteries_charged"] = round(
                    energy_to_charge, 3
                )
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
                    self._hourly_calculations[time_range]["batteries_charged"] = round(
                        energy_to_charge, 3
                    )
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
        min_price_check = True

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

            if min_price_diff != 0 and avg_charge_diff < min_price_diff:
                min_price_check = False

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
                    self._hourly_calculations[time_range]["batteries_charged"] = round(
                        energy_to_charge, 3
                    )
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

    async def _async_calculate_batteries_schedules_best_charge_time(self) -> None:
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

        if self._hsem_huawei_solar_batteries_state_of_capacity_state == 100:
            await async_logger(
                self,
                "Skipping charge as the batteries are already at 100% capacity. ",
            )
            return

        if (
            self._hsem_batteries_current_capacity
            == self._hsem_batteries_usable_capacity
        ):
            await async_logger(
                self,
                "Skipping charge as the batteries are already at capacity. ",
            )
            return

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
                "Skipping charge as the batteries already has sufficient capacity. ",
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

    async def _async_calculate_batteries_schedules(self) -> None:
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

                        if estimated_net_consumption is None:
                            estimated_net_consumption = 0.0

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

                        if estimated_net_consumption is None:
                            estimated_net_consumption = 0.0

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

                        if estimated_net_consumption is None:
                            estimated_net_consumption = 0.0

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

    async def _async_optimization_strategy(self) -> None:
        """Calculate the optimization strategy for each hour of the day."""

        now = datetime.now()
        current_month = now.month

        await async_logger(
            self, "Starting optimization strategy for all remaining hours."
        )

        for hour, data in self._hourly_calculations.items():
            import_price = data["import_price"]
            export_price = data["export_price"]
            net_consumption = data["estimated_net_consumption"]

            # Fully Fed to Grid due to export price being higher than import price
            if export_price > import_price:
                data["recommendation"] = Recommendations.FullyFedToGrid.value
                await async_logger(
                    self,
                    f"Hour: {hour} | Recommendation set to FullyFedToGrid (export price > import price).",
                )

        # Calculate when to charge the battery to the full from solar
        batteries_needed_charge = (
            self._hsem_batteries_usable_capacity - self._hsem_batteries_current_capacity
        )

        if batteries_needed_charge < 0.0:
            batteries_needed_charge = 0.0

        await async_logger(
            self,
            f"Batteries needed charge: {batteries_needed_charge} kWh | Current capacity: {self._hsem_batteries_current_capacity} kWh | Usable capacity: {self._hsem_batteries_usable_capacity} kWh",
        )

        charged = 0.0

        # Loop through hourly_calculations sorted by import_price ascending to charge batteries from solar while import price is highest
        sorted_hours = sorted(
            self._hourly_calculations.items(),
            key=lambda item: item[1].get("import_price", 0),
            reverse=False,
        )
        for hour, data in sorted_hours:
            if charged >= batteries_needed_charge:
                break

            if data["recommendation"] is not None:
                continue

            net_consumption = data["estimated_net_consumption"]

            # Negative net consumption means we have solar surplus to charge batteries while covering the house
            if net_consumption is not None and net_consumption < 0:
                charged += net_consumption
                data["recommendation"] = Recommendations.BatteriesChargeSolar.value
                await async_logger(
                    self,
                    f"Hour: {hour} | Charging from solar surplus. Net Consumption: {net_consumption} | Total charged: {charged} kWh.",
                )

        # So did we charge the battery totally?
        fully_charged_battery = charged >= batteries_needed_charge

        await async_logger(
            self, f"Fully charged battery status: {fully_charged_battery}"
        )

        # Battery is maybe fully charged. Lets just make the battery wait now or charge from solar!?
        for hour, data in self._hourly_calculations.items():
            if data["recommendation"] is not None:
                continue

            # The battery is already fully exported. Force export the solar power
            if fully_charged_battery:
                data["recommendation"] = Recommendations.BatteriesWaitMode.value
                await async_logger(
                    self,
                    f"Hour: {hour} | Battery fully charged, setting recommendation to BatteriesWaitMode.",
                )
            else:
                if current_month in DEFAULT_HSEM_MONTHS_WINTER_SPRING:
                    data["recommendation"] = Recommendations.BatteriesWaitMode.value
                    await async_logger(
                        self,
                        f"Hour: {hour} | Winter/Spring: setting recommendation to BatteriesWaitMode.",
                    )

                if current_month in DEFAULT_HSEM_MONTHS_SUMMER:
                    data["recommendation"] = Recommendations.BatteriesChargeSolar.value
                    await async_logger(
                        self,
                        f"Hour: {hour} | Summer: setting recommendation to BatteriesChargeSolar.",
                    )

        await async_logger(
            self, "Completed optimization strategy for all remaining hours."
        )

    async def _async_find_next_batteries_schedule_capacity(
        self, current_hour: int
    ) -> float:
        """
        Find the total required battery capacity for all upcoming active schedules.

        Args:
            current_hour (int): The current hour of the day.

        Returns:
            float: The total needed battery capacity (kWh) for all upcoming schedules.
        """
        schedules = []

        if self._hsem_batteries_enable_batteries_schedule_1:
            start_time = convert_to_time(
                self._hsem_batteries_enable_batteries_schedule_1_start
            )
            if start_time is not None:
                schedules.append(
                    {
                        "start": start_time.hour,
                        "needed_capacity": self._hsem_batteries_enable_batteries_schedule_1_needed_batteries_capacity,
                    }
                )
        if self._hsem_batteries_enable_batteries_schedule_2:
            start_time = convert_to_time(
                self._hsem_batteries_enable_batteries_schedule_2_start
            )
            if start_time is not None:
                schedules.append(
                    {
                        "start": start_time.hour,
                        "needed_capacity": self._hsem_batteries_enable_batteries_schedule_2_needed_batteries_capacity,
                    }
                )
        if self._hsem_batteries_enable_batteries_schedule_3:
            start_time = convert_to_time(
                self._hsem_batteries_enable_batteries_schedule_3_start
            )
            if start_time is not None:
                schedules.append(
                    {
                        "start": start_time.hour,
                        "needed_capacity": self._hsem_batteries_enable_batteries_schedule_3_needed_batteries_capacity,
                    }
                )

        # Filter schedules that start after the current hour
        upcoming_schedules = [s for s in schedules if s["start"] > current_hour]

        if not upcoming_schedules:
            return 0.0  # No upcoming schedules, return 0

        # Sum up the needed capacity for all upcoming schedules
        total_needed_capacity = sum(s["needed_capacity"] for s in upcoming_schedules)

        return total_needed_capacity

    async def _async_calculate_energy_needs(self) -> None:
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
        self._energy_needs = {}
        for label, hours in time_ranges.items():
            total_consumption = 0.0
            for hour in hours:
                if hour in self._hourly_calculations:
                    net_consumption = self._hourly_calculations[hour].get(
                        "estimated_net_consumption"
                    )
                    if net_consumption is not None:
                        total_consumption += net_consumption

            self._energy_needs[label] = round(total_consumption, 2)

    async def async_update(self, event=None) -> None:
        """Manually trigger the sensor update."""
        await self._async_handle_update(None)

    async def async_options_updated(self, config_entry) -> None:
        """Handle options update from configuration change."""
        self._update_settings()

        await self._async_handle_update(None)

    # Function to update timer interval
    async def _async_update_timer_interval(self) -> None:
        # Cancel existing timer if it exists
        if self._timer is not None:
            self._timer()

        # Set interval based on missing_input_entities
        interval = (
            timedelta(seconds=5)
            if self._missing_input_entities
            else timedelta(minutes=self._update_interval)
        )

        # Register new timer and store reference
        self._timer = async_track_time_interval(
            self.hass, self._async_handle_update, interval
        )

        await async_logger(
            self,
            f"Update interval set to: {interval} (missing_input_entities: {self._missing_input_entities})",
        )

    async def async_added_to_hass(self) -> None:
        """Handle the sensor being added to Home Assistant."""
        # Keep track of the registered timer

        # Initial update
        await self._async_handle_update(None)

        # Setup timer
        await self._async_update_timer_interval()

        # Schedule updates at the start of every hour
        async_track_time_change(
            self.hass,
            self._async_handle_update,
            hour="*",
            minute=0,
            second=10,
        )

        # Wrap the original handle_update to update timer interval after each update
        original_handle_update = self._async_handle_update

        async def wrapped_handle_update(event):
            await original_handle_update(event)
            await self._async_update_timer_interval()

        self._async_handle_update = wrapped_handle_update

        await super().async_added_to_hass()

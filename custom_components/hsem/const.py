"""
This module defines constants used in the Huawei Solar Energy Management (HSEM) integration.
"""

import voluptuous as vol

DOMAIN = "hsem"  # Domain name for the integration
NAME = "Huawei Solar Energy Management"  # Display name for the integration

# Default TOU modes for EV charger when charging
DEFAULT_HSEM_EV_CHARGER_TOU_MODES = ["00:00-00:01/1234567/+"]

# Default TOU modes for letting the battery wait
DEFAULT_HSEM_BATTERIES_WAIT_MODE = ["00:00-00:01/1234567/+"]

# TOU mode for force charging the battery
DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE = ["00:00-23:59/1234567/+"]

# TOU mode for force dicharging the battery
DEFAULT_HSEM_TOU_MODES_FORCE_DISCHARGE = ["00:00-23:59/1234567/-"]

# Default list of months considered winter and spring
DEFAULT_HSEM_MONTHS_WINTER_SPRING = [1, 2, 3, 4, 9, 10, 11, 12]

# Default list of months considered summer
DEFAULT_HSEM_MONTHS_SUMMER = [5, 6, 7, 8]

# Minimum required version of Huawei Solar
MIN_HUAWEI_SOLAR_VERSION = "1.5.0a1"

DEFAULT_CONFIG_VALUES = {
    "device_name": NAME,
    "hsem_batteries_conversion_loss": 10,
    "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
    "hsem_batteries_enable_batteries_schedule_1_min_price_difference": 0.00,
    "hsem_batteries_enable_batteries_schedule_1_start": "07:00:00",
    "hsem_batteries_enable_batteries_schedule_1": True,
    "hsem_batteries_enable_batteries_schedule_2_end": "21:00:00",
    "hsem_batteries_enable_batteries_schedule_2_min_price_difference": 0.00,
    "hsem_batteries_enable_batteries_schedule_2_start": "17:00:00",
    "hsem_batteries_enable_batteries_schedule_2": True,
    "hsem_batteries_enable_batteries_schedule_3_end": "00:00:00",
    "hsem_batteries_enable_batteries_schedule_3_min_price_difference": 0.00,
    "hsem_batteries_enable_batteries_schedule_3_start": "00:00:00",
    "hsem_batteries_enable_batteries_schedule_3": False,
    "hsem_energi_data_service_export_min_price": -0.00,
    "hsem_energi_data_service_export": "sensor.energi_data_service_produktion",
    "hsem_energi_data_service_import": "sensor.energi_data_service",
    "hsem_ev_charger_force_max_discharge_power": False,
    "hsem_ev_charger_max_discharge_power": 0,
    "hsem_ev_charger_power": vol.UNDEFINED,
    "hsem_ev_charger_status": vol.UNDEFINED,
    "hsem_extended_attributes": False,
    "hsem_house_consumption_energy_weight_14d": 10,
    "hsem_house_consumption_energy_weight_1d": 25,
    "hsem_house_consumption_energy_weight_3d": 50,
    "hsem_house_consumption_energy_weight_7d": 15,
    "hsem_house_consumption_power": "sensor.power_house_load",
    "hsem_house_power_includes_ev_charger_power": True,
    "hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou": "select.batteries_excess_pv_energy_use_in_tou",
    "hsem_huawei_solar_batteries_grid_charge_cutoff_soc": "number.batteries_grid_charge_cutoff_soc",
    "hsem_huawei_solar_batteries_maximum_charging_power": "number.batteries_maximum_charging_power",
    "hsem_huawei_solar_batteries_maximum_discharging_power": "number.batteries_maximum_discharging_power",
    "hsem_huawei_solar_batteries_rated_capacity": "sensor.batteries_rated_capacity",
    "hsem_huawei_solar_batteries_state_of_capacity": "sensor.batteries_state_of_capacity",
    "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods": "sensor.batteries_tou_charging_and_discharging_periods",
    "hsem_huawei_solar_batteries_working_mode": "select.batteries_working_mode",
    "hsem_huawei_solar_device_id_batteries": vol.UNDEFINED,
    "hsem_huawei_solar_device_id_inverter_1": vol.UNDEFINED,
    "hsem_huawei_solar_device_id_inverter_2": vol.UNDEFINED,
    "hsem_huawei_solar_inverter_active_power_control": "sensor.inverter_active_power_control",
    "hsem_read_only": False,
    "hsem_solar_production_power": "sensor.power_inverter_input_total",
    "hsem_solcast_pv_forecast_forecast_today": "sensor.solcast_pv_forecast_forecast_today",
    "hsem_solcast_pv_forecast_forecast_tomorrow": "sensor.solcast_pv_forecast_forecast_tomorrow",
    "hsem_solcast_pv_forecast_forecast_likelihood": "pv_estimate",
    "hsem_update_interval": 5,
    "hsem_verbose_logging": False,
}

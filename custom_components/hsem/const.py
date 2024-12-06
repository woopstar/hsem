"""
This module defines constants used in the Huawei Solar Energy Management (HSEM) integration.
"""

import voluptuous as vol

DOMAIN = "hsem"  # Domain name for the integration
NAME = "Huawei Solar Energy Management"  # Display name for the integration

# Default TOU modes for EV charger when charging
DEFAULT_HSEM_EV_CHARGER_TOU_MODES = ["00:00-00:01/1234567/+"]

# Default TOU modes for solar energy consumption throughout the day
DEFAULT_HSEM_DEFAULT_TOU_MODES = [
    # "00:01-05:59/1234567/+",
    "06:00-10:00/1234567/-",
    # "15:00-16:59/1234567/+",
    "17:00-23:59/1234567/-",
]

# TOU mode for force charging the battery
DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE = ["00:00-23:59/1234567/+"]

# TOU mode for force dicharging the battery
DEFAULT_HSEM_TOU_MODES_FORCE_DISCHARGE = ["00:00-23:59/1234567/-"]

# Default list of months considered winter and spring
DEFAULT_HSEM_MONTHS_WINTER_SPRING = [1, 2, 3, 4, 9, 10, 11, 12]

# Default list of months considered summer
DEFAULT_HSEM_MONTHS_SUMMER = [5, 6, 7, 8]

DEFAULT_CONFIG_VALUES = {
    "device_name": NAME,
    "hsem_update_interval": 5,
    "hsem_batteries_conversion_loss": 10,
    "hsem_batteries_enable_charge_hours_day_end": "17:00:00",
    "hsem_batteries_enable_charge_hours_day_start": "12:00:00",
    "hsem_batteries_enable_charge_hours_day": True,
    "hsem_batteries_enable_charge_hours_night_end": "06:00:00",
    "hsem_batteries_enable_charge_hours_night_start": "00:00:00",
    "hsem_batteries_enable_charge_hours_night": True,
    "hsem_energi_data_service_export": "sensor.energi_data_service_produktion",
    "hsem_energi_data_service_import": "sensor.energi_data_service",
    "hsem_ev_charger_power": vol.UNDEFINED,
    "hsem_ev_charger_status": vol.UNDEFINED,
    "hsem_house_consumption_energy_weight_14d": 10,
    "hsem_house_consumption_energy_weight_1d": 30,
    "hsem_house_consumption_energy_weight_3d": 40,
    "hsem_house_consumption_energy_weight_7d": 20,
    "hsem_house_consumption_power": "sensor.power_house_load",
    "hsem_house_power_includes_ev_charger_power": True,
    "hsem_huawei_solar_batteries_grid_charge_cutoff_soc": "number.batteries_grid_charge_cutoff_soc",
    "hsem_huawei_solar_batteries_maximum_charging_power": "number.batteries_maximum_charging_power",
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
}

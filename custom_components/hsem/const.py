"""
This module defines constants used in the Huawei Solar Energy Management (HSEM) integration.

Constants:
    DOMAIN (str): Domain name for the integration.
    NAME (str): Display name for the integration.

    DEFAULT_HSEM_ENERGI_DATA_SERVICE_IMPORT (str): Default sensor entity ID for energy data service import.
    DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT (str): Default sensor entity ID for energy data service export.
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_WORKING_MODE (str): Default select entity ID for solar battery working mode.
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_STATE_OF_CAPACITY (str): Default sensor entity ID for battery state of capacity.
    DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL (str): Default sensor entity ID for inverter active power control.
    DEFAULT_HSEM_HOUSE_CONSUMPTION_POWER (str): Default sensor entity ID for house power consumption.
    DEFAULT_HSEM_SOLAR_PRODUCTION_POWER (str): Default sensor entity ID for total solar production power.
    DEFAULT_HSEM_SOLCAST_PV_FORECAST_FORECAST_TODAY (str): Default sensor entity ID for today’s solar PV forecast.
    DEFAULT_HSEM_SOLCAST_PV_FORECAST_FORECAST_TOMORROW (str): Default sensor entity ID for tomorrow’s solar PV forecast.
    DEFAULT_HSEM_MORNING_ENERGY_NEED (float): Default morning energy need in kWh.
    DEFAULT_HSEM_BATTERY_MAX_CAPACITY (float): Default battery maximum capacity in kWh.
    DEFAULT_HSEM_EV_CHARGER_STATUS (str): Default sensor entity ID for EV charger status.
    DEFAULT_HSEM_EV_CHARGER_TOU_MODES (list): Default TOU modes for EV charger when charging.
    DEFAULT_HSEM_DEFAULT_TOU_MODES (list): Default TOU modes for solar energy consumption throughout the day.
    DEFAULT_HSEM_MONTHS_WINTER_SPRING (list): Default list of months considered winter and spring.
    DEFAULT_HSEM_MONTHS_SUMMER (list): Default list of months considered summer.
    HOUSE_CONSUMPTION_ENERGY_WEIGHT_3D (float): Weighting factor for the 3-day average of house consumption energy.
    HOUSE_CONSUMPTION_ENERGY_WEIGHT_7D (float): Weighting factor for the 7-day average of house consumption energy.
    HOUSE_CONSUMPTION_ENERGY_WEIGHT_14D (float): Weighting factor for the 14-day average of house consumption energy.
    DEFAULT_HSEM_HOUSE_POWER_INCLUDES_EV_CHARGER_POWER (bool): Default for house power includes EV charger power.
    DEFAULT_HSEM_EV_CHARGER_POWER (str): Default sensor entity ID for EV charger power.
    DEFAULT_HSEM_BATTERY_CONVERSION_LOSS (int): Default conversion loss for battery charging.
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_MAXIMUM_CHARGING_POWER (str): Default sensor entity ID for battery charging power.
"""

DOMAIN = "hsem"  # Domain name for the integration
NAME = "Huawei Solar Energy Management"  # Display name for the integration

# Default sensor entity ID for energy data service import
DEFAULT_HSEM_ENERGI_DATA_SERVICE_IMPORT = "sensor.energi_data_service"

# Default sensor entity ID for energy data service export
DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT = "sensor.energi_data_service_produktion"

# Default select entity ID for solar battery working mode
DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_WORKING_MODE = "select.batteries_working_mode"

# Default sensor entity ID for battery state of capacity
DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_STATE_OF_CAPACITY = (
    "sensor.batteries_state_of_capacity"
)

# Default sensor entity ID for inverter active power control
DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL = (
    "sensor.inverter_active_power_control"
)

# Default sensor entity ID for house power consumption
DEFAULT_HSEM_HOUSE_CONSUMPTION_POWER = "sensor.power_house_load"

# Default sensor entity ID for total solar production power
DEFAULT_HSEM_SOLAR_PRODUCTION_POWER = "sensor.power_inverter_input_total"

# Default sensor entity ID for today’s solar PV forecast
DEFAULT_HSEM_SOLCAST_PV_FORECAST_FORECAST_TODAY = (
    "sensor.solcast_pv_forecast_forecast_today"
)

# Default sensor entity ID for tomorrow’s solar PV forecast
DEFAULT_HSEM_SOLCAST_PV_FORECAST_FORECAST_TOMORROW = (
    "sensor.solcast_pv_forecast_forecast_tomorrow"
)

# Default morning energy need in kWh
DEFAULT_HSEM_MORNING_ENERGY_NEED = 1.5

# Default battery maximum capacity in kWh
DEFAULT_HSEM_BATTERY_MAX_CAPACITY = 10

# Default sensor entity ID for EV charger status
DEFAULT_HSEM_EV_CHARGER_STATUS = ""

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

# Weighting factors for calculating the weighted average of house consumption energy over different time periods
HOUSE_CONSUMPTION_ENERGY_WEIGHT_1D = 0.3  # 30% weight for the 1-day average
HOUSE_CONSUMPTION_ENERGY_WEIGHT_3D = 0.4  # 40% weight for the 3-day average
HOUSE_CONSUMPTION_ENERGY_WEIGHT_7D = 0.2  # 20% weight for the 7-day average
HOUSE_CONSUMPTION_ENERGY_WEIGHT_14D = 0.1  # 10% weight for the 14-day average

# Default for house power includes EV charger power
DEFAULT_HSEM_HOUSE_POWER_INCLUDES_EV_CHARGER_POWER = True

# Default sensor entity ID for EV charger power
DEFAULT_HSEM_EV_CHARGER_POWER = ""

# Default conversion loss for battery charging in pct
DEFAULT_HSEM_BATTERY_CONVERSION_LOSS = 10

# Default sensor entity ID for battery charging power
DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_MAXIMUM_CHARGING_POWER = (
    "number.batteries_maximum_charging_power"
)

# Default sensor entity ID for for batteries grid charge cutoff SOC
DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_GRID_CHARGE_CUTOFF_SOC = (
    "number.batteries_grid_charge_cutoff_soc"
)

# Default sensor entity ID for for batteries charging and discahrging periods
DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_TOU_CHARGING_AND_DISCHARGING_PERIODS = (
    "sensor.batteries_tou_charging_and_discharging_periods"
)

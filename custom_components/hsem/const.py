DOMAIN = "hsem"  # Domain name for the integration
NAME = "Huawei Solar Energy Management"  # Display name for the integration
ICON = "mdi:power"  # Default icon for the integration's sensors

# Default sensor entity ID for energy data service import
DEFAULT_HSEM_ENERGI_DATA_SERVICE_IMPORT = "sensor.energi_data_service"

# Default sensor entity ID for energy data service export
DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT = "sensor.energi_data_service_produktion"

# Default select entity ID for solar battery working mode
DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_WORKING_MODE = "select.battery_working_mode"

# Default sensor entity ID for battery state of capacity
DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_STATE_OF_CAPACITY = (
    "sensor.battery_state_of_capacity"
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
DEFAULT_HSEM_EV_CHARGER_STATUS = "sensor.go_echarger_is_charging"

# Default Time-Of-Use (TOU) modes for import sensor when enabled
DEFAULT_HSEM_IMPORT_SENSOR_TOU_MODES = ["00:00-23:59/1234567/+"]

# Default TOU modes for EV charger when charging
DEFAULT_HSEM_EV_CHARGER_TOU_MODES = ["00:00-00:01/1234567/+"]

# Default TOU modes for solar energy consumption throughout the day
DEFAULT_HSEM_DEFAULT_TOU_MODES = [
    "00:01-05:59/1234567/+",
    "06:00-10:00/1234567/-",
    "17:00-23:59/1234567/-",
]

# Default list of months considered winter and spring
DEFAULT_HSEM_MONTHS_WINTER_SPRING = [1, 2, 3, 4, 9, 10, 11, 12]

# Default list of months considered summer
DEFAULT_HSEM_MONTHS_SUMMER = [5, 6, 7, 8]

# Weighting factors for calculating the weighted average of house consumption energy over different time periods
HOUSE_CONSUMPTION_ENERGY_WEIGHT_3D = 0.6  # 60% weight for the 3-day average
HOUSE_CONSUMPTION_ENERGY_WEIGHT_7D = 0.3  # 30% weight for the 7-day average
HOUSE_CONSUMPTION_ENERGY_WEIGHT_14D = 0.1  # 10% weight for the 14-day average

# Default for house power includes EV charger power
DEFAULT_HSEM_HOUSE_POWER_INCLUDES_EV_CHARGER_POWER = True

# Default sensor entity ID for EV charger power
DEFAULT_HSEM_EV_CHARGER_POWER = "sensor.go_echarger_222819_nrg_12"

# Default conversion loss for battery charging
DEFAULT_HSEM_BATTERY_CONVERSION_LOSS = 10

# Default sensor entity ID for battery charging power
DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_MAXIMUM_CHARGING_POWER = (
    "number.battery_maximum_charging_power"
)

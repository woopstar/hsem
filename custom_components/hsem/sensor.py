import logging

from .const import DOMAIN
from .custom_sensors.house_consumption_energy_average_sensor import (
    HouseConsumptionEnergyAverageSensor,
)
from .custom_sensors.house_consumption_energy_sensor import HouseConsumptionEnergySensor
from .custom_sensors.house_consumption_power_sensor import HouseConsumptionPowerSensor
from .custom_sensors.working_mode_sensor import WorkingModeSensor
from .utils.misc import get_config_value

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up HSEM sensors from a config entry."""

    # Extract configuration parameters
    hsem_huawei_solar_device_id_inverter_1 = get_config_value(
        config_entry, "hsem_huawei_solar_device_id_inverter_1"
    )
    hsem_huawei_solar_device_id_inverter_2 = get_config_value(
        config_entry, "hsem_huawei_solar_device_id_inverter_2"
    )
    hsem_huawei_solar_device_id_batteries = get_config_value(
        config_entry, "hsem_huawei_solar_device_id_batteries"
    )
    hsem_huawei_solar_batteries_working_mode = get_config_value(
        config_entry, "hsem_huawei_solar_batteries_working_mode"
    )
    hsem_huawei_solar_batteries_state_of_capacity = get_config_value(
        config_entry, "hsem_huawei_solar_batteries_state_of_capacity"
    )
    hsem_house_consumption_power = get_config_value(
        config_entry, "hsem_house_consumption_power"
    )
    hsem_solar_production_power = get_config_value(
        config_entry, "hsem_solar_production_power"
    )
    hsem_solcast_pv_forecast_forecast_today = get_config_value(
        config_entry, "hsem_solcast_pv_forecast_forecast_today"
    )
    hsem_ev_charger_status = get_config_value(config_entry, "hsem_ev_charger_status")
    hsem_ev_charger_power = get_config_value(config_entry, "hsem_ev_charger_power")

    hsem_battery_max_capacity = get_config_value(
        config_entry, "hsem_battery_max_capacity"
    )
    hsem_energi_data_service_import = get_config_value(
        config_entry, "hsem_energi_data_service_import"
    )
    hsem_energi_data_service_export = get_config_value(
        config_entry, "hsem_energi_data_service_export"
    )
    hsem_huawei_solar_inverter_active_power_control = get_config_value(
        config_entry, "hsem_huawei_solar_inverter_active_power_control"
    )
    hsem_huawei_solar_batteries_maximum_charging_power = get_config_value(
        config_entry, "hsem_huawei_solar_batteries_maximum_charging_power"
    )
    hsem_battery_conversion_loss = get_config_value(
        config_entry, "hsem_battery_conversion_loss"
    )
    hsem_house_power_includes_ev_charger_power = get_config_value(
        config_entry, "hsem_house_power_includes_ev_charger_power"
    )
    hsem_morning_energy_need = get_config_value(
        config_entry, "hsem_morning_energy_need"
    )

    # Create the export from the input from hsem_energi_data_service_export
    working_mode_sensor = WorkingModeSensor(config_entry)

    # Add input entities to the sensor
    working_mode_sensor.set_hsem_huawei_solar_device_id_inverter_1(
        hsem_huawei_solar_device_id_inverter_1
    )
    working_mode_sensor.set_hsem_huawei_solar_device_id_inverter_2(
        hsem_huawei_solar_device_id_inverter_2
    )
    working_mode_sensor.set_hsem_huawei_solar_device_id_batteries(
        hsem_huawei_solar_device_id_batteries
    )
    working_mode_sensor.set_hsem_huawei_solar_batteries_working_mode(
        hsem_huawei_solar_batteries_working_mode
    )
    working_mode_sensor.set_hsem_huawei_solar_batteries_state_of_capacity(
        hsem_huawei_solar_batteries_state_of_capacity
    )
    working_mode_sensor.set_hsem_house_consumption_power(hsem_house_consumption_power)
    working_mode_sensor.set_hsem_solar_production_power(hsem_solar_production_power)
    working_mode_sensor.set_hsem_ev_charger_status(hsem_ev_charger_status)
    working_mode_sensor.set_hsem_ev_charger_power(hsem_ev_charger_power)
    working_mode_sensor.set_hsem_solcast_pv_forecast_forecast_today(
        hsem_solcast_pv_forecast_forecast_today
    )
    working_mode_sensor.set_hsem_battery_max_capacity(hsem_battery_max_capacity)
    working_mode_sensor.set_hsem_energi_data_service_import(
        hsem_energi_data_service_import
    )
    working_mode_sensor.set_hsem_energi_data_service_export(
        hsem_energi_data_service_export
    )
    working_mode_sensor.set_hsem_huawei_solar_inverter_active_power_control(
        hsem_huawei_solar_inverter_active_power_control
    )
    working_mode_sensor.set_hsem_huawei_solar_batteries_maximum_charging_power(
        hsem_huawei_solar_batteries_maximum_charging_power
    )
    working_mode_sensor.set_hsem_battery_conversion_loss(hsem_battery_conversion_loss)
    working_mode_sensor.set_hsem_house_power_includes_ev_charger_power(
        hsem_house_power_includes_ev_charger_power
    )

    # Wait for power, energy and energy average sensors to be set up
    power_sensors = await async_setup_power_sensors(
        config_entry,
        hsem_house_consumption_power,
        hsem_ev_charger_power,
        hsem_house_power_includes_ev_charger_power,
    )

    energy_sensors = await async_setup_energy_sensors(config_entry)

    energy_average_sensors = await async_setup_energy__average_sensors(config_entry)

    # Add sensors to Home Assistant
    async_add_entities(
        [working_mode_sensor] + power_sensors + energy_sensors + energy_average_sensors
    )

    # Store reference to the platform to handle unloads later
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    hass.data[DOMAIN][config_entry.entry_id] = async_add_entities


async def async_unload_entry(hass, entry):
    """Handle unloading of an entry."""
    platform = hass.data[DOMAIN].get(entry.entry_id)
    if platform:
        return await platform.async_remove_entry(entry)
    return False


async def async_setup_power_sensors(
    config_entry,
    hsem_house_consumption_power,
    hsem_ev_charger_power,
    hsem_house_power_includes_ev_charger_power,
):
    """Set up house consumption power sensors for each hour block."""
    sensors = []
    for hour in range(24):
        hour_start = hour
        hour_end = (hour + 1) % 24
        sensor = HouseConsumptionPowerSensor(config_entry, hour_start, hour_end)
        sensor.set_hsem_house_consumption_power(hsem_house_consumption_power)
        sensor.set_hsem_ev_charger_power(hsem_ev_charger_power)
        sensor.set_hsem_house_power_includes_ev_charger_power(
            hsem_house_power_includes_ev_charger_power
        )
        sensors.append(sensor)
    return sensors


async def async_setup_energy_sensors(config_entry):
    """Setup House Consumption Energy sensors for each hour in the day."""
    sensors = []
    for hour in range(24):
        hour_start = hour
        hour_end = (hour + 1) % 24
        sensors.append(HouseConsumptionEnergySensor(config_entry, hour_start, hour_end))
    return sensors


async def async_setup_energy__average_sensors(config_entry):
    """Setup House Consumption Energy Average sensors for each hour in the day."""
    sensors = []
    for hour in range(24):
        hour_start = hour
        hour_end = (hour + 1) % 24
        sensors.append(
            HouseConsumptionEnergyAverageSensor(
                config_entry, hour_start, hour_end, 2160, 3
            )
        )
        sensors.append(
            HouseConsumptionEnergyAverageSensor(
                config_entry, hour_start, hour_end, 5040, 7
            )
        )
        sensors.append(
            HouseConsumptionEnergyAverageSensor(
                config_entry, hour_start, hour_end, 10080, 14
            )
        )
    return sensors

import logging

from .const import DOMAIN
from .custom_sensors.house_consumption_energy_average_sensor import (
    HouseConsumptionEnergyAverageSensor,
)
from .custom_sensors.house_consumption_energy_sensor import HouseConsumptionEnergySensor
from .custom_sensors.house_consumption_power_sensor import HouseConsumptionPowerSensor
from .custom_sensors.working_mode_sensor import WorkingModeSensor
from .utils.misc import generate_md5_hash, get_config_value

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

    # Create the export from the input from hsem_energi_data_service_export
    working_mode_sensor = WorkingModeSensor(
        hsem_huawei_solar_device_id_inverter_1,
        hsem_huawei_solar_device_id_inverter_2,
        hsem_huawei_solar_device_id_batteries,
        hsem_huawei_solar_batteries_working_mode,
        hsem_huawei_solar_batteries_state_of_capacity,
        config_entry,
    )

    # Afvent at power_sensors returnerer en liste
    power_sensors = await async_setup_power_sensors(
        config_entry, hsem_house_consumption_power
    )

    energy_sensors = await async_setup_energy_sensors(config_entry)

    energy_average_sensors = await async_setup_energy__average_sensors(config_entry)

    # Tilføj alle sensorer til Home Assistant
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


async def async_setup_power_sensors(config_entry, hsem_house_consumption_power):
    """Set up house consumption power sensors for each hour block."""
    sensors = []
    for hour in range(24):
        hour_start = hour
        hour_end = (hour + 1) % 24
        sensors.append(
            HouseConsumptionPowerSensor(
                config_entry, hour_start, hour_end, hsem_house_consumption_power
            )
        )
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
            HouseConsumptionEnergyAverageSensor(config_entry, hour_start, hour_end)
        )
    return sensors

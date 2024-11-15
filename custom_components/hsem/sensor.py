import logging

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.custom_sensors.house_consumption_power_sensor import (
    HouseConsumptionPowerSensor,
)
from custom_components.hsem.custom_sensors.working_mode_sensor import WorkingModeSensor
from custom_components.hsem.utils.misc import get_config_value

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """
    Set up HSEM sensors from a config entry.

    This function initializes various HSEM sensors based on the provided configuration entry.
    It extracts configuration parameters, creates a WorkingModeSensor, sets its attributes,
    and sets up power, energy, and energy average sensors. Finally, it adds these sensors
    to Home Assistant and stores a reference to the platform for handling unloads later.

    Args:
        hass (HomeAssistant): The Home Assistant instance.
        config_entry (ConfigEntry): The configuration entry containing setup information.
        async_add_entities (Callable): The function to add entities to Home Assistant.

    Returns:
        None
    """

    # Setup working mode sensor
    working_mode_sensor = WorkingModeSensor(config_entry)

    # Add power, energy and energy average sensors
    power_sensors = []
    for hour in range(24):
        hour_start = hour
        hour_end = (hour + 1) % 24
        sensor = HouseConsumptionPowerSensor(config_entry, hour_start, hour_end)
        power_sensors.append(sensor)

    # Add sensors to Home Assistant
    async_add_entities([working_mode_sensor] + power_sensors)

    # Store reference to the platform to handle unloads later
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    hass.data[DOMAIN][config_entry.entry_id] = async_add_entities


async def async_unload_entry(hass, entry):
    """
    Handle unloading of an entry.

    This function is responsible for unloading a specific entry from the Home Assistant instance.
    It retrieves the platform associated with the entry from the hass data and attempts to remove it.

    Args:
        hass (HomeAssistant): The Home Assistant instance.
        entry (ConfigEntry): The configuration entry to unload.

    Returns:
        bool: True if the entry was successfully removed, False otherwise.
    """
    platform = hass.data[DOMAIN].get(entry.entry_id)
    if platform:
        return await platform.async_remove_entry(entry)
    return False

import logging

from .const import DOMAIN
from .custom_sensors.working_mode_sensor import WorkingModeSensor
from .utils.misc import get_config_value, generate_md5_hash

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

    # Create the export from the input from hsem_energi_data_service_export
    working_mode_sensor = WorkingModeSensor(
        hsem_huawei_solar_device_id_inverter_1,
        hsem_huawei_solar_device_id_inverter_2,
        hsem_huawei_solar_device_id_batteries,
        hsem_huawei_solar_batteries_working_mode,
        hsem_huawei_solar_batteries_state_of_capacity,
        config_entry,
    )

    # Add sensors to Home Assistant
    async_add_entities(
        [
            working_mode_sensor,
        ]
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

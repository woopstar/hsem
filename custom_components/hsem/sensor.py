import hashlib
import logging

from .const import (
    DOMAIN,
)

from .custom_sensors.working_mode_sensor import WorkingModeSensor

_LOGGER = logging.getLogger(__name__)

def generate_md5_hash(input_sensor):
    """Generate an MD5 hash based on the input sensor's name."""
    return hashlib.md5(input_sensor.encode("utf-8")).hexdigest()

def get_config_value(config_entry, key, default_value):
    """Get the configuration value from options or fall back to the initial data."""
    return config_entry.options.get(key, config_entry.data.get(key, default_value))


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up HSEM sensors from a config entry."""
    config = config_entry.data

    # Extract configuration parameters
    hsem_huawei_solar_device_id_inverter_1 = config.get("hsem_huawei_solar_device_id_inverter_1")
    hsem_huawei_solar_device_id_inverter_2 = config.get("hsem_huawei_solar_device_id_inverter_2")
    hsem_huawei_solar_device_id_batteries = config.get("hsem_huawei_solar_device_id_batteries")
    hsem_huawei_solar_batteries_working_mode = config.get("hsem_huawei_solar_batteries_working_mode")
    hsem_huawei_solar_batteries_state_of_capacity = config.get("hsem_huawei_solar_batteries_state_of_capacity")
    hsem_huawei_solar_inverter_active_power_control = config.get("hsem_huawei_solar_inverter_active_power_control")

    # Create the export from the input from hsem_energi_data_service_export
    working_mode_sensor = WorkingModeSensor(
        hsem_huawei_solar_device_id_inverter_1,
        hsem_huawei_solar_device_id_inverter_2,
        hsem_huawei_solar_device_id_batteries,
        hsem_huawei_solar_batteries_working_mode,
        hsem_huawei_solar_batteries_state_of_capacity,
        hsem_huawei_solar_inverter_active_power_control,
        config_entry
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

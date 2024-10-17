import hashlib
import logging

from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .custom_sensors.export_sensor import ExportSensor
from .custom_sensors.import_sensor import ImportSensor
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
    hsem_huawei_solar_inverter_active_power_control = get_config_value(
        config_entry, "hsem_huawei_solar_inverter_active_power_control"
    )
    hsem_huawei_solar_device_id_batteries = get_config_value(
        config_entry, "hsem_huawei_solar_device_id_batteries"
    )
    hsem_energi_data_service_import = get_config_value(
        config_entry, "hsem_energi_data_service_import"
    )
    hsem_energi_data_service_export = get_config_value(
        config_entry, "hsem_energi_data_service_export"
    )

    # Create the export from the input from hsem_energi_data_service_export
    export_sensor = ExportSensor(
        hsem_huawei_solar_device_id_inverter_1,
        hsem_huawei_solar_device_id_inverter_2,
        hsem_huawei_solar_inverter_active_power_control,
        hsem_energi_data_service_export,
        config_entry,
    )

    import_sensor = ImportSensor(
        hsem_huawei_solar_device_id_batteries,
        hsem_energi_data_service_import,
        config_entry,
    )

    # Add sensors to Home Assistant
    async_add_entities(
        [
            export_sensor,
            import_sensor,
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

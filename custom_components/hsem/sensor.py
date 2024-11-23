from custom_components.hsem.custom_sensors.HSEMHouseConsumptionPowerSensor import HSEMHouseConsumptionPowerSensor
from custom_components.hsem.custom_sensors.HSEMWorkingModeSensor import HSEMWorkingModeSensor
from homeassistant.helpers.entity_platform import AddEntitiesCallback

async def async_setup_entry(hass, config_entry, async_add_entities: AddEntitiesCallback) -> None:
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
    working_mode_sensor = HSEMWorkingModeSensor(config_entry)

    async_add_entities([working_mode_sensor])

    # Add power, energy and energy average sensors
    power_sensors = []
    for hour in range(24):
        hour_start = hour
        hour_end = (hour + 1) % 24
        sensor = HSEMHouseConsumptionPowerSensor(config_entry, hour_start, hour_end, async_add_entities)
        power_sensors.append(sensor)

    # Add sensors to Home Assistant
    async_add_entities(power_sensors)

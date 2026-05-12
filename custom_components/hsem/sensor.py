from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.custom_sensors.degraded_mode_sensor import (
    HSEMDegradedModeSensor,
)
from custom_components.hsem.custom_sensors.force_mode_sensor import HSEMForceModeSensor
from custom_components.hsem.custom_sensors.hardware_writes_sensor import (
    HSEMHardwareWritesSensor,
)
from custom_components.hsem.custom_sensors.house_consumption_power_sensor import (
    HSEMHouseConsumptionPowerSensor,
)
from custom_components.hsem.custom_sensors.missing_entities_sensor import (
    HSEMMissingEntitiesSensor,
)
from custom_components.hsem.custom_sensors.net_consumption_sensor import (
    HSEMNetConsumptionSensor,
)
from custom_components.hsem.custom_sensors.next_update_sensor import (
    HSEMNextUpdateSensor,
)
from custom_components.hsem.custom_sensors.read_only_sensor import HSEMReadOnlySensor
from custom_components.hsem.custom_sensors.working_mode_sensor import (
    HSEMWorkingModeSensor,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HSEM sensors from a config entry."""

    # Retrieve the coordinator created in async_setup_entry (__init__.py).
    coordinator: HSEMDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id][
        "coordinator"
    ]

    # Diagnostic sensors — all subscribe to coordinator updates.
    degraded_mode_sensor = HSEMDegradedModeSensor(config_entry, coordinator)
    read_only_sensor = HSEMReadOnlySensor(config_entry, coordinator)
    next_update_sensor = HSEMNextUpdateSensor(config_entry, coordinator)
    missing_entities_sensor = HSEMMissingEntitiesSensor(config_entry, coordinator)
    hardware_writes_sensor = HSEMHardwareWritesSensor(config_entry, coordinator)
    net_consumption_sensor = HSEMNetConsumptionSensor(config_entry, coordinator)
    force_mode_sensor = HSEMForceModeSensor(config_entry, coordinator)

    # Working-mode sensor — subscribes to coordinator updates and owns hardware writes.
    working_mode_sensor = HSEMWorkingModeSensor(config_entry, coordinator)

    async_add_entities(
        [
            degraded_mode_sensor,
            read_only_sensor,
            next_update_sensor,
            missing_entities_sensor,
            hardware_writes_sensor,
            net_consumption_sensor,
            force_mode_sensor,
            working_mode_sensor,
        ]
    )

    # Add power, energy and energy average sensors (these remain self-polling).
    power_sensors = []
    for hour in range(24):
        hour_start = hour
        hour_end = (hour + 1) % 24
        sensor = HSEMHouseConsumptionPowerSensor(
            config_entry, hour_start, hour_end, async_add_entities
        )
        power_sensors.append(sensor)

    # Add sensors to Home Assistant
    async_add_entities(power_sensors)

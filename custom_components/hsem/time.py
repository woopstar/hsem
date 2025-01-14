from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.hsem.custom_times.entity import HSEMTimeEntity
from custom_components.hsem.utils.misc import get_config_value

TIMES = {
    "hsem_batteries_enable_batteries_schedule_1_start": {
        "name": "Batteries Discharge Schedule 1 Start",
        "description": "Start time for schedule 1.",
    },
    "hsem_batteries_enable_batteries_schedule_1_end": {
        "name": "Batteries Discharge Schedule 1 End",
        "description": "End time for schedule 1.",
    },
    "hsem_batteries_enable_batteries_schedule_2_start": {
        "name": "Batteries Discharge Schedule 2 Start",
        "description": "Start time for schedule 2.",
    },
    "hsem_batteries_enable_batteries_schedule_2_end": {
        "name": "Batteries Discharge Schedule 2 End",
        "description": "End time for schedule 2.",
    },
    "hsem_batteries_enable_batteries_schedule_3_start": {
        "name": "Batteries Discharge Schedule 3 Start",
        "description": "Start time for schedule 3.",
    },
    "hsem_batteries_enable_batteries_schedule_3_end": {
        "name": "Batteries Discharge Schedule 3 End",
        "description": "End time for schedule 3.",
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up time entities for the HSEM integration."""
    time_entities = []
    for key, data in TIMES.items():

        current_value = str(get_config_value(config_entry, key))
        time_entities.append(
            HSEMTimeEntity(
                hass,
                config_entry,
                key,
                data["name"],
                data["description"],
                current_value,
            )
        )

    async_add_entities(time_entities)

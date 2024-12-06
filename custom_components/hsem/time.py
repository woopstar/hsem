from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.hsem.custom_times.entity import HSEMTimeEntity
from custom_components.hsem.utils.misc import get_config_value

TIMES = {
    "hsem_batteries_enable_charge_hours_day_start": {
        "name": "Daytime Start",
        "description": "Start time for daytime charging hours.",
    },
    "hsem_batteries_enable_charge_hours_day_end": {
        "name": "Daytime End",
        "description": "End time for daytime charging hours.",
    },
    "hsem_batteries_enable_charge_hours_night_start": {
        "name": "Night Start",
        "description": "Start time for nighttime charging hours.",
    },
    "hsem_batteries_enable_charge_hours_night_end": {
        "name": "Night End",
        "description": "End time for nighttime charging hours.",
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

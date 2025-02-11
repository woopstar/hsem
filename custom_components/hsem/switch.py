from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.hsem.custom_switches.entity import HSEMSwitch

SWITCHES = {
    "hsem_read_only": {
        "name": "Read Only",
        "description": "Toggle read-only mode for the integration.",
    },
    "hsem_extended_attributes": {
        "name": "Extended Attributes",
        "description": "Extend amount of attributes provided by the working mode sensor.",
    },
    "hsem_verbose_logging": {
        "name": "Verbose Logging",
        "description": "Enable to get verbose logging into the HA log.",
    },
    "hsem_batteries_enable_batteries_schedule_1": {
        "name": "Batteries Discharge Schedule 1",
        "description": "Enable or disable batteries schedule 1.",
    },
    "hsem_batteries_enable_batteries_schedule_2": {
        "name": "Batteries Discharge Schedule 2",
        "description": "Enable or disable batteries schedule 2.",
    },
    "hsem_batteries_enable_batteries_schedule_3": {
        "name": "Batteries Discharge Schedule 3",
        "description": "Enable or disable batteries schedule 3.",
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HSEM switches from a config entry."""
    async_add_entities(
        [
            HSEMSwitch(
                hass,
                config_entry,
                key,
                switch_data["name"],
                switch_data["description"],
            )
            for key, switch_data in SWITCHES.items()
        ]
    )

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.hsem.custom_switches.entity import HSEMSwitch

SWITCHES = {
    "hsem_read_only": {
        "name": "Read Only",
        "description": "Toggle read-only mode for the integration.",
    },
    "hsem_batteries_enable_charge_hours_day": {
        "name": "Day Charging",
        "description": "Enable or disable daytime charge hours.",
    },
    "hsem_batteries_enable_charge_hours_night": {
        "name": "Night Charging",
        "description": "Enable or disable nighttime charge hours.",
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
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

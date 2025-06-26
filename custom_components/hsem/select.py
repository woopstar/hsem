"""
Selector entity for forcing a working mode in the HSEM integration.

This module defines a selector entity that allows users to choose a working mode,
including an "Auto" option as default. The available working modes are imported
from utils/recommendations.py.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.hsem.custom_selectors.entity import HSEMWorkingModeSelector
from custom_components.hsem.utils.recommendations import Recommendations

# Only include the specified recommendations
RECOMMENDATION_OPTIONS = [
    Recommendations.BatteriesChargeGrid.value,
    Recommendations.BatteriesChargeSolar.value,
    Recommendations.BatteriesDischargeMode.value,
    Recommendations.BatteriesWaitMode.value,
    Recommendations.EVSmartCharging.value,
    Recommendations.ForceBatteriesDischarge.value,
    Recommendations.ForceExport.value,
]

SELECTORS = {
    "hsem_force_working_mode": {
        "unique_id": "hsem_force_working_mode",
        "name": "Force Working Mode",
        "description": "Force a specific working mode for the integration.",
        "options": ["auto"] + RECOMMENDATION_OPTIONS,
        "default": "auto",
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HSEM selectors from a config entry."""
    async_add_entities(
        [
            HSEMWorkingModeSelector(
                hass,
                config_entry,
                selector_data["unique_id"],
                selector_data["name"],
                selector_data["description"],
                selector_data["options"],
                selector_data["default"],
            )
            for key, selector_data in SELECTORS.items()
        ]
    )

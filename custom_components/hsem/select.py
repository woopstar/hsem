"""Select platform for the HSEM integration.

Exposes a ``SelectEntity`` that lets users force a specific working mode or
leave the planner in ``"auto"`` mode.
"""

from homeassistant.components.select import SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.hsem.custom_selectors.entity import HSEMWorkingModeSelector
from custom_components.hsem.utils.recommendations import Recommendations

# Selectable working modes exposed to the user.
_RECOMMENDATION_OPTIONS = [
    Recommendations.BatteriesChargeGrid.value,
    Recommendations.BatteriesChargeSolar.value,
    Recommendations.BatteriesDischargeMode.value,
    Recommendations.BatteriesWaitMode.value,
    Recommendations.EVSmartCharging.value,
    Recommendations.ForceBatteriesDischarge.value,
    Recommendations.ForceExport.value,
]

# Default selection value.
_DEFAULT_OPTION = "auto"

# Entity descriptions for each select entity in this platform.
# Using SelectEntityDescription keeps the definition declarative and makes it
# trivial to add more selectors in the future without duplicating constructor
# arguments.
SELECTOR_DESCRIPTIONS: tuple[SelectEntityDescription, ...] = (
    SelectEntityDescription(
        key="hsem_force_working_mode",
        name="Force Working Mode",
        icon="mdi:chart-timeline-variant",
        options=[_DEFAULT_OPTION] + _RECOMMENDATION_OPTIONS,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HSEM select entities from a config entry."""
    async_add_entities(
        [
            HSEMWorkingModeSelector(
                hass,
                config_entry,
                description,
                _DEFAULT_OPTION,
            )
            for description in SELECTOR_DESCRIPTIONS
        ]
    )

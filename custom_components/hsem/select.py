"""Select platform for the HSEM integration.

Exposes ``SelectEntity`` instances that let users configure integration
settings from a dropdown on the entity page, without re-running the
config/options flow.
"""

from homeassistant.components.select import SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.hsem.custom_selectors.solcast_likelihood import (
    HSEMSolcastLikelihoodSelector,
)
from custom_components.hsem.custom_selectors.working_mode import HSEMWorkingModeSelector
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.sensornames.diagnostics import (
    get_force_working_mode_selector_key,
    get_solcast_likelihood_selector_key,
)

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
# Display names come from translations via translation_key.
SELECTOR_DESCRIPTIONS: tuple[SelectEntityDescription, ...] = (
    SelectEntityDescription(
        key=get_force_working_mode_selector_key(),
        icon="mdi:chart-timeline-variant",
        options=[_DEFAULT_OPTION] + _RECOMMENDATION_OPTIONS,
        translation_key="force_working_mode",
    ),
    SelectEntityDescription(
        key=get_solcast_likelihood_selector_key(),
        icon="mdi:solar-power",
        options=["pv_estimate", "pv_estimate10", "pv_estimate90"],
        translation_key="pv_estimate_likelihood",
    ),
)


async def async_setup_entry(  # NOSONAR -- HA platform callback, must be async
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HSEM select entities from a config entry."""
    entities: list = []
    for description in SELECTOR_DESCRIPTIONS:
        if description.key == get_force_working_mode_selector_key():
            entities.append(
                HSEMWorkingModeSelector(
                    hass,
                    config_entry,
                    description,
                    _DEFAULT_OPTION,
                )
            )
        elif description.key == get_solcast_likelihood_selector_key():
            entities.append(
                HSEMSolcastLikelihoodSelector(
                    hass,
                    config_entry,
                    description,
                )
            )
    async_add_entities(entities)

"""Number platform for the HSEM integration.

Exposes ``NumberEntity`` instances that let users adjust integration
settings (battery charge/discharge efficiency and EV target SoC) from
the entity page, without re-running the config/options flow.
"""

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.hsem.custom_numbers.battery_efficiency import (
    HSEMBatteryEfficiencyNumber,
)
from custom_components.hsem.custom_numbers.ev_target_soc import HSEMEVTargetSocNumber
from custom_components.hsem.utils.sensornames import (
    get_charge_efficiency_number_entity_id,
    get_charge_efficiency_number_key,
    get_charge_efficiency_number_unique_id,
    get_discharge_efficiency_number_entity_id,
    get_discharge_efficiency_number_key,
    get_discharge_efficiency_number_unique_id,
    get_ev_second_target_soc_number_entity_id,
    get_ev_second_target_soc_number_key,
    get_ev_second_target_soc_number_unique_id,
    get_ev_target_soc_number_entity_id,
    get_ev_target_soc_number_key,
    get_ev_target_soc_number_unique_id,
)

# Entity descriptions for each number entity in this platform.
# Display names come from translations via translation_key.
NUMBER_DESCRIPTIONS: tuple[NumberEntityDescription, ...] = (
    NumberEntityDescription(
        key=get_charge_efficiency_number_key(),
        icon="mdi:battery-plus",
        entity_category=EntityCategory.CONFIG,
        translation_key="charge_efficiency",
    ),
    NumberEntityDescription(
        key=get_discharge_efficiency_number_key(),
        icon="mdi:battery-minus",
        entity_category=EntityCategory.CONFIG,
        translation_key="discharge_efficiency",
    ),
    NumberEntityDescription(
        key=get_ev_target_soc_number_key(),
        icon="mdi:ev-station",
        translation_key="ev_target_soc",
    ),
    NumberEntityDescription(
        key=get_ev_second_target_soc_number_key(),
        icon="mdi:ev-station",
        translation_key="ev_second_target_soc",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HSEM number entities from a config entry."""
    config_keys = {
        get_charge_efficiency_number_key(): "hsem_batteries_charge_efficiency",
        get_discharge_efficiency_number_key(): "hsem_batteries_discharge_efficiency",
        get_ev_target_soc_number_key(): "hsem_ev_target_soc",
        get_ev_second_target_soc_number_key(): "hsem_ev_second_target_soc",
    }

    _id_map = {
        get_charge_efficiency_number_key(): (
            get_charge_efficiency_number_unique_id(config_entry.entry_id),
            get_charge_efficiency_number_entity_id(),
        ),
        get_discharge_efficiency_number_key(): (
            get_discharge_efficiency_number_unique_id(config_entry.entry_id),
            get_discharge_efficiency_number_entity_id(),
        ),
        get_ev_target_soc_number_key(): (
            get_ev_target_soc_number_unique_id(config_entry.entry_id),
            get_ev_target_soc_number_entity_id(config_entry.entry_id),
        ),
        get_ev_second_target_soc_number_key(): (
            get_ev_second_target_soc_number_unique_id(config_entry.entry_id),
            get_ev_second_target_soc_number_entity_id(config_entry.entry_id),
        ),
    }

    _ev_target_soc_keys = {
        get_ev_target_soc_number_key(),
        get_ev_second_target_soc_number_key(),
    }

    entities: list[NumberEntity] = []
    for description in NUMBER_DESCRIPTIONS:
        if description.key in _ev_target_soc_keys:
            entities.append(
                HSEMEVTargetSocNumber(
                    hass,
                    config_entry,
                    description,
                    config_key=config_keys[description.key],
                    unique_id=_id_map[description.key][0],
                    entity_id=_id_map[description.key][1],
                )
            )
        else:
            entities.append(
                HSEMBatteryEfficiencyNumber(
                    hass,
                    config_entry,
                    description,
                    config_key=config_keys[description.key],
                    unique_id=_id_map[description.key][0],
                    entity_id=_id_map[description.key][1],
                )
            )
    async_add_entities(entities)

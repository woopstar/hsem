"""Switch platform for the HSEM integration.

Exposes :class:`SwitchEntity` instances that let users toggle integration
settings (read-only mode, verbose logging, discharge schedules, etc.) without
leaving the entity page.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.hsem.custom_switches.description import (
    HSEMSwitchEntityDescription,
)
from custom_components.hsem.custom_switches.switch import HSEMSwitch
from custom_components.hsem.utils.sensornames import (
    get_batteries_schedule_1_switch_key,
    get_batteries_schedule_2_switch_key,
    get_batteries_schedule_3_switch_key,
    get_ev_force_charge_now_switch_key,
    get_ev_force_discharge_switch_key,
    get_ev_second_force_charge_now_switch_key,
    get_ev_second_smart_charging_switch_key,
    get_ev_smart_charging_switch_key,
    get_extended_attributes_switch_key,
    get_read_only_switch_key,
    get_verbose_logging_switch_key,
)

_ICON_TOGGLE = "mdi:toggle-switch"
_ICON_EV = "mdi:ev-station"

# One description per switch.  Keys are sourced from sensornames.py so that
# unique_ids and entity_ids are defined in one place.  Display names come
# from translations via translation_key.
SWITCH_DESCRIPTIONS: tuple[HSEMSwitchEntityDescription, ...] = (
    HSEMSwitchEntityDescription(
        key=get_read_only_switch_key(),
        icon=_ICON_TOGGLE,
        translation_key="read_only",
    ),
    HSEMSwitchEntityDescription(
        key=get_extended_attributes_switch_key(),
        icon=_ICON_TOGGLE,
        translation_key="extended_attributes",
    ),
    HSEMSwitchEntityDescription(
        key=get_verbose_logging_switch_key(),
        icon=_ICON_TOGGLE,
        translation_key="verbose_logging",
    ),
    HSEMSwitchEntityDescription(
        key=get_batteries_schedule_1_switch_key(),
        icon=_ICON_TOGGLE,
        translation_key="batteries_schedule_1",
    ),
    HSEMSwitchEntityDescription(
        key=get_batteries_schedule_2_switch_key(),
        icon=_ICON_TOGGLE,
        translation_key="batteries_schedule_2",
    ),
    HSEMSwitchEntityDescription(
        key=get_batteries_schedule_3_switch_key(),
        icon=_ICON_TOGGLE,
        translation_key="batteries_schedule_3",
    ),
    HSEMSwitchEntityDescription(
        key=get_ev_force_discharge_switch_key(),
        icon=_ICON_TOGGLE,
        translation_key="ev_force_discharge",
    ),
    HSEMSwitchEntityDescription(
        key=get_ev_smart_charging_switch_key(),
        icon=_ICON_EV,
        translation_key="ev_smart_charging",
    ),
    HSEMSwitchEntityDescription(
        key=get_ev_force_charge_now_switch_key(),
        icon=_ICON_EV,
        translation_key="ev_force_charge_now",
    ),
    HSEMSwitchEntityDescription(
        key=get_ev_second_smart_charging_switch_key(),
        icon=_ICON_EV,
        translation_key="ev_second_smart_charging",
    ),
    HSEMSwitchEntityDescription(
        key=get_ev_second_force_charge_now_switch_key(),
        icon=_ICON_EV,
        translation_key="ev_second_force_charge_now",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HSEM switch entities from a config entry."""
    async_add_entities(
        [
            HSEMSwitch(hass, config_entry, description)
            for description in SWITCH_DESCRIPTIONS
        ]
    )

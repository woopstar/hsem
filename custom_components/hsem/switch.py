"""Switch platform for the HSEM integration.

Exposes :class:`SwitchEntity` instances that let users toggle integration
settings (read-only mode, verbose logging, discharge schedules, etc.) without
leaving the entity page.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.hsem.custom_switches.entity import (
    HSEMSwitch,
    HSEMSwitchEntityDescription,
)
from custom_components.hsem.utils.sensornames import (
    get_batteries_schedule_1_switch_key,
    get_batteries_schedule_1_switch_name,
    get_batteries_schedule_2_switch_key,
    get_batteries_schedule_2_switch_name,
    get_batteries_schedule_3_switch_key,
    get_batteries_schedule_3_switch_name,
    get_ev_force_charge_now_switch_key,
    get_ev_force_charge_now_switch_name,
    get_ev_force_discharge_switch_key,
    get_ev_force_discharge_switch_name,
    get_ev_second_force_charge_now_switch_key,
    get_ev_second_force_charge_now_switch_name,
    get_ev_second_smart_charging_switch_key,
    get_ev_second_smart_charging_switch_name,
    get_ev_smart_charging_switch_key,
    get_ev_smart_charging_switch_name,
    get_extended_attributes_switch_key,
    get_extended_attributes_switch_name,
    get_read_only_switch_key,
    get_read_only_switch_name,
    get_verbose_logging_switch_key,
    get_verbose_logging_switch_name,
)

# One description per switch.  Keys and names are sourced from sensornames.py
# so that unique_ids, entity_ids, and display names are defined in one place.
SWITCH_DESCRIPTIONS: tuple[HSEMSwitchEntityDescription, ...] = (
    HSEMSwitchEntityDescription(
        key=get_read_only_switch_key(),
        name=get_read_only_switch_name(),
        icon="mdi:toggle-switch",
        description="Toggle read-only mode for the integration.",
    ),
    HSEMSwitchEntityDescription(
        key=get_extended_attributes_switch_key(),
        name=get_extended_attributes_switch_name(),
        icon="mdi:toggle-switch",
        description="Extend amount of attributes provided by the working mode sensor.",
    ),
    HSEMSwitchEntityDescription(
        key=get_verbose_logging_switch_key(),
        name=get_verbose_logging_switch_name(),
        icon="mdi:toggle-switch",
        description="Enable to get verbose logging into the HA log.",
    ),
    HSEMSwitchEntityDescription(
        key=get_batteries_schedule_1_switch_key(),
        name=get_batteries_schedule_1_switch_name(),
        icon="mdi:toggle-switch",
        description="Enable or disable batteries schedule 1.",
    ),
    HSEMSwitchEntityDescription(
        key=get_batteries_schedule_2_switch_key(),
        name=get_batteries_schedule_2_switch_name(),
        icon="mdi:toggle-switch",
        description="Enable or disable batteries schedule 2.",
    ),
    HSEMSwitchEntityDescription(
        key=get_batteries_schedule_3_switch_key(),
        name=get_batteries_schedule_3_switch_name(),
        icon="mdi:toggle-switch",
        description="Enable or disable batteries schedule 3.",
    ),
    HSEMSwitchEntityDescription(
        key=get_ev_force_discharge_switch_key(),
        name=get_ev_force_discharge_switch_name(),
        icon="mdi:toggle-switch",
        description=(
            "Enable this if you want to force a specific maximum discharge power"
            " for the Huawei batteries while EV is charging."
        ),
    ),
    HSEMSwitchEntityDescription(
        key=get_ev_smart_charging_switch_key(),
        name=get_ev_smart_charging_switch_name(),
        icon="mdi:ev-station",
        description="Enable smart charging for the primary EV.",
    ),
    HSEMSwitchEntityDescription(
        key=get_ev_force_charge_now_switch_key(),
        name=get_ev_force_charge_now_switch_name(),
        icon="mdi:ev-station",
        description="Force the primary EV to charge at full speed immediately, ignoring the deadline.",
    ),
    HSEMSwitchEntityDescription(
        key=get_ev_second_smart_charging_switch_key(),
        name=get_ev_second_smart_charging_switch_name(),
        icon="mdi:ev-station",
        description="Enable smart charging for the second EV.",
    ),
    HSEMSwitchEntityDescription(
        key=get_ev_second_force_charge_now_switch_key(),
        name=get_ev_second_force_charge_now_switch_name(),
        icon="mdi:ev-station",
        description="Force the second EV to charge at full speed immediately, ignoring the deadline.",
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

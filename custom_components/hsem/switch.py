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

# One description per switch.  The ``key`` is the config-entry option key used
# to persist the value; it is also the basis for the ``unique_id``.
SWITCH_DESCRIPTIONS: tuple[HSEMSwitchEntityDescription, ...] = (
    HSEMSwitchEntityDescription(
        key="hsem_read_only",
        name="Read Only",
        icon="mdi:toggle-switch",
        description="Toggle read-only mode for the integration.",
    ),
    HSEMSwitchEntityDescription(
        key="hsem_extended_attributes",
        name="Extended Attributes",
        icon="mdi:toggle-switch",
        description="Extend amount of attributes provided by the working mode sensor.",
    ),
    HSEMSwitchEntityDescription(
        key="hsem_verbose_logging",
        name="Verbose Logging",
        icon="mdi:toggle-switch",
        description="Enable to get verbose logging into the HA log.",
    ),
    HSEMSwitchEntityDescription(
        key="hsem_batteries_enable_batteries_schedule_1",
        name="Batteries Discharge Schedule 1",
        icon="mdi:toggle-switch",
        description="Enable or disable batteries schedule 1.",
    ),
    HSEMSwitchEntityDescription(
        key="hsem_batteries_enable_batteries_schedule_2",
        name="Batteries Discharge Schedule 2",
        icon="mdi:toggle-switch",
        description="Enable or disable batteries schedule 2.",
    ),
    HSEMSwitchEntityDescription(
        key="hsem_batteries_enable_batteries_schedule_3",
        name="Batteries Discharge Schedule 3",
        icon="mdi:toggle-switch",
        description="Enable or disable batteries schedule 3.",
    ),
    HSEMSwitchEntityDescription(
        key="hsem_ev_charger_force_max_discharge_power",
        name="EV Charger Force Max Discharge Power",
        icon="mdi:toggle-switch",
        description=(
            "Enable this if you want to force a specific maximum discharge power"
            " for the Huawei batteries while EV is charging."
        ),
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

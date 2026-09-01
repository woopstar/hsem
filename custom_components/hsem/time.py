"""Time platform for the HSEM integration.

Exposes :class:`TimeEntity` instances for EV charge deadlines, allowing
users to set them from the entity page.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.hsem.custom_times.description import HSEMTimeEntityDescription
from custom_components.hsem.custom_times.time import HSEMTimeEntity
from custom_components.hsem.utils.misc import get_config_value
from custom_components.hsem.utils.sensornames.ev import (
    get_ev_deadline_time_key,
    get_ev_second_deadline_time_key,
)

_ICON_CLOCK = "mdi:clock"

# One description per time entity.  Keys are sourced from sensornames/ package so
# that unique_ids and entity_ids are defined in one place.  Display names
# come from translations via translation_key.
TIME_DESCRIPTIONS: tuple[HSEMTimeEntityDescription, ...] = (
    HSEMTimeEntityDescription(
        key=get_ev_deadline_time_key(),
        icon=_ICON_CLOCK,
        translation_key="ev_deadline",
    ),
    HSEMTimeEntityDescription(
        key=get_ev_second_deadline_time_key(),
        icon=_ICON_CLOCK,
        translation_key="ev_second_deadline",
    ),
)


# Time entity keys that only apply to a configured EV — created only when
# that EV's planned load (managed charging) is enabled (issue #859).
_EV_TIME_GATES: dict[str, str] = {
    get_ev_deadline_time_key(): "hsem_ev_planned_load_enabled",
    get_ev_second_deadline_time_key(): "hsem_ev_second_planned_load_enabled",
}


async def async_setup_entry(  # NOSONAR -- HA platform callback, must be async
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HSEM time entities from a config entry."""
    descriptions = [
        description
        for description in TIME_DESCRIPTIONS
        if (config_flag := _EV_TIME_GATES.get(description.key)) is None
        or bool(get_config_value(config_entry, config_flag))
    ]

    async_add_entities(
        [
            HSEMTimeEntity(
                hass,
                config_entry,
                # Stamp the live config-entry value into the description's
                # default_value so the entity starts with the persisted time.
                HSEMTimeEntityDescription(
                    key=description.key,
                    icon=description.icon,
                    translation_key=description.translation_key,
                    default_value=str(get_config_value(config_entry, description.key)),
                ),
            )
            for description in descriptions
        ]
    )

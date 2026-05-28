"""Time platform for the HSEM integration.

Exposes :class:`TimeEntity` instances for each battery discharge schedule
start and end time, allowing users to set them from the entity page.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.hsem.custom_times.entity import (
    HSEMTimeEntity,
    HSEMTimeEntityDescription,
)
from custom_components.hsem.utils.misc import get_config_value
from custom_components.hsem.utils.sensornames import (
    get_schedule_1_end_time_key,
    get_schedule_1_start_time_key,
    get_schedule_2_end_time_key,
    get_schedule_2_start_time_key,
    get_schedule_3_end_time_key,
    get_schedule_3_start_time_key,
)

# One description per time entity.  Keys are sourced from sensornames.py so
# that unique_ids and entity_ids are defined in one place.  Display names
# come from translations via translation_key.
TIME_DESCRIPTIONS: tuple[HSEMTimeEntityDescription, ...] = (
    HSEMTimeEntityDescription(
        key=get_schedule_1_start_time_key(),
        icon="mdi:clock",
        translation_key="schedule_1_start",
    ),
    HSEMTimeEntityDescription(
        key=get_schedule_1_end_time_key(),
        icon="mdi:clock",
        translation_key="schedule_1_end",
    ),
    HSEMTimeEntityDescription(
        key=get_schedule_2_start_time_key(),
        icon="mdi:clock",
        translation_key="schedule_2_start",
    ),
    HSEMTimeEntityDescription(
        key=get_schedule_2_end_time_key(),
        icon="mdi:clock",
        translation_key="schedule_2_end",
    ),
    HSEMTimeEntityDescription(
        key=get_schedule_3_start_time_key(),
        icon="mdi:clock",
        translation_key="schedule_3_start",
    ),
    HSEMTimeEntityDescription(
        key=get_schedule_3_end_time_key(),
        icon="mdi:clock",
        translation_key="schedule_3_end",
    ),
)

# Keep TIMES for backwards compat with existing tests that import it.
TIMES: dict[str, dict[str, str]] = {
    desc.key: {"name": "", "description": ""} for desc in TIME_DESCRIPTIONS
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HSEM time entities from a config entry."""
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
            for description in TIME_DESCRIPTIONS
        ]
    )

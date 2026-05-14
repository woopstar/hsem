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

# One description per time entity.  The ``key`` is the config-entry option key
# used to persist the value; it is also the basis for the ``unique_id``.
TIME_DESCRIPTIONS: tuple[HSEMTimeEntityDescription, ...] = (
    HSEMTimeEntityDescription(
        key="hsem_batteries_enable_batteries_schedule_1_start",
        name="Batteries Discharge Schedule 1 Start",
        icon="mdi:clock",
        description="Start time for schedule 1.",
    ),
    HSEMTimeEntityDescription(
        key="hsem_batteries_enable_batteries_schedule_1_end",
        name="Batteries Discharge Schedule 1 End",
        icon="mdi:clock",
        description="End time for schedule 1.",
    ),
    HSEMTimeEntityDescription(
        key="hsem_batteries_enable_batteries_schedule_2_start",
        name="Batteries Discharge Schedule 2 Start",
        icon="mdi:clock",
        description="Start time for schedule 2.",
    ),
    HSEMTimeEntityDescription(
        key="hsem_batteries_enable_batteries_schedule_2_end",
        name="Batteries Discharge Schedule 2 End",
        icon="mdi:clock",
        description="End time for schedule 2.",
    ),
    HSEMTimeEntityDescription(
        key="hsem_batteries_enable_batteries_schedule_3_start",
        name="Batteries Discharge Schedule 3 Start",
        icon="mdi:clock",
        description="Start time for schedule 3.",
    ),
    HSEMTimeEntityDescription(
        key="hsem_batteries_enable_batteries_schedule_3_end",
        name="Batteries Discharge Schedule 3 End",
        icon="mdi:clock",
        description="End time for schedule 3.",
    ),
)

# Keep TIMES for backwards compat with existing tests that import it.
TIMES: dict[str, dict[str, str]] = {
    desc.key: {"name": desc.name, "description": desc.description}
    for desc in TIME_DESCRIPTIONS
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
                    name=description.name,
                    icon=description.icon,
                    description=description.description,
                    default_value=str(get_config_value(config_entry, description.key)),
                ),
            )
            for description in TIME_DESCRIPTIONS
        ]
    )

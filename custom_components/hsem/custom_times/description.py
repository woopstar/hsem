"""Time entity description and ID map for the HSEM integration.

Defines :class:`HSEMTimeEntityDescription` and the
``build_time_id_map()`` function that maps config-entry keys to
(unique_id, entity_id) tuples.
"""

from dataclasses import dataclass

from homeassistant.components.time import TimeEntityDescription

from custom_components.hsem.utils.sensornames.controls import (
    get_schedule_1_end_time_entity_id,
    get_schedule_1_end_time_key,
    get_schedule_1_end_time_unique_id,
    get_schedule_1_start_time_entity_id,
    get_schedule_1_start_time_key,
    get_schedule_1_start_time_unique_id,
    get_schedule_2_end_time_entity_id,
    get_schedule_2_end_time_key,
    get_schedule_2_end_time_unique_id,
    get_schedule_2_start_time_entity_id,
    get_schedule_2_start_time_key,
    get_schedule_2_start_time_unique_id,
    get_schedule_3_end_time_entity_id,
    get_schedule_3_end_time_key,
    get_schedule_3_end_time_unique_id,
    get_schedule_3_start_time_entity_id,
    get_schedule_3_start_time_key,
    get_schedule_3_start_time_unique_id,
)
from custom_components.hsem.utils.sensornames.ev import (
    get_ev_deadline_time_entity_id,
    get_ev_deadline_time_key,
    get_ev_deadline_time_unique_id,
    get_ev_second_deadline_time_entity_id,
    get_ev_second_deadline_time_key,
    get_ev_second_deadline_time_unique_id,
)


def build_time_id_map(entry_id: str) -> dict[str, tuple[str, str]]:
    """Build the time ID map for a given config entry.

    Args:
        entry_id: The config entry ID for uniqueness across entries.

    Returns:
        A dict mapping config-entry keys to (unique_id, entity_id) tuples.
    """
    return {
        get_schedule_1_start_time_key(): (
            get_schedule_1_start_time_unique_id(entry_id),
            get_schedule_1_start_time_entity_id(),
        ),
        get_schedule_1_end_time_key(): (
            get_schedule_1_end_time_unique_id(entry_id),
            get_schedule_1_end_time_entity_id(),
        ),
        get_schedule_2_start_time_key(): (
            get_schedule_2_start_time_unique_id(entry_id),
            get_schedule_2_start_time_entity_id(),
        ),
        get_schedule_2_end_time_key(): (
            get_schedule_2_end_time_unique_id(entry_id),
            get_schedule_2_end_time_entity_id(),
        ),
        get_schedule_3_start_time_key(): (
            get_schedule_3_start_time_unique_id(entry_id),
            get_schedule_3_start_time_entity_id(),
        ),
        get_schedule_3_end_time_key(): (
            get_schedule_3_end_time_unique_id(entry_id),
            get_schedule_3_end_time_entity_id(),
        ),
        get_ev_deadline_time_key(): (
            get_ev_deadline_time_unique_id(entry_id),
            get_ev_deadline_time_entity_id(),
        ),
        get_ev_second_deadline_time_key(): (
            get_ev_second_deadline_time_unique_id(entry_id),
            get_ev_second_deadline_time_entity_id(),
        ),
    }


@dataclass(frozen=True)
class HSEMTimeEntityDescription(TimeEntityDescription):
    """Extended entity description that adds a human-readable description field.

    Attributes
    ----------
    description:
        Short human-readable description of the time entity's purpose, exposed
        as an entity attribute for dashboard display.
    default_value:
        Initial time value as an ``"HH:MM:SS"`` string.
    """

    description: str = ""
    default_value: str = "00:00:00"

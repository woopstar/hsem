"""Time entity description and ID map for the HSEM integration.

Defines :class:`HSEMTimeEntityDescription` and the module-level
``_TIME_ID_MAP`` dictionary that maps config-entry keys to
(unique_id, entity_id) tuples.
"""

from dataclasses import dataclass

from homeassistant.components.time import TimeEntityDescription

from custom_components.hsem.utils.sensornames import (
    get_ev_deadline_time_entity_id,
    get_ev_deadline_time_key,
    get_ev_deadline_time_unique_id,
    get_ev_second_deadline_time_entity_id,
    get_ev_second_deadline_time_key,
    get_ev_second_deadline_time_unique_id,
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

# Map from config-entry key → (unique_id, entity_id)
_TIME_ID_MAP: dict[str, tuple[str, str]] = {
    get_schedule_1_start_time_key(): (
        get_schedule_1_start_time_unique_id(),
        get_schedule_1_start_time_entity_id(),
    ),
    get_schedule_1_end_time_key(): (
        get_schedule_1_end_time_unique_id(),
        get_schedule_1_end_time_entity_id(),
    ),
    get_schedule_2_start_time_key(): (
        get_schedule_2_start_time_unique_id(),
        get_schedule_2_start_time_entity_id(),
    ),
    get_schedule_2_end_time_key(): (
        get_schedule_2_end_time_unique_id(),
        get_schedule_2_end_time_entity_id(),
    ),
    get_schedule_3_start_time_key(): (
        get_schedule_3_start_time_unique_id(),
        get_schedule_3_start_time_entity_id(),
    ),
    get_schedule_3_end_time_key(): (
        get_schedule_3_end_time_unique_id(),
        get_schedule_3_end_time_entity_id(),
    ),
    get_ev_deadline_time_key(): (
        get_ev_deadline_time_unique_id(),
        get_ev_deadline_time_entity_id(),
    ),
    get_ev_second_deadline_time_key(): (
        get_ev_second_deadline_time_unique_id(),
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

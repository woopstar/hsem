"""Config flow step for battery schedule 3.

This module is a thin numbered wrapper around the shared helpers in
:mod:`custom_components.hsem.flows.schedule_helpers`.  All schema
construction and validation logic lives there.
"""

import voluptuous as vol

from custom_components.hsem.flows.schedule_helpers import (
    build_batteries_schedule_step_schema,
    validate_batteries_schedule_input,
)


async def get_batteries_schedule_3_step_schema(
    config_entry, hass=None, user_input: dict | None = None
) -> vol.Schema:
    """Return the data schema for the batteries_schedule_3 flow step."""
    return await build_batteries_schedule_step_schema(
        3, config_entry, hass=hass, user_input=user_input
    )


async def validate_batteries_schedule_3_input(user_input) -> dict[str, str]:
    """Validate user input for the battery schedule 3 step."""
    return await validate_batteries_schedule_input(3, user_input)

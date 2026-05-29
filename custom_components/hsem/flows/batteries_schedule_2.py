"""Config flow step for battery schedule 2.

This module is a thin numbered wrapper around the shared helpers in
:mod:`custom_components.hsem.flows.schedule_helpers`.  All schema
construction and validation logic lives there.
"""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.hsem.flows.schedule_helpers import (
    build_batteries_schedule_step_schema,
    validate_batteries_schedule_input,
)


async def get_batteries_schedule_2_step_schema(
    config_entry: ConfigEntry | None,
    hass: HomeAssistant | None = None,
    user_input: dict | None = None,
) -> vol.Schema:
    """Return the data schema for the batteries_schedule_2 flow step."""
    return await build_batteries_schedule_step_schema(
        2, config_entry, hass=hass, user_input=user_input
    )


async def validate_batteries_schedule_2_input(user_input: dict) -> dict[str, str]:
    """Validate user input for the battery schedule 2 step."""
    return await validate_batteries_schedule_input(2, user_input)

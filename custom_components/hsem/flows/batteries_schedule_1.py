"""Config flow step for battery schedule 1.

This module is a thin numbered wrapper around the shared helpers in
:mod:`custom_components.hsem.flows.schedule_helpers`.  All schema
construction and validation logic lives there.
"""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.hsem.flows.schedule_helpers import (
    build_batteries_schedule_step_schema,
    resolve_usable_capacity_kwh,
    validate_batteries_schedule_input,
)

# Re-export the shared capacity resolver under the legacy private name so
# that any external code that imports ``_resolve_usable_capacity_kwh`` from
# this module continues to work without changes.
_resolve_usable_capacity_kwh = resolve_usable_capacity_kwh


async def get_batteries_schedule_1_step_schema(
    config_entry: ConfigEntry | None,
    hass: HomeAssistant | None = None,
    user_input: dict | None = None,
) -> vol.Schema:
    """Return the data schema for the batteries_schedule_1 flow step."""
    return await build_batteries_schedule_step_schema(
        1, config_entry, hass=hass, user_input=user_input
    )


async def validate_batteries_schedule_1_input(user_input: dict) -> dict[str, str]:
    """Validate user input for the battery schedule 1 step."""
    return await validate_batteries_schedule_input(1, user_input)

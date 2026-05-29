"""Merged config flow step for all three battery discharge schedules.

Combines the three previously separate schedule steps (batteries_schedule_1,
batteries_schedule_2, batteries_schedule_3) into a single form so that users
can configure all schedule windows at once.  Schema construction and
validation reuse the shared helpers in :mod:`schedule_helpers`.
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
# that existing imports from batteries_schedule_1 continue to work.
_resolve_usable_capacity_kwh = resolve_usable_capacity_kwh


async def get_batteries_schedules_step_schema(
    config_entry: ConfigEntry | None,
    hass: HomeAssistant | None = None,
    user_input: dict | None = None,
) -> vol.Schema:
    """Return the data schema for the merged batteries_schedules step.

    Combines schedules 1, 2, and 3 into a single form.  Each schedule has
    three fields: enabled (boolean), start (time), end (time).
    """
    schema_1 = await build_batteries_schedule_step_schema(
        1, config_entry, hass=hass, user_input=user_input
    )
    schema_2 = await build_batteries_schedule_step_schema(
        2, config_entry, hass=hass, user_input=user_input
    )
    schema_3 = await build_batteries_schedule_step_schema(
        3, config_entry, hass=hass, user_input=user_input
    )
    # Merge all three schemas into one
    merged = {}
    merged.update(schema_1.schema)
    merged.update(schema_2.schema)
    merged.update(schema_3.schema)
    return vol.Schema(merged)


async def validate_batteries_schedules_input(user_input: dict) -> dict[str, str]:
    """Validate user input for the merged batteries_schedules step.

    Delegates to the shared validator for each of the three schedules.
    """
    errors = {}
    for n in (1, 2, 3):
        errs = await validate_batteries_schedule_input(n, user_input)
        errors.update(errs)
    return errors

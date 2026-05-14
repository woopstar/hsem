"""Config flow step for the second EV charger.

This module is a thin wrapper around the shared helpers in
:mod:`custom_components.hsem.flows.ev_helpers`.  All schema construction
and validation logic lives there.
"""

import voluptuous as vol

from custom_components.hsem.flows.ev_helpers import (
    build_ev_charger_schema,
    validate_ev_charger_input,
)


async def get_ev_second_step_schema(config_entry) -> vol.Schema:
    """Return the data schema for the second EV charger flow step."""
    return await build_ev_charger_schema(
        config_entry,
        prefix="hsem_ev_second",
        include_primary_fields=False,
    )


async def validate_ev_second_step_input(hass, user_input) -> dict[str, str]:
    """Validate user input for the second EV charger flow step."""
    return await validate_ev_charger_input(
        hass,
        user_input,
        prefix="hsem_ev_second",
    )

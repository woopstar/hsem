"""Config flow step for the primary EV charger.

This module is a thin wrapper around the shared helpers in
:mod:`custom_components.hsem.flows.ev_helpers`.  All schema construction
and validation logic lives there.
"""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.hsem.flows.ev_helpers import (
    build_ev_charger_schema,
    validate_ev_charger_input,
)


async def get_ev_step_schema(config_entry: ConfigEntry | None) -> vol.Schema:
    """Return the data schema for the primary EV charger flow step."""
    return await build_ev_charger_schema(
        config_entry,
        prefix="hsem_ev",
        include_primary_fields=True,
    )


async def validate_ev_step_input(
    hass: HomeAssistant, user_input: dict
) -> dict[str, str]:
    """Validate user input for the primary EV charger flow step."""
    return await validate_ev_charger_input(
        hass,
        user_input,
        prefix="hsem_ev",
        extra_required_fields=["hsem_house_power_includes_ev_charger_power"],
    )

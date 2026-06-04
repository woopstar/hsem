"""Config flow step for the primary EV planned load integration.

Thin wrapper around :mod:`ev_planned_load_helpers` using the
``hsem_ev_planned_load_`` prefix.  All schema and validation logic
lives in the shared helpers module.

The step is optional — when ``hsem_ev_planned_load_enabled`` is False
all fields are ignored by the planner and state collector.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.hsem.flows.ev_planned_load_helpers import (
    build_ev_planned_load_schema,
    validate_ev_planned_load_schema_input,
)

_PREFIX = "hsem_ev_planned_load"


async def get_ev_planned_load_step_schema(
    config_entry: ConfigEntry | None,
) -> vol.Schema:
    """Return the data schema for the primary EV planned load config flow step."""
    return await build_ev_planned_load_schema(config_entry, _PREFIX)


async def validate_ev_planned_load_input(
    _hass: HomeAssistant, user_input: dict
) -> dict[str, str]:
    """Validate user input for the primary EV planned load flow step."""
    return await validate_ev_planned_load_schema_input(user_input, _PREFIX)

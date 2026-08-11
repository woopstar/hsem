"""Config flow step for battery wait-mode behaviour.

Allows the user to choose whether ``batteries_wait_mode`` keeps the battery
strictly idle or allows normal household self-consumption while protecting
the planner's required battery reserve.
"""

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import get_config_value

WAIT_MODE_BEHAVIOR_OPTIONS = [
    {"label": "Strict wait", "value": "strict"},
    {
        "label": "Self-consumption with reserve",
        "value": "self_consumption_with_reserve",
    },
]


async def get_batteries_wait_mode_step_schema(  # NOSONAR
    config_entry: ConfigEntry | None,
    _user_input: dict | None = None,
    _hass: Any | None = None,
) -> vol.Schema:
    """Return the data schema for the 'batteries_wait_mode' step.

    Args:
        config_entry: Existing config entry (used during options flow editing)
            or ``None`` for the initial config flow.
        _user_input: Accumulated user input dict from previous config flow steps
            (ignored — kept for call-site compatibility).
        _hass: Home Assistant instance (ignored — kept for call-site compatibility).

    Returns:
        A ``vol.Schema`` with a single select input for wait-mode behaviour.
    """
    return vol.Schema(
        {
            vol.Required(
                "hsem_batteries_wait_mode_behavior",
                default=get_config_value(
                    config_entry, "hsem_batteries_wait_mode_behavior"
                ),
            ): selector(
                {
                    "select": {
                        "options": WAIT_MODE_BEHAVIOR_OPTIONS,
                        "mode": "list",
                        "translation_key": "batteries_wait_mode_behavior",
                    }
                }
            ),
        }
    )


async def validate_batteries_wait_mode_input(user_input: dict) -> dict[str, str]:
    """Validate user input for the 'batteries_wait_mode' step.

    Args:
        user_input: Dict of field name → value submitted by the user.

    Returns:
        Dict mapping field names to translation error keys; empty on success.
    """
    errors: dict[str, str] = {}
    value = user_input.get("hsem_batteries_wait_mode_behavior")
    if value not in ("strict", "self_consumption_with_reserve"):
        errors["hsem_batteries_wait_mode_behavior"] = "invalid_wait_mode_behavior"
    return errors

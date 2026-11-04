"""Config flow step for battery wait-mode behaviour.

Allows the user to choose whether ``batteries_wait_mode`` keeps the battery
strictly idle or allows normal household self-consumption while protecting
the planner's required battery reserve.
"""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import get_config_value
from custom_components.hsem.utils.schema import vol
from custom_components.hsem.utils.wait_mode_behavior import (
    WAIT_MODE_BEHAVIOR_VALUES,
    WaitModeBehavior,
)

WAIT_MODE_BEHAVIOR_OPTIONS = [
    {"label": "Strict wait", "value": WaitModeBehavior.Strict.value},
    {
        "label": "Self-consumption with reserve",
        "value": WaitModeBehavior.SelfConsumptionWithReserve.value,
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
            vol.Required(
                "hsem_batteries_topup_min_power_pct",
                default=get_config_value(
                    config_entry, "hsem_batteries_topup_min_power_pct"
                ),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "mode": "slider",
                        "unit_of_measurement": PERCENTAGE,
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
    if value not in WAIT_MODE_BEHAVIOR_VALUES:
        errors["hsem_batteries_wait_mode_behavior"] = "invalid_wait_mode_behavior"
    pct = user_input.get("hsem_batteries_topup_min_power_pct")
    try:
        pct_value = float(pct) if pct is not None else 10.0
    except TypeError, ValueError:
        pct_value = -1.0
    if not 0.0 <= pct_value <= 100.0:
        errors["hsem_batteries_topup_min_power_pct"] = "invalid_topup_min_power_pct"
    return errors

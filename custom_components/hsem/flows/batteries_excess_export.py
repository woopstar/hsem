"""Flow configuration for battery excess energy export optimization."""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import merge_errors, validate_price
from custom_components.hsem.utils.misc import get_config_value


async def get_batteries_excess_export_step_schema(
    config_entry: ConfigEntry | None,
    user_input: dict | None = None,
    hass: HomeAssistant | None = None,
) -> vol.Schema:
    """Return the data schema for the 'batteries_excess_export' step.

    The price threshold is no longer a manual input — it is auto-calculated
    at runtime from the configured purchase price, expected cycles, and the
    live battery usable capacity using ``calculate_recommended_threshold()``.

    Args:
        config_entry: Existing config entry (used during options flow editing).
        user_input: Accumulated user input dict from previous config flow steps
            (ignored by this simplified schema — kept for call-site compatibility).
        hass: Home Assistant instance (ignored — kept for call-site compatibility).
    """

    return vol.Schema(
        {
            vol.Required(
                "hsem_batteries_enable_excess_export",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_excess_export"
                ),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_batteries_excess_export_discharge_buffer",
                default=get_config_value(
                    config_entry, "hsem_batteries_excess_export_discharge_buffer"
                ),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 50,
                        "step": 1,
                        "mode": "slider",
                        "unit_of_measurement": PERCENTAGE,
                    }
                }
            ),
        }
    )


async def validate_batteries_excess_export_input(user_input: dict) -> dict[str, str]:
    """Validate user input for batteries excess export configuration.

    The price threshold is auto-calculated at runtime from battery
    depreciation parameters, so only the discharge buffer is validated here.

    Args:
        user_input: Dict of field name → value submitted by the user.

    Returns:
        Dict mapping field names to translation error keys; empty on success.
    """
    buffer_errors = validate_price(
        user_input,
        "hsem_batteries_excess_export_discharge_buffer",
        min_price=0.0,
        max_price=50.0,
        allow_negative=False,
    )
    return merge_errors(buffer_errors)

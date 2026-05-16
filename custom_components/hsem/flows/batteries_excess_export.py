"""Flow configuration for battery excess energy export optimization."""

import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.flows.batteries_schedule_1 import (
    _resolve_usable_capacity_kwh,
)
from custom_components.hsem.utils.config_validator import merge_errors, validate_price
from custom_components.hsem.utils.misc import (
    calculate_recommended_threshold,
    convert_to_float,
    convert_to_int,
    get_config_value,
)


async def get_batteries_excess_export_step_schema(
    config_entry, user_input: dict | None = None, hass=None
) -> vol.Schema:
    """Return the data schema for the 'batteries_excess_export' step.

    Args:
        config_entry: Existing config entry (used during options flow editing).
        user_input: Accumulated user input dict from previous config flow steps.
        hass: Optional Home Assistant instance used to resolve the live rated
            capacity state for a more accurate depreciation threshold preview.
    """

    # Calculate recommended threshold as default if not already set.
    # Priority: config_entry (existing config) > user_input (from previous steps) > defaults
    purchase_price = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_purchase_price")
        or (user_input.get("hsem_batteries_purchase_price") if user_input else None)
        or 0.0
    )
    _cycles_ex = convert_to_int(
        get_config_value(config_entry, "hsem_batteries_expected_cycles")
        or (user_input.get("hsem_batteries_expected_cycles") if user_input else None)
    )
    expected_cycles = _cycles_ex if _cycles_ex is not None else 6000
    # Resolve rated capacity from the live HA entity when possible (Wh → kWh).
    # Falls back to 10.0 kWh for the UI preview when the entity is unavailable.
    usable_capacity = _resolve_usable_capacity_kwh(hass, config_entry, user_input)

    recommended = calculate_recommended_threshold(
        purchase_price, expected_cycles, usable_capacity, 0.0
    )

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
                        "unit_of_measurement": "%",
                    }
                }
            ),
            vol.Required(
                "hsem_batteries_excess_export_price_threshold",
                default=get_config_value(
                    config_entry, "hsem_batteries_excess_export_price_threshold"
                )
                or recommended,
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 1,
                        "step": 0.01,
                        "mode": "box",
                    }
                }
            ),
        }
    )


async def validate_batteries_excess_export_input(user_input) -> dict[str, str]:
    """Validate user input for batteries excess export configuration."""
    buffer_errors = validate_price(
        user_input,
        "hsem_batteries_excess_export_discharge_buffer",
        min_price=0.0,
        max_price=50.0,
        allow_negative=False,
    )
    threshold_errors = validate_price(
        user_input,
        "hsem_batteries_excess_export_price_threshold",
        min_price=0.0,
        max_price=1.0,
        allow_negative=False,
    )
    return merge_errors(buffer_errors, threshold_errors)

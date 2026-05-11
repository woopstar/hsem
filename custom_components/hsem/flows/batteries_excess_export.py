"""Flow configuration for battery excess energy export optimization."""

import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import (
    calculate_recommended_threshold,
    convert_to_float,
    convert_to_int,
    get_config_value,
)


async def get_batteries_excess_export_step_schema(
    config_entry, user_input: dict | None = None
) -> vol.Schema:
    """Return the data schema for the 'batteries_excess_export' step.

    Args:
        config_entry: Existing config entry (used during options flow editing).
        user_input: Accumulated user input dict from previous config flow steps.
    """

    # Calculate recommended threshold as default if not already set.
    # Priority: config_entry (existing config) > user_input (from previous steps) > defaults
    purchase_price = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_purchase_price")
        or (user_input.get("hsem_batteries_purchase_price") if user_input else None)
        or 0.0
    )
    expected_cycles = convert_to_int(
        get_config_value(config_entry, "hsem_batteries_expected_cycles")
        or (user_input.get("hsem_batteries_expected_cycles") if user_input else None)
        or 6000
    )
    conversion_loss = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_conversion_loss")
        or (user_input.get("hsem_batteries_conversion_loss") if user_input else None)
        or 10.0
    )
    # Usable capacity is typically a sensor value not available during config setup.
    # Using a reasonable default; actual value will be used in working_mode_sensor.
    usable_capacity = 10.0

    recommended = calculate_recommended_threshold(
        purchase_price, expected_cycles, usable_capacity, conversion_loss
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
    errors = {}

    try:
        # Validate discharge buffer is between 0 and 50%
        buffer = user_input.get("hsem_batteries_excess_export_discharge_buffer")
        if buffer is not None:
            if not isinstance(buffer, (int, float)) or buffer < 0 or buffer > 50:
                errors["hsem_batteries_excess_export_discharge_buffer"] = (
                    "invalid_buffer_range"
                )

        # Validate price threshold is non-negative
        threshold = user_input.get("hsem_batteries_excess_export_price_threshold")
        if threshold is not None:
            if not isinstance(threshold, (int, float)) or threshold < 0:
                errors["hsem_batteries_excess_export_price_threshold"] = (
                    "invalid_price_threshold"
                )

    except (ValueError, TypeError):
        errors["base"] = "invalid_value"

    return errors

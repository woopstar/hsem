"""Flow configuration for battery excess energy export optimization."""

import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import (
    get_config_value,
    calculate_recommended_threshold,
)


async def get_batteries_excess_export_step_schema(config_entry) -> vol.Schema:
    """Return the data schema for the 'batteries_excess_export' step."""

    # Calculate recommended threshold as default if not already set
    purchase_price = get_config_value(config_entry, "hsem_batteries_purchase_price") or 0.0
    expected_cycles = get_config_value(config_entry, "hsem_batteries_expected_cycles") or 6000
    # We can't easily get the current usable capacity from the config_entry as it's a sensor
    # So we use a reasonable default or 0.0 which will result in 0.0 recommended threshold
    # In a real scenario, we might want to fetch the sensor state here.
    usable_capacity = 10.0  # Default assumption for calculation
    conversion_loss = get_config_value(config_entry, "hsem_batteries_conversion_loss") or 10.0
    
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
                ) or recommended,
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

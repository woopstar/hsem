from datetime import datetime

import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import (
    calculate_recommended_threshold,
    convert_to_float,
    convert_to_int,
    get_config_value,
)


async def get_batteries_schedule_1_step_schema(config_entry) -> vol.Schema:
    """Return the data schema for the 'batteries_schedule' step."""

    # Calculate recommended threshold as default if not already set
    purchase_price = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_purchase_price") or 0.0
    )
    expected_cycles = convert_to_int(
        get_config_value(config_entry, "hsem_batteries_expected_cycles") or 6000
    )
    usable_capacity = 10.0  # Default assumption for calculation
    conversion_loss = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_conversion_loss") or 10.0
    )

    recommended = calculate_recommended_threshold(
        purchase_price, expected_cycles, usable_capacity, conversion_loss
    )

    return vol.Schema(
        {
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_1",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_1"
                ),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_1_start",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_1_start"
                ),
            ): selector({"time": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_1_end",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_1_end"
                ),
            ): selector({"time": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_1_min_price_difference",
                default=get_config_value(
                    config_entry,
                    "hsem_batteries_enable_batteries_schedule_1_min_price_difference",
                )
                or recommended,
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 5,
                        "step": 0.01,
                        "mode": "box",
                    }
                }
            ),
        }
    )


async def validate_batteries_schedule_1_input(user_input) -> dict[str, str]:
    """Validate user input."""
    errors = {}

    try:
        # Validate schedule 1 if enabled
        if "hsem_batteries_enable_batteries_schedule_1" in user_input:
            if user_input.get("hsem_batteries_enable_batteries_schedule_1"):
                start = user_input.get(
                    "hsem_batteries_enable_batteries_schedule_1_start"
                )
                end = user_input.get("hsem_batteries_enable_batteries_schedule_1_end")

                # Ensure values are valid times and start < end
                start_time = datetime.strptime(start, "%H:%M:%S").time()
                end_time = datetime.strptime(end, "%H:%M:%S").time()

                if start_time >= end_time:
                    errors["base"] = "start_time_after_end_time"

    except (ValueError, TypeError):
        errors["base"] = "invalid_time_format"

    return errors

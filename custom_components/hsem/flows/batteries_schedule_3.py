from datetime import datetime

import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import (
    calculate_recommended_threshold,
    convert_to_float,
    convert_to_int,
    get_config_value,
)


async def get_batteries_schedule_3_step_schema(
    config_entry, user_input: dict | None = None
) -> vol.Schema:
    """Return the data schema for the 'batteries_schedule' step.

    Args:
        config_entry: The config entry containing saved configuration values.
        user_input: Optional user input from previous config steps.
    """

    # Calculate recommended threshold as default if not already set
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
    # Default assumption for usable capacity
    usable_capacity = 10.0
    conversion_loss = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_conversion_loss")
        or (user_input.get("hsem_batteries_conversion_loss") if user_input else None)
        or 10.0
    )

    recommended = calculate_recommended_threshold(
        purchase_price, expected_cycles, usable_capacity, conversion_loss
    )

    return vol.Schema(
        {
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_3",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_3"
                ),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_3_start",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_3_start"
                ),
            ): selector({"time": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_3_end",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_3_end"
                ),
            ): selector({"time": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_3_min_price_difference",
                default=get_config_value(
                    config_entry,
                    "hsem_batteries_enable_batteries_schedule_3_min_price_difference",
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


async def validate_batteries_schedule_3_input(user_input) -> dict[str, str]:
    """Validate user input."""
    errors = {}

    try:
        # Validate schedule 3 if enabled
        if "hsem_batteries_enable_batteries_schedule_3" in user_input:
            if user_input.get("hsem_batteries_enable_batteries_schedule_3"):
                start = user_input.get(
                    "hsem_batteries_enable_batteries_schedule_3_start"
                )
                end = user_input.get("hsem_batteries_enable_batteries_schedule_3_end")

                # Ensure values are valid times and start < end
                start_time = datetime.strptime(start, "%H:%M:%S").time()
                end_time = datetime.strptime(end, "%H:%M:%S").time()

                if start_time >= end_time:
                    errors["base"] = "start_time_after_end_time"

    except (ValueError, TypeError):
        errors["base"] = "invalid_time_format"

    return errors

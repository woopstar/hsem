from datetime import datetime

import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import (
    calculate_recommended_threshold,
    convert_to_float,
    convert_to_int,
    get_config_value,
)


async def get_batteries_schedule_2_step_schema(config_entry) -> vol.Schema:
    """Return the data schema for the 'batteries_schedule' step."""

    # Calculate recommended threshold as default if not already set
    purchase_price = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_purchase_price") or 0.0
    )
    _cycles_2 = convert_to_int(
        get_config_value(config_entry, "hsem_batteries_expected_cycles")
    )
    expected_cycles = _cycles_2 if _cycles_2 is not None else 6000
    # Rated capacity is a live HA sensor entity and cannot be read during
    # config-flow schema construction.  Use 10 kWh as a representative default
    # for the depreciation-threshold *preview only*.  The actual calculation in
    # the planner uses the live sensor value.
    usable_capacity = 10.0
    conversion_loss = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_conversion_loss") or 10.0
    )

    recommended = calculate_recommended_threshold(
        purchase_price, expected_cycles, usable_capacity, conversion_loss
    )

    return vol.Schema(
        {
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_2",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_2"
                ),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_2_start",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_2_start"
                ),
            ): selector({"time": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_2_end",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_2_end"
                ),
            ): selector({"time": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_2_min_price_difference",
                default=get_config_value(
                    config_entry,
                    "hsem_batteries_enable_batteries_schedule_2_min_price_difference",
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


async def validate_batteries_schedule_2_input(user_input) -> dict[str, str]:
    """Validate user input."""
    errors = {}

    try:
        # Validate schedule 2 if enabled
        if "hsem_batteries_enable_batteries_schedule_2" in user_input:
            if user_input.get("hsem_batteries_enable_batteries_schedule_2"):
                start = user_input.get(
                    "hsem_batteries_enable_batteries_schedule_2_start"
                )
                end = user_input.get("hsem_batteries_enable_batteries_schedule_2_end")

                # Allow cross-midnight windows (start >= end is valid, e.g. 23:00-02:00)
                start_time = datetime.strptime(start, "%H:%M:%S").time()
                end_time = datetime.strptime(end, "%H:%M:%S").time()

                if start_time == end_time:
                    errors["base"] = "start_time_equals_end_time"
    except (ValueError, TypeError):
        errors["base"] = "invalid_time_format"

    return errors

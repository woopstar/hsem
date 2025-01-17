from datetime import datetime

import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import get_config_value


async def get_batteries_schedule_3_step_schema(config_entry):
    """Return the data schema for the 'batteries_schedule' step."""
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
                ),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 5,
                        "step": 0.01,
                        "unit_of_measurement": "DKK",
                        "mode": "box",
                    }
                }
            ),
        }
    )


async def validate_batteries_schedule_3_input(user_input):
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

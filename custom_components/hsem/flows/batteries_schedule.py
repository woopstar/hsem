from datetime import datetime

import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import get_config_value


async def get_batteries_schedule_step_schema(config_entry):
    """Return the data schema for the 'batteries_schedule' step."""
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
                ),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 10,
                        "step": 0.1,
                        "unit_of_measurement": "DKK",
                        "mode": "box",
                    }
                }
            ),
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
        }
    )


async def validate_batteries_schedule_input(user_input):
    """Validate user input for the 'charge_hours' step."""
    errors = {}

    required_fields = [
        "hsem_batteries_enable_batteries_schedule_1",
        "hsem_batteries_enable_batteries_schedule_1_start",
        "hsem_batteries_enable_batteries_schedule_1_end",
        "hsem_batteries_enable_batteries_schedule_2",
        "hsem_batteries_enable_batteries_schedule_2_start",
        "hsem_batteries_enable_batteries_schedule_2_end",
        "hsem_batteries_enable_batteries_schedule_3",
        "hsem_batteries_enable_batteries_schedule_3_start",
        "hsem_batteries_enable_batteries_schedule_3_end",
    ]

    try:
        # Validate schedule 1
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

        # Validate schedule 2
        if "hsem_batteries_enable_batteries_schedule_2" in user_input:
            if user_input.get("hsem_batteries_enable_batteries_schedule_2"):
                start = user_input.get(
                    "hsem_batteries_enable_batteries_schedule_2_start"
                )
                end = user_input.get("hsem_batteries_enable_batteries_schedule_2_end")

                # Ensure values are valid times and start < end
                start_time = datetime.strptime(start, "%H:%M:%S").time()
                end_time = datetime.strptime(end, "%H:%M:%S").time()

                if start_time >= end_time:
                    errors["base"] = "start_time_after_end_time"

        # Validate schedule 3
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

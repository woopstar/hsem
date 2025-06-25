from datetime import datetime

import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import get_config_value

MAX_SCHEDULES = 10  # Maximum number of battery schedules


async def get_batteries_schedule_step_schema(
    config_entry, schedule_number: int
) -> vol.Schema:
    """
    Return the data schema for a specific battery schedule step.

    Parameters:
    config_entry: The configuration entry object.
    schedule_number (int): The schedule number (1 to MAX_SCHEDULES).

    Returns:
    vol.Schema: The schema for the battery schedule step.
    """
    return vol.Schema(
        {
            vol.Required(
                f"hsem_batteries_enable_batteries_schedule_{schedule_number}",
                default=get_config_value(
                    config_entry,
                    f"hsem_batteries_enable_batteries_schedule_{schedule_number}",
                ),
            ): selector({"boolean": {}}),
            vol.Required(
                f"hsem_batteries_enable_batteries_schedule_{schedule_number}_start",
                default=get_config_value(
                    config_entry,
                    f"hsem_batteries_enable_batteries_schedule_{schedule_number}_start",
                ),
            ): selector({"time": {}}),
            vol.Required(
                f"hsem_batteries_enable_batteries_schedule_{schedule_number}_end",
                default=get_config_value(
                    config_entry,
                    f"hsem_batteries_enable_batteries_schedule_{schedule_number}_end",
                ),
            ): selector({"time": {}}),
            vol.Required(
                f"hsem_batteries_enable_batteries_schedule_{schedule_number}_min_price_difference",
                default=get_config_value(
                    config_entry,
                    f"hsem_batteries_enable_batteries_schedule_{schedule_number}_min_price_difference",
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


async def validate_batteries_schedule_input(
    user_input, schedule_number: int
) -> dict[str, str]:
    """
    Validate user input for a specific battery schedule.

    Parameters:
    user_input: The user input dictionary.
    schedule_number (int): The schedule number (1 to MAX_SCHEDULES).

    Returns:
    dict[str, str]: A dictionary of errors, if any.
    """
    errors = {}

    try:
        # Validate the schedule if enabled
        if f"hsem_batteries_enable_batteries_schedule_{schedule_number}" in user_input:
            if user_input.get(
                f"hsem_batteries_enable_batteries_schedule_{schedule_number}"
            ):
                start = user_input.get(
                    f"hsem_batteries_enable_batteries_schedule_{schedule_number}_start"
                )
                end = user_input.get(
                    f"hsem_batteries_enable_batteries_schedule_{schedule_number}_end"
                )

                # Ensure values are valid times and start < end
                start_time = datetime.strptime(start, "%H:%M:%S").time()
                end_time = datetime.strptime(end, "%H:%M:%S").time()

                if start_time >= end_time:
                    errors["base"] = "start_time_after_end_time"

    except (ValueError, TypeError):
        errors["base"] = "invalid_time_format"

    return errors

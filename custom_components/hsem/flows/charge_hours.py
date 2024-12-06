from datetime import datetime

import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import get_config_value


async def get_charge_hours_step_schema(config_entry):
    """Return the data schema for the 'charge_hours' step."""
    return vol.Schema(
        {
            vol.Required(
                "hsem_batteries_enable_charge_hours_day",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_charge_hours_day"
                ),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_batteries_enable_charge_hours_day_start",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_charge_hours_day_start"
                ),
            ): selector({"time": {}}),
            vol.Required(
                "hsem_batteries_enable_charge_hours_day_end",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_charge_hours_day_end"
                ),
            ): selector({"time": {}}),
            vol.Required(
                "hsem_batteries_enable_charge_hours_night",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_charge_hours_night"
                ),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_batteries_enable_charge_hours_night_start",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_charge_hours_night_start"
                ),
            ): selector({"time": {}}),
            vol.Required(
                "hsem_batteries_enable_charge_hours_night_end",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_charge_hours_night_end"
                ),
            ): selector({"time": {}}),
        }
    )


async def validate_charge_hours_input(user_input):
    """Validate user input for the 'charge_hours' step."""
    errors = {}

    required_fields = [
        "hsem_batteries_enable_charge_hours_day",
        "hsem_batteries_enable_charge_hours_day_start",
        "hsem_batteries_enable_charge_hours_day_end",
        "hsem_batteries_enable_charge_hours_night",
        "hsem_batteries_enable_charge_hours_night_start",
        "hsem_batteries_enable_charge_hours_night_end",
    ]

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"

    try:
        # Validate day charge hours
        if user_input.get("hsem_batteries_enable_charge_hours_day"):
            day_start = user_input.get("hsem_batteries_enable_charge_hours_day_start")
            day_end = user_input.get("hsem_batteries_enable_charge_hours_day_end")

            # Ensure values are valid times and start < end
            day_start_time = datetime.strptime(day_start, "%H:%M:%S").time()
            day_end_time = datetime.strptime(day_end, "%H:%M:%S").time()

            if day_start_time >= day_end_time:
                errors["base"] = "start_time_after_end_time"

        # Validate night charge hours
        if user_input.get("hsem_batteries_enable_charge_hours_night"):
            night_start = user_input.get(
                "hsem_batteries_enable_charge_hours_night_start"
            )
            night_end = user_input.get("hsem_batteries_enable_charge_hours_night_end")

            # Ensure values are valid times and start < end
            night_start_time = datetime.strptime(night_start, "%H:%M:%S").time()
            night_end_time = datetime.strptime(night_end, "%H:%M:%S").time()

            if night_start_time >= night_end_time:
                errors["base"] = "start_time_after_end_time"

    except (ValueError, TypeError):
        errors["base"] = "invalid_time_format"

    return errors

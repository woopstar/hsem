import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import convert_months_to_int, get_config_value


def _month_options():
    return [str(i) for i in range(1, 13)]


async def get_months_schema(config_entry) -> vol.Schema:
    """Return the data schema for the 'power' step."""

    return vol.Schema(
        {
            vol.Required(
                "hsem_months_winter",
                default=get_config_value(
                    config_entry,
                    "hsem_months_winter",
                ),
            ): selector(
                {
                    "select": {
                        "options": _month_options(),
                        "multiple": True,
                        "translation_key": "months",
                        "mode": "list",
                    }
                }
            ),
        }
    )


async def validate_months_input(hass, user_input) -> dict[str, str]:
    """Validate user input for the 'power' step."""
    errors = {}

    required_fields = [
        "hsem_months_winter",
    ]

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"
            continue

    # Validate and convert months to integers
    if "hsem_months_winter" in user_input:
        try:
            winter_months = convert_months_to_int(user_input["hsem_months_winter"])
        except ValueError as e:
            errors["hsem_months_winter"] = str(e)
            return errors

    # If we have winter months, calculate summer months (all others)
    if "hsem_months_winter" in user_input:
        all_months = set(range(1, 13))
        summer_months = sorted(list(all_months - set(winter_months)))

        # Validate that there's at least one month in each season
        if not winter_months:
            errors["hsem_months_winter"] = "Winter season must have at least one month"
        elif not summer_months:
            errors["hsem_months_winter"] = "Summer season must have at least one month"

    return errors

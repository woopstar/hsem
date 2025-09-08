import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import get_config_value


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

    return errors

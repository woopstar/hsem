import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import validate_months
from custom_components.hsem.utils.misc import get_config_value


def _month_options() -> list[str]:
    return [str(i) for i in range(1, 13)]


async def get_months_schema(config_entry: ConfigEntry | None) -> vol.Schema:
    """Return the data schema for the 'power' step."""

    # Stored months are integers; the multi-select selector requires string
    # option values.  Convert here so the form pre-selects the saved months.
    raw = get_config_value(config_entry, "hsem_months_winter")
    default_months = [str(m) for m in raw] if raw else []

    return vol.Schema(
        {
            vol.Required(
                "hsem_months_winter",
                default=default_months,
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


async def validate_months_input(
    hass: HomeAssistant, user_input: dict
) -> dict[str, str]:
    """Validate user input for the 'months' step."""
    return validate_months(user_input)

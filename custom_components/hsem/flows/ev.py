import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.const import (
    DEFAULT_HSEM_HOUSE_POWER_INCLUDES_EV_CHARGER_POWER,
)
from custom_components.hsem.utils.misc import get_config_value


async def get_ev_step_schema(config_entry):
    """Return the data schema for the 'misc' step."""
    return vol.Schema(
        {
            vol.Optional(
                "hsem_ev_charger_status",
                default=get_config_value(config_entry, "hsem_ev_charger_status", ""),
            ): selector({"entity": {"domain": ["sensor", "switch", "input_boolean"]}}),
            vol.Optional(
                "hsem_ev_charger_power",
                default=get_config_value(config_entry, "hsem_ev_charger_power", ""),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_house_power_includes_ev_charger_power",
                default=get_config_value(
                    config_entry,
                    "hsem_house_power_includes_ev_charger_power",
                    DEFAULT_HSEM_HOUSE_POWER_INCLUDES_EV_CHARGER_POWER,
                ),
            ): selector({"boolean": {}}),
        }
    )


async def validate_ev_step_input(user_input):
    """Validate user input for the 'misc' step."""
    errors = {}

    required_fields = [
        "hsem_house_power_includes_ev_charger_power",
    ]

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"

    return errors

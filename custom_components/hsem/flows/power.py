import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.const import (
    DEFAULT_HSEM_HOUSE_CONSUMPTION_POWER,
    DEFAULT_HSEM_SOLAR_PRODUCTION_POWER,
)
from custom_components.hsem.utils.misc import get_config_value


def get_power_step_schema(config_entry):
    """Return the data schema for the 'power' step."""
    return vol.Schema(
        {
            vol.Required(
                "hsem_house_consumption_power",
                default=get_config_value(
                    config_entry,
                    "hsem_house_consumption_power",
                    DEFAULT_HSEM_HOUSE_CONSUMPTION_POWER,
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_solar_production_power",
                default=get_config_value(
                    config_entry,
                    "hsem_solar_production_power",
                    DEFAULT_HSEM_SOLAR_PRODUCTION_POWER,
                ),
            ): selector({"entity": {"domain": "sensor"}}),
        }
    )


def validate_power_step_input(user_input):
    """Validate user input for the 'power' step."""
    errors = {}

    required_fields = [
        "hsem_house_consumption_power",
        "hsem_solar_production_power",
    ]

    for field in required_fields:
        if not user_input.get(field):
            errors[field] = "required"

    return errors

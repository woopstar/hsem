import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import get_config_value


async def get_init_step_schema(config_entry):
    """Return the data schema for the 'init' step."""
    return vol.Schema(
        {
            vol.Required(
                "device_name",
                default=get_config_value(config_entry, "device_name"),
            ): str,
            vol.Required(
                "hsem_read_only",
                default=get_config_value(config_entry, "hsem_read_only"),
            ): selector({"boolean": {}}),
        }
    )


async def validate_init_step_input(user_input):
    """Validate user input for the 'init' step."""
    errors = {}

    required_fields = [
        "device_name",
        "hsem_read_only",
    ]

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"

    return errors

import voluptuous as vol
from custom_components.hsem.const import NAME, DEFAULT_HSEM_READ_ONLY
from custom_components.hsem.utils.misc import get_config_value
from homeassistant.helpers.selector import selector

def get_init_step_schema(config_entry):
    """Return the data schema for the 'init' step."""
    return vol.Schema(
        {
            vol.Required(
                "device_name",
                default=get_config_value(config_entry, "device_name", NAME),
            ): str,
            vol.Required(
                "hsem_read_only",
                default=get_config_value(
                    config_entry,
                    "hsem_read_only",
                    DEFAULT_HSEM_READ_ONLY,
                ),
            ): selector({"boolean": {}}),
        }
    )

def validate_init_step_input(user_input):
    """Validate user input for the 'init' step."""
    errors = {}
    if not user_input.get("device_name"):
        errors["device_name"] = "required"
    return errors

import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import async_entity_exists, get_config_value


async def get_power_step_schema(config_entry) -> vol.Schema:
    """Return the data schema for the 'power' step."""
    return vol.Schema(
        {
            vol.Required(
                "hsem_house_consumption_power",
                default=get_config_value(config_entry, "hsem_house_consumption_power"),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_solar_production_power",
                default=get_config_value(config_entry, "hsem_solar_production_power"),
            ): selector({"entity": {"domain": "sensor"}}),
        }
    )


async def validate_power_step_input(hass, user_input) -> dict[str, str]:
    """Validate user input for the 'power' step."""
    errors = {}

    required_fields = [
        "hsem_house_consumption_power",
        "hsem_solar_production_power",
    ]

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"
        else:
            entity_id = user_input[field]
            if not await async_entity_exists(hass, entity_id):
                errors[field] = "entity_not_found"

    return errors

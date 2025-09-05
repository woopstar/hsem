import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import async_entity_exists, get_config_value


async def get_solcast_step_schema(config_entry) -> vol.Schema:
    """Return the data schema for the 'solcast' step."""
    return vol.Schema(
        {
            vol.Required(
                "hsem_solcast_pv_forecast_forecast_today",
                default=get_config_value(
                    config_entry, "hsem_solcast_pv_forecast_forecast_today"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_solcast_pv_forecast_forecast_tomorrow",
                default=get_config_value(
                    config_entry, "hsem_solcast_pv_forecast_forecast_tomorrow"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_solcast_pv_forecast_forecast_likelihood",
                default=get_config_value(
                    config_entry,
                    "hsem_solcast_pv_forecast_forecast_likelihood",
                ),
            ): selector(
                {
                    "select": {
                        "options": ["pv_estimate", "pv_estimate10", "pv_estimate90"]
                    }
                }
            ),
        }
    )


async def validate_solcast_step_input(hass, user_input) -> dict[str, str]:
    """Validate user input for the 'solcast' step."""
    errors = {}

    required_fields = [
        "hsem_solcast_pv_forecast_forecast_today",
        "hsem_solcast_pv_forecast_forecast_tomorrow",
    ]

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"
        else:
            entity_id = user_input[field]
            if not await async_entity_exists(hass, entity_id):
                errors[field] = "entity_not_found"

    return errors

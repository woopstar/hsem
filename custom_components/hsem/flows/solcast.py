import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import async_validate_entity_ids
from custom_components.hsem.utils.misc import get_config_value


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
    return await async_validate_entity_ids(
        hass,
        user_input,
        required_fields=[
            "hsem_solcast_pv_forecast_forecast_today",
            "hsem_solcast_pv_forecast_forecast_tomorrow",
        ],
    )

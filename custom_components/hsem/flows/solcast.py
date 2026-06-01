"""Config flow step for Solcast solar forecast configuration.

Allows the user to select the Solcast forecast entities for today
and tomorrow, as well as the PV estimate likelihood level.
"""

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import async_validate_entity_ids
from custom_components.hsem.utils.misc import get_config_value


async def get_solcast_step_schema(
    config_entry: ConfigEntry | None,
) -> vol.Schema:  # NOSONAR
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
                        "options": ["pv_estimate", "pv_estimate10", "pv_estimate90"],
                        "translation_key": "pv_estimate_likelihood",
                        "mode": "list",
                    }
                }
            ),
        }
    )


async def validate_solcast_step_input(
    hass: HomeAssistant, user_input: dict
) -> dict[str, str]:
    """Validate user input for the 'solcast' step."""
    return await async_validate_entity_ids(
        hass,
        user_input,
        required_fields=[
            "hsem_solcast_pv_forecast_forecast_today",
            "hsem_solcast_pv_forecast_forecast_tomorrow",
        ],
    )

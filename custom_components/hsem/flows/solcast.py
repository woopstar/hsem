import voluptuous as vol
from homeassistant.helpers.selector import selector
from custom_components.hsem.const import (
    DEFAULT_HSEM_SOLCAST_PV_FORECAST_FORECAST_TODAY,
    DEFAULT_HSEM_SOLCAST_PV_FORECAST_FORECAST_TOMORROW,
)
from custom_components.hsem.utils.misc import get_config_value

def get_solcast_step_schema(config_entry):
    """Return the data schema for the 'solcast' step."""
    return vol.Schema(
        {
            vol.Required(
                "hsem_solcast_pv_forecast_forecast_today",
                default=get_config_value(
                    config_entry,
                    "hsem_solcast_pv_forecast_forecast_today",
                    DEFAULT_HSEM_SOLCAST_PV_FORECAST_FORECAST_TODAY,
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_solcast_pv_forecast_forecast_tomorrow",
                default=get_config_value(
                    config_entry,
                    "hsem_solcast_pv_forecast_forecast_tomorrow",
                    DEFAULT_HSEM_SOLCAST_PV_FORECAST_FORECAST_TOMORROW,
                ),
            ): selector({"entity": {"domain": "sensor"}}),
        }
    )

def validate_solcast_step_input(user_input):
    """Validate user input for the 'solcast' step."""
    errors = {}

    required_fields = [
        "hsem_solcast_pv_forecast_forecast_today",
        "hsem_solcast_pv_forecast_forecast_tomorrow",
    ]

    for field in required_fields:
        if not user_input.get(field):
            errors[field] = "required"

    return errors

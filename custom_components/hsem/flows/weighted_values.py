"""Config flow step for weighted consumption values.

Allows the user to configure the weighting percentages for house
consumption energy estimates over 1-day, 3-day, 7-day, and 14-day
lookback periods.
"""

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import validate_consumption_weights
from custom_components.hsem.utils.misc import get_config_value


async def get_weighted_values_step_schema(  # NOSONAR -- async required by HA config/options flow framework
    config_entry: ConfigEntry | None,
) -> vol.Schema:
    """Return the data schema for the 'weighted_values' step."""
    return vol.Schema(
        {
            vol.Required(
                "hsem_house_consumption_energy_weight_1d",
                default=get_config_value(
                    config_entry, "hsem_house_consumption_energy_weight_1d"
                ),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "unit_of_measurement": PERCENTAGE,
                        "mode": "slider",
                    }
                }
            ),
            vol.Required(
                "hsem_house_consumption_energy_weight_3d",
                default=get_config_value(
                    config_entry, "hsem_house_consumption_energy_weight_3d"
                ),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "unit_of_measurement": PERCENTAGE,
                        "mode": "slider",
                    }
                }
            ),
            vol.Required(
                "hsem_house_consumption_energy_weight_7d",
                default=get_config_value(
                    config_entry, "hsem_house_consumption_energy_weight_7d"
                ),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "unit_of_measurement": PERCENTAGE,
                        "mode": "slider",
                    }
                }
            ),
            vol.Required(
                "hsem_house_consumption_energy_weight_14d",
                default=get_config_value(
                    config_entry, "hsem_house_consumption_energy_weight_14d"
                ),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "unit_of_measurement": PERCENTAGE,
                        "mode": "slider",
                    }
                }
            ),
        }
    )


async def validate_weighted_values_input(
    user_input: dict,
) -> dict[str, str]:  # NOSONAR -- async required by HA config/options flow framework
    """Validate user input for the 'weighted_values' step."""
    required_fields = [
        "hsem_house_consumption_energy_weight_1d",
        "hsem_house_consumption_energy_weight_3d",
        "hsem_house_consumption_energy_weight_7d",
        "hsem_house_consumption_energy_weight_14d",
    ]
    required_errors: dict[str, str] = {
        f: "required" for f in required_fields if f not in user_input
    }
    weight_errors = validate_consumption_weights(user_input)
    # Field-level required errors take priority over the base weight error.
    from custom_components.hsem.utils.config_validator import merge_errors

    return merge_errors(required_errors, weight_errors)

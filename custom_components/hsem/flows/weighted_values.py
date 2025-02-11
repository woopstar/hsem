import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import get_config_value


async def get_weighted_values_step_schema(config_entry) -> vol.Schema:
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
                        "unit_of_measurement": "%",
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
                        "unit_of_measurement": "%",
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
                        "unit_of_measurement": "%",
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
                        "unit_of_measurement": "%",
                        "mode": "slider",
                    }
                }
            ),
        }
    )


async def validate_weighted_values_input(user_input) -> dict[str, str]:
    """Validate user input for the 'weighted_values' step."""
    errors = {}

    required_fields = [
        "hsem_house_consumption_energy_weight_1d",
        "hsem_house_consumption_energy_weight_3d",
        "hsem_house_consumption_energy_weight_7d",
        "hsem_house_consumption_energy_weight_14d",
    ]

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"

    total_weight = (
        int(user_input.get("hsem_house_consumption_energy_weight_1d", 0))
        + int(user_input.get("hsem_house_consumption_energy_weight_3d", 0))
        + int(user_input.get("hsem_house_consumption_energy_weight_7d", 0))
        + int(user_input.get("hsem_house_consumption_energy_weight_14d", 0))
    )

    if total_weight != 100:
        errors["base"] = "hsem_house_consumption_energy_weight_total"

    return errors

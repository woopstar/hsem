import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import (
    async_validate_entity_ids,
    merge_errors,
    validate_price,
)
from custom_components.hsem.utils.misc import get_config_value


async def get_energidataservice_step_schema(config_entry) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                "hsem_energi_data_service_import",
                default=get_config_value(
                    config_entry, "hsem_energi_data_service_import"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_energi_data_service_export",
                default=get_config_value(
                    config_entry, "hsem_energi_data_service_export"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_energi_data_service_export_min_price",
                default=get_config_value(
                    config_entry, "hsem_energi_data_service_export_min_price"
                ),
            ): selector(
                {
                    "number": {
                        "min": -2.00,
                        "max": 2.00,
                        "step": 0.01,
                        "mode": "slider",
                    }
                }
            ),
            vol.Required(
                "hsem_energi_data_service_update_interval",
                default=str(
                    get_config_value(
                        config_entry, "hsem_energi_data_service_update_interval"
                    )
                ),
            ): selector(
                {
                    "select": {
                        "multiple": False,
                        "translation_key": "update_interval_minutes",
                        "mode": "list",
                        "options": [
                            "15",
                            "60",
                        ],
                    }
                }
            ),
        }
    )


async def validate_energidataservice_input(hass, user_input) -> dict[str, str]:
    """Validate user input for the 'energidataservice' step."""
    entity_errors = await async_validate_entity_ids(
        hass,
        user_input,
        required_fields=[
            "hsem_energi_data_service_import",
            "hsem_energi_data_service_export",
        ],
    )
    price_errors = validate_price(
        user_input,
        "hsem_energi_data_service_export_min_price",
        min_price=-2.0,
        max_price=2.0,
        allow_negative=True,
    )
    required_errors: dict[str, str] = {}
    for field in (
        "hsem_energi_data_service_export_min_price",
        "hsem_energi_data_service_update_interval",
    ):
        if field not in user_input:
            required_errors[field] = "required"
    return merge_errors(entity_errors, price_errors, required_errors)

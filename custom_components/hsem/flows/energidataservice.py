import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import async_entity_exists, get_config_value


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
        }
    )


async def validate_energidataservice_input(hass, user_input) -> dict[str, str]:
    errors = {}

    required_fields = [
        "hsem_energi_data_service_export_min_price",
    ]

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"

    optional_entity_fields = [
        "hsem_energi_data_service_import",
        "hsem_energi_data_service_export",
    ]

    for field in optional_entity_fields:
        if field not in user_input:
            errors[field] = "required"
        else:
            entity_id = user_input[field]
            if not await async_entity_exists(hass, entity_id):
                errors[field] = "entity_not_found"

    return errors

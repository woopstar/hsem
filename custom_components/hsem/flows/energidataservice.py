import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import async_entity_exists, get_config_value


async def get_energidataservice_step_schema(config_entry):
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
        }
    )


async def validate_energidataservice_input(hass, user_input):
    errors = {}

    required_fields = [
        "hsem_energi_data_service_import",
        "hsem_energi_data_service_export",
    ]

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"
        else:
            entity_id = user_input[field]
            if not await async_entity_exists(hass, entity_id):
                errors[field] = "entity_not_found"

    return errors

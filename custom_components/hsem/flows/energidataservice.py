import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.const import (
    DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT,
    DEFAULT_HSEM_ENERGI_DATA_SERVICE_IMPORT,
)
from custom_components.hsem.utils.misc import get_config_value


def get_energidataservice_step_schema(config_entry):
    return vol.Schema(
        {
            vol.Required(
                "hsem_energi_data_service_import",
                default=get_config_value(
                    config_entry,
                    "hsem_energi_data_service_import",
                    DEFAULT_HSEM_ENERGI_DATA_SERVICE_IMPORT,
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_energi_data_service_export",
                default=get_config_value(
                    config_entry,
                    "hsem_energi_data_service_export",
                    DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT,
                ),
            ): selector({"entity": {"domain": "sensor"}}),
        }
    )


def validate_energidataservice_input(user_input):
    errors = {}

    required_fields = [
        "hsem_energi_data_service_import",
        "hsem_energi_data_service_export",
    ]

    for field in required_fields:
        if not user_input.get(field):
            errors[field] = "required"

    return errors

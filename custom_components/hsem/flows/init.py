"""Config flow step for initial HSEM device setup.

Handles the first step of the config flow where the user configures
the device name, update interval, logging verbosity, extended
attributes, and recommendation interval settings.
"""

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import get_config_value


async def get_init_step_schema(config_entry: ConfigEntry | None) -> vol.Schema:
    """Return the data schema for the 'init' step."""
    return vol.Schema(
        {
            vol.Required(
                "device_name",
                default=get_config_value(config_entry, "device_name"),
            ): str,
            vol.Required(
                "hsem_update_interval",
                default=get_config_value(config_entry, "hsem_update_interval"),
            ): selector(
                {
                    "number": {
                        "min": 1,
                        "max": 59,
                        "step": 1,
                        "unit_of_measurement": UnitOfTime.MINUTES,
                        "mode": "slider",
                    }
                }
            ),
            vol.Required(
                "hsem_read_only",
                default=bool(get_config_value(config_entry, "hsem_read_only")),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_verbose_logging",
                default=bool(get_config_value(config_entry, "hsem_verbose_logging")),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_extended_attributes",
                default=bool(
                    get_config_value(config_entry, "hsem_extended_attributes")
                ),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_recommendation_interval_minutes",
                default=str(
                    get_config_value(
                        config_entry, "hsem_recommendation_interval_minutes"
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
            vol.Required(
                "hsem_recommendation_interval_length",
                default=str(
                    get_config_value(
                        config_entry, "hsem_recommendation_interval_length"
                    )
                ),
            ): selector(
                {
                    "select": {
                        "multiple": False,
                        "translation_key": "update_interval_length",
                        "mode": "list",
                        "options": [
                            "12",
                            "24",
                            "36",
                            "48",
                            "72",
                        ],
                    }
                }
            ),
        }
    )


async def validate_init_step_input(user_input: dict) -> dict[str, str]:
    """Validate user input for the 'init' step."""
    errors = {}

    required_fields = [
        "device_name",
        "hsem_read_only",
        "hsem_verbose_logging",
        "hsem_extended_attributes",
        "hsem_update_interval",
        "hsem_recommendation_interval_minutes",
        "hsem_recommendation_interval_length",
    ]

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"

    return errors

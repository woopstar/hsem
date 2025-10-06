import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import async_entity_exists, get_config_value


async def get_ev_step_schema(config_entry) -> vol.Schema:
    """Return the data schema for the 'misc' step."""
    return vol.Schema(
        {
            vol.Optional(
                "hsem_ev_charger_status",
                default=get_config_value(config_entry, "hsem_ev_charger_status"),
            ): selector(
                {
                    "entity": {
                        "domain": [
                            "sensor",
                            "switch",
                            "input_boolean",
                            "binary_sensor",
                            "button",
                        ]
                    }
                }
            ),
            vol.Optional(
                "hsem_ev_charger_power",
                default=get_config_value(config_entry, "hsem_ev_charger_power"),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_house_power_includes_ev_charger_power",
                default=get_config_value(
                    config_entry, "hsem_house_power_includes_ev_charger_power"
                ),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_ev_charger_force_max_discharge_power",
                default=get_config_value(
                    config_entry, "hsem_ev_charger_force_max_discharge_power"
                ),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_ev_charger_max_discharge_power",
                default=get_config_value(
                    config_entry, "hsem_ev_charger_max_discharge_power"
                ),
            ): selector(
                {
                    "number": {
                        "min": 50,
                        "max": 5000,
                        "step": 1,
                        "unit_of_measurement": "W",
                        "mode": "slider",
                    }
                }
            ),
            vol.Optional(
                "hsem_ev_soc",
                default=get_config_value(config_entry, "hsem_ev_soc"),
            ): selector(
                {
                    "entity": {
                        "domain": ["sensor", "switch", "input_boolean", "input_number"]
                    }
                }
            ),
            vol.Optional(
                "hsem_ev_soc_target",
                default=get_config_value(config_entry, "hsem_ev_soc_target"),
            ): selector(
                {
                    "entity": {
                        "domain": ["sensor", "switch", "input_boolean", "input_number"]
                    }
                }
            ),
            vol.Optional(
                "hsem_ev_connected",
                default=get_config_value(config_entry, "hsem_ev_connected"),
            ): selector(
                {
                    "entity": {
                        "domain": [
                            "sensor",
                            "switch",
                            "input_boolean",
                            "button",
                            "binary_sensor",
                        ]
                    }
                }
            ),
            vol.Required(
                "hsem_ev_allow_charge_past_target_soc",
                default=get_config_value(
                    config_entry, "hsem_ev_allow_charge_past_target_soc"
                ),
            ): selector({"boolean": {}}),
        }
    )


async def validate_ev_step_input(hass, user_input) -> dict[str, str]:
    """Validate user input for the 'misc' step."""
    errors = {}

    required_fields = [
        "hsem_house_power_includes_ev_charger_power",
        "hsem_ev_charger_max_discharge_power",
        "hsem_ev_charger_force_max_discharge_power",
        "hsem_ev_allow_charge_past_target_soc",
    ]

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"

    optional_entity_fields = [
        "hsem_ev_charger_status",
        "hsem_ev_charger_power",
        "hsem_ev_soc",
        "hsem_ev_soc_target",
        "hsem_ev_connected",
    ]

    for field in optional_entity_fields:
        if field in user_input:
            entity_id = user_input.get(field)
            if entity_id:
                # Tjek om entiteten eksisterer
                if not await async_entity_exists(hass, entity_id):
                    errors[field] = "entity_not_found"

    return errors

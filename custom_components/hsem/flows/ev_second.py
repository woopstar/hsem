import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import (
    async_validate_entity_ids,
    merge_errors,
)
from custom_components.hsem.utils.misc import get_config_value


async def get_ev_second_step_schema(config_entry) -> vol.Schema:
    """Return the data schema for the 'misc' step."""
    return vol.Schema(
        {
            vol.Optional(
                "hsem_ev_second_charger_status",
                default=get_config_value(config_entry, "hsem_ev_second_charger_status"),
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
                "hsem_ev_second_charger_power",
                default=get_config_value(config_entry, "hsem_ev_second_charger_power"),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_ev_second_charger_force_max_discharge_power",
                default=get_config_value(
                    config_entry, "hsem_ev_second_charger_force_max_discharge_power"
                ),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_ev_second_charger_max_discharge_power",
                default=get_config_value(
                    config_entry, "hsem_ev_second_charger_max_discharge_power"
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
                "hsem_ev_second_soc",
                default=get_config_value(config_entry, "hsem_ev_second_soc"),
            ): selector(
                {
                    "entity": {
                        "domain": ["sensor", "switch", "input_boolean", "input_number"]
                    }
                }
            ),
            vol.Optional(
                "hsem_ev_second_soc_target",
                default=get_config_value(config_entry, "hsem_ev_second_soc_target"),
            ): selector(
                {
                    "entity": {
                        "domain": ["sensor", "switch", "input_boolean", "input_number"]
                    }
                }
            ),
            vol.Optional(
                "hsem_ev_second_connected",
                default=get_config_value(config_entry, "hsem_ev_second_connected"),
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
                "hsem_ev_second_allow_charge_past_target_soc",
                default=get_config_value(
                    config_entry, "hsem_ev_second_allow_charge_past_target_soc"
                ),
            ): selector({"boolean": {}}),
        }
    )


async def validate_ev_second_step_input(hass, user_input) -> dict[str, str]:
    """Validate user input for the 'ev_second' step."""
    required_errors: dict[str, str] = {
        f: "required"
        for f in (
            "hsem_ev_second_charger_max_discharge_power",
            "hsem_ev_second_charger_force_max_discharge_power",
            "hsem_ev_second_allow_charge_past_target_soc",
        )
        if f not in user_input
    }
    entity_errors = await async_validate_entity_ids(
        hass,
        user_input,
        required_fields=[],
        optional_fields=[
            "hsem_ev_second_charger_status",
            "hsem_ev_second_charger_power",
            "hsem_ev_second_soc",
            "hsem_ev_second_soc_target",
            "hsem_ev_second_connected",
        ],
    )
    return merge_errors(required_errors, entity_errors)

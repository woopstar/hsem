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
            ): selector({"entity": {"domain": ["sensor", "switch", "input_boolean"]}}),
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
        }
    )


async def validate_ev_step_input(hass, user_input) -> dict[str, str]:
    """Validate user input for the 'misc' step."""
    errors = {}

    required_fields = [
        "hsem_house_power_includes_ev_charger_power",
    ]

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"

    optional_entity_fields = [
        "hsem_ev_charger_status",
        "hsem_ev_charger_power",
    ]

    for field in optional_entity_fields:
        if field in user_input:
            entity_id = user_input.get(field)
            if entity_id:
                # Tjek om entiteten eksisterer
                if not await async_entity_exists(hass, entity_id):
                    errors[field] = "entity_not_found"

    return errors

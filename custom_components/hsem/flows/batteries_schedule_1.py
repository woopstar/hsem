import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import validate_time_window
from custom_components.hsem.utils.misc import (
    calculate_recommended_threshold,
    convert_to_float,
    convert_to_int,
    get_config_value,
)


def _resolve_usable_capacity_kwh(
    hass,
    config_entry,
    user_input: dict | None = None,
) -> float:
    """Return the usable battery capacity in kWh for threshold preview calculations.

    Resolves the live HA state of ``hsem_huawei_solar_batteries_rated_capacity``
    (stored in Wh) and converts it to kWh.  Falls back to 10.0 kWh when the
    entity is unavailable or the state cannot be parsed.

    Priority for the entity-id string:
    1. ``config_entry`` (options flow / existing config)
    2. ``user_input`` (config flow — data from previous steps)
    3. Built-in fallback: 10.0 kWh
    """
    rated_capacity_entity = get_config_value(
        config_entry, "hsem_huawei_solar_batteries_rated_capacity"
    ) or (
        user_input.get("hsem_huawei_solar_batteries_rated_capacity")
        if user_input
        else None
    )
    if hass and rated_capacity_entity:
        state = hass.states.get(rated_capacity_entity)
        if state is not None:
            rated_wh = convert_to_float(state.state)
            if rated_wh and rated_wh > 0:
                return rated_wh / 1000.0
    # Fall back to a representative default for the UI preview.
    return 10.0


async def get_batteries_schedule_1_step_schema(
    config_entry, hass=None, user_input: dict | None = None
) -> vol.Schema:
    """Return the data schema for the 'batteries_schedule' step."""

    # Calculate recommended threshold as default if not already set
    purchase_price = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_purchase_price") or 0.0
    )
    _cycles_1 = convert_to_int(
        get_config_value(config_entry, "hsem_batteries_expected_cycles")
    )
    expected_cycles = _cycles_1 if _cycles_1 is not None else 6000
    usable_capacity = _resolve_usable_capacity_kwh(hass, config_entry, user_input)
    conversion_loss = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_conversion_loss") or 10.0
    )

    recommended = calculate_recommended_threshold(
        purchase_price, expected_cycles, usable_capacity, conversion_loss
    )

    return vol.Schema(
        {
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_1",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_1"
                ),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_1_start",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_1_start"
                ),
            ): selector({"time": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_1_end",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_1_end"
                ),
            ): selector({"time": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_1_min_price_difference",
                default=get_config_value(
                    config_entry,
                    "hsem_batteries_enable_batteries_schedule_1_min_price_difference",
                )
                or recommended,
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 5,
                        "step": 0.01,
                        "mode": "box",
                    }
                }
            ),
        }
    )


async def validate_batteries_schedule_1_input(user_input) -> dict[str, str]:
    """Validate user input for the battery schedule 1 step."""
    return validate_time_window(
        user_input,
        enabled_field="hsem_batteries_enable_batteries_schedule_1",
        start_field="hsem_batteries_enable_batteries_schedule_1_start",
        end_field="hsem_batteries_enable_batteries_schedule_1_end",
    )

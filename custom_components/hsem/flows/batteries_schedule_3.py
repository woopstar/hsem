import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.flows.batteries_schedule_1 import (
    _resolve_usable_capacity_kwh,
)
from custom_components.hsem.utils.config_validator import validate_time_window
from custom_components.hsem.utils.misc import (
    calculate_recommended_threshold,
    convert_to_float,
    convert_to_int,
    get_config_value,
)


async def get_batteries_schedule_3_step_schema(
    config_entry, hass=None, user_input: dict | None = None
) -> vol.Schema:
    """Return the data schema for the 'batteries_schedule' step."""

    # Calculate recommended threshold as default if not already set
    purchase_price = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_purchase_price") or 0.0
    )
    _cycles_3 = convert_to_int(
        get_config_value(config_entry, "hsem_batteries_expected_cycles")
    )
    expected_cycles = _cycles_3 if _cycles_3 is not None else 6000
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
                "hsem_batteries_enable_batteries_schedule_3",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_3"
                ),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_3_start",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_3_start"
                ),
            ): selector({"time": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_3_end",
                default=get_config_value(
                    config_entry, "hsem_batteries_enable_batteries_schedule_3_end"
                ),
            ): selector({"time": {}}),
            vol.Required(
                "hsem_batteries_enable_batteries_schedule_3_min_price_difference",
                default=get_config_value(
                    config_entry,
                    "hsem_batteries_enable_batteries_schedule_3_min_price_difference",
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


async def validate_batteries_schedule_3_input(user_input) -> dict[str, str]:
    """Validate user input for the battery schedule 3 step."""
    return validate_time_window(
        user_input,
        enabled_field="hsem_batteries_enable_batteries_schedule_3",
        start_field="hsem_batteries_enable_batteries_schedule_3_start",
        end_field="hsem_batteries_enable_batteries_schedule_3_end",
    )

"""Config flow step for battery economics and planner hysteresis.

This module covers battery depreciation, round-trip efficiency, and
planner anti-flapping hysteresis settings — both plan-level (issue #372)
and window-level (issue #315).
"""

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import merge_errors, validate_price
from custom_components.hsem.utils.misc import get_config_value


async def get_battery_economics_step_schema(  # NOSONAR
    config_entry: ConfigEntry | None,
) -> vol.Schema:
    """Return the data schema for the 'battery_economics' step.

    Args:
        config_entry: Existing config entry (used during options flow editing)
            or ``None`` for the initial config flow.

    Returns:
        A ``vol.Schema`` with number/selector inputs for battery economics
        and planner hysteresis.
    """
    return vol.Schema(
        {
            vol.Required(
                "hsem_batteries_purchase_price",
                default=get_config_value(config_entry, "hsem_batteries_purchase_price"),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 100000,
                        "step": 100,
                        "mode": "box",
                    }
                }
            ),
            vol.Required(
                "hsem_batteries_expected_cycles",
                default=get_config_value(
                    config_entry, "hsem_batteries_expected_cycles"
                ),
            ): selector(
                {
                    "number": {
                        "min": 1,
                        "max": 20000,
                        "step": 100,
                        "mode": "box",
                    }
                }
            ),
            vol.Required(
                "hsem_batteries_cycle_cost",
                default=get_config_value(config_entry, "hsem_batteries_cycle_cost"),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 1,
                        "step": 0.001,
                        "mode": "box",
                    }
                }
            ),
            vol.Required(
                "hsem_batteries_capacity_loss_pct",
                default=get_config_value(
                    config_entry, "hsem_batteries_capacity_loss_pct"
                ),
            ): selector(
                {
                    "number": {
                        "min": 10,
                        "max": 50,
                        "step": 1,
                        "unit_of_measurement": PERCENTAGE,
                        "mode": "slider",
                    }
                }
            ),
            vol.Required(
                "hsem_batteries_charge_efficiency",
                default=get_config_value(
                    config_entry, "hsem_batteries_charge_efficiency"
                ),
            ): selector(
                {
                    "number": {
                        "min": 50,
                        "max": 100,
                        "step": 1,
                        "unit_of_measurement": PERCENTAGE,
                        "mode": "slider",
                    }
                }
            ),
            vol.Required(
                "hsem_batteries_discharge_efficiency",
                default=get_config_value(
                    config_entry, "hsem_batteries_discharge_efficiency"
                ),
            ): selector(
                {
                    "number": {
                        "min": 50,
                        "max": 100,
                        "step": 1,
                        "unit_of_measurement": PERCENTAGE,
                        "mode": "slider",
                    }
                }
            ),
            # --- Planner hysteresis (anti-flapping, issues #315 / #372) ---
            vol.Required(
                "hsem_planner_hysteresis_enabled",
                default=get_config_value(
                    config_entry, "hsem_planner_hysteresis_enabled"
                ),
            ): selector({"boolean": {}}),
            vol.Required(
                "hsem_planner_hysteresis_absolute",
                default=get_config_value(
                    config_entry, "hsem_planner_hysteresis_absolute"
                ),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 1,
                        "step": 0.01,
                        "mode": "box",
                    }
                }
            ),
            vol.Required(
                "hsem_planner_hysteresis_percentage",
                default=get_config_value(
                    config_entry, "hsem_planner_hysteresis_percentage"
                ),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 25,
                        "step": 0.5,
                        "unit_of_measurement": PERCENTAGE,
                        "mode": "slider",
                    }
                }
            ),
            vol.Required(
                "hsem_planner_window_hysteresis_minutes",
                default=get_config_value(
                    config_entry, "hsem_planner_window_hysteresis_minutes"
                ),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 60,
                        "step": 1,
                        "mode": "box",
                    }
                }
            ),
        }
    )


async def validate_battery_economics_input(
    user_input: dict,
) -> dict[str, str]:  # NOSONAR
    """Validate user input for the 'battery_economics' step.

    Args:
        user_input: Dict of field name → value submitted by the user.

    Returns:
        Dict mapping field names to translation error keys; empty on success.
    """
    scalar_required = [
        "hsem_batteries_purchase_price",
        "hsem_batteries_expected_cycles",
        "hsem_batteries_cycle_cost",
        "hsem_batteries_capacity_loss_pct",
        "hsem_batteries_charge_efficiency",
        "hsem_batteries_discharge_efficiency",
        "hsem_planner_hysteresis_enabled",
        "hsem_planner_hysteresis_absolute",
        "hsem_planner_hysteresis_percentage",
        "hsem_planner_window_hysteresis_minutes",
    ]
    required_errors: dict[str, str] = {
        f: "required" for f in scalar_required if f not in user_input
    }

    price_errors = validate_price(
        user_input,
        "hsem_batteries_purchase_price",
        min_price=0.0,
        max_price=100_000.0,
        allow_negative=False,
    )

    return merge_errors(required_errors, price_errors)

"""Reusable schema factory and validator for battery schedule config flow steps.

All three schedule steps (1, 2, 3) share identical logic.  The only
difference is the numeric suffix embedded in the config-entry key names.
This module provides a single parameterised schema builder and a single
parameterised validator so that ``batteries_schedule_{1,2,3}.py`` each
contain only a thin numbered wrapper — removing the duplicated code.

Public API
----------
- :func:`build_batteries_schedule_step_schema` — async schema factory.
- :func:`validate_batteries_schedule_input` — async validator.
- :func:`resolve_usable_capacity_kwh` — helper used by schema factories.
"""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import validate_time_window
from custom_components.hsem.utils.misc import convert_to_float, get_config_value


def resolve_usable_capacity_kwh(
    hass: HomeAssistant | None,
    config_entry: ConfigEntry | None,
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

    Args:
        hass: Home Assistant instance (may be None during initial config).
        config_entry: Active config entry (may be None for new installs).
        user_input: Optional dict of values collected in prior flow steps.

    Returns:
        Usable battery capacity in kWh.
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
    return 10.0


async def build_batteries_schedule_step_schema(
    schedule_number: int,
    config_entry: ConfigEntry | None,
    hass: HomeAssistant | None = None,
    user_input: dict | None = None,
) -> vol.Schema:
    """Return the voluptuous schema for a numbered battery schedule step.

    Constructs schema keys by substituting *schedule_number* into the
    standard ``hsem_batteries_enable_batteries_schedule_N*`` key pattern.

    Args:
        schedule_number: Integer suffix (1, 2, or 3) identifying the schedule.
        config_entry: Active config entry; ``None`` for the initial config flow.
        hass: Home Assistant instance; ``None`` when not yet available.
        user_input: Optional dict of values from prior flow steps — used to
            resolve the rated-capacity entity during first-time setup.

    Returns:
        A ``vol.Schema`` for the ``batteries_schedule_N`` flow step.
    """
    n = schedule_number
    prefix = f"hsem_batteries_enable_batteries_schedule_{n}"

    return vol.Schema(
        {
            vol.Required(
                prefix,
                default=get_config_value(config_entry, prefix),
            ): selector({"boolean": {}}),
            vol.Required(
                f"{prefix}_start",
                default=get_config_value(config_entry, f"{prefix}_start"),
            ): selector({"time": {}}),
            vol.Required(
                f"{prefix}_end",
                default=get_config_value(config_entry, f"{prefix}_end"),
            ): selector({"time": {}}),
        }
    )


async def validate_batteries_schedule_input(
    schedule_number: int,
    user_input: dict,
) -> dict[str, str]:
    """Validate user input for a numbered battery schedule step.

    Delegates to :func:`~custom_components.hsem.utils.config_validator.validate_time_window`
    after deriving the correct field names from *schedule_number*.

    Args:
        schedule_number: Integer suffix (1, 2, or 3) identifying the schedule.
        user_input: Dict of field name → value submitted by the user.

    Returns:
        Dict mapping field names to translation error keys; empty on success.
    """
    prefix = f"hsem_batteries_enable_batteries_schedule_{schedule_number}"
    return validate_time_window(
        user_input,
        enabled_field=prefix,
        start_field=f"{prefix}_start",
        end_field=f"{prefix}_end",
    )

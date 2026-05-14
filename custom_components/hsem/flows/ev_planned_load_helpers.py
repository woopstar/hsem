"""Reusable schema factories and validators for EV planned load config flow steps.

The primary EV planned-load step (``flows/ev_planned_load.py``) and the second
EV planned-load step (``flows/ev_second_planned_load.py``) share the same field
structure.  The only difference is the field-name prefix:

- Primary:  ``hsem_ev_planned_load_``
- Secondary: ``hsem_ev_second_planned_load_``

This module parameterises both the schema builder and the validator so that
each wrapper contains only a thin delegation call.

Public API
----------
- :func:`build_ev_planned_load_schema` — async schema factory.
- :func:`validate_ev_planned_load_input` — async validator.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import (
    async_validate_entity_ids,
    merge_errors,
)
from custom_components.hsem.utils.misc import get_config_value

# Entity domains accepted for EV connected binary sensor and smart charging flag.
_BOOL_DOMAINS = ["binary_sensor", "input_boolean", "sensor", "switch"]
# Entity domains accepted for SoC, target SoC.
_SENSOR_DOMAINS = ["sensor", "input_number", "number"]
# Entity domains accepted for deadline entity.
_TIME_DOMAINS = ["input_datetime", "sensor", "input_text"]

# Shared number selectors — defined once for reuse across both EV steps.
_CAPACITY_SELECTOR = selector(
    {
        "number": {
            "min": 1.0,
            "max": 200.0,
            "step": 0.5,
            "unit_of_measurement": "kWh",
            "mode": "box",
        }
    }
)

_POWER_KW_SELECTOR = selector(
    {
        "number": {
            "min": 0.1,
            "max": 50.0,
            "step": 0.1,
            "unit_of_measurement": "kW",
            "mode": "box",
        }
    }
)

_EFFICIENCY_SELECTOR = selector(
    {
        "number": {
            "min": 50,
            "max": 100,
            "step": 1,
            "unit_of_measurement": "%",
            "mode": "slider",
        }
    }
)

_SOC_SELECTOR = selector(
    {
        "number": {
            "min": 0,
            "max": 100,
            "step": 1,
            "unit_of_measurement": "%",
            "mode": "slider",
        }
    }
)

# Optional entity field names relative to a prefix (suffix only, without trailing _)
_OPTIONAL_ENTITY_SUFFIXES = [
    "connected_sensor",
    "soc_sensor",
    "target_soc_entity",
    "deadline_entity",
    "smart_charging_entity",
    "actual_power_sensor",
]


async def build_ev_planned_load_schema(config_entry, prefix: str) -> vol.Schema:
    """Return the data schema for an EV planned load config flow step.

    Args:
        config_entry: Active config entry or ``None`` for the initial config flow.
        prefix: Field-name prefix, e.g. ``"hsem_ev_planned_load"`` or
            ``"hsem_ev_second_planned_load"``.

    Returns:
        A ``vol.Schema`` for the EV planned load step.
    """

    def _k(suffix: str) -> str:
        return f"{prefix}_{suffix}"

    def _v(suffix: str):
        return get_config_value(config_entry, _k(suffix))

    return vol.Schema(
        {
            vol.Required(
                _k("enabled"),
                default=_v("enabled"),
            ): selector({"boolean": {}}),
            vol.Optional(
                _k("connected_sensor"),
                default=_v("connected_sensor"),
            ): selector({"entity": {"domain": _BOOL_DOMAINS}}),
            vol.Optional(
                _k("soc_sensor"),
                default=_v("soc_sensor"),
            ): selector({"entity": {"domain": _SENSOR_DOMAINS}}),
            vol.Optional(
                _k("target_soc_entity"),
                default=_v("target_soc_entity"),
            ): selector({"entity": {"domain": _SENSOR_DOMAINS}}),
            vol.Required(
                _k("target_soc_fixed"),
                default=_v("target_soc_fixed"),
            ): _SOC_SELECTOR,
            vol.Optional(
                _k("deadline_entity"),
                default=_v("deadline_entity"),
            ): selector({"entity": {"domain": _TIME_DOMAINS}}),
            vol.Required(
                _k("deadline_fixed"),
                default=_v("deadline_fixed"),
            ): selector({"text": {}}),
            vol.Optional(
                _k("smart_charging_entity"),
                default=_v("smart_charging_entity"),
            ): selector({"entity": {"domain": _BOOL_DOMAINS}}),
            vol.Required(
                _k("battery_capacity_kwh"),
                default=_v("battery_capacity_kwh"),
            ): _CAPACITY_SELECTOR,
            vol.Required(
                _k("charger_power_kw"),
                default=_v("charger_power_kw"),
            ): _POWER_KW_SELECTOR,
            vol.Required(
                _k("charger_efficiency"),
                default=_v("charger_efficiency"),
            ): _EFFICIENCY_SELECTOR,
            vol.Required(
                _k("base_load_includes_ev"),
                default=_v("base_load_includes_ev"),
            ): selector({"boolean": {}}),
            vol.Optional(
                _k("actual_power_sensor"),
                default=_v("actual_power_sensor"),
            ): selector({"entity": {"domain": _SENSOR_DOMAINS}}),
        }
    )


async def validate_ev_planned_load_schema_input(
    hass, user_input: dict, prefix: str
) -> dict[str, str]:
    """Validate user input for an EV planned load flow step.

    When the feature is disabled (``{prefix}_enabled`` is False), validation
    is skipped — the planner ignores all EV planned load fields.

    Args:
        hass: The Home Assistant instance.
        user_input: Submitted form data.
        prefix: Field-name prefix, e.g. ``"hsem_ev_planned_load"`` or
            ``"hsem_ev_second_planned_load"``.

    Returns:
        A mapping of ``{field_key: error_code}`` for any validation failures.
    """
    if not user_input.get(f"{prefix}_enabled", False):
        return {}

    errors: dict[str, str] = {}
    for suffix in _OPTIONAL_ENTITY_SUFFIXES:
        field = f"{prefix}_{suffix}"
        val = user_input.get(field)
        if val and str(val).strip():
            entity_errors = await async_validate_entity_ids(hass, {field: val}, [field])
            merge_errors(errors, entity_errors)
    return errors

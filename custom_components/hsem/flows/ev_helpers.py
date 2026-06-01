"""Reusable schema factories and validators for EV config flow steps.

The primary EV step (``flows/ev.py``) and the second EV step
(``flows/ev_second.py``) share near-identical structure.  The differences are:

- Field prefix: ``hsem_ev_`` vs ``hsem_ev_second_``.
- The primary step has two extra top-level fields
  (``hsem_ev_second_enabled`` and
  ``hsem_house_power_includes_ev_charger_power``).
- The required-fields list for validation differs slightly.

This module parameterises both the schema builder and the validator so that
``ev.py`` and ``ev_second.py`` each contain only a thin wrapper.

Public API
----------
- :func:`build_ev_charger_schema` — async schema factory for one EV charger.
- :func:`validate_ev_charger_input` — async validator for one EV charger.
"""

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import (
    async_validate_entity_ids,
    merge_errors,
)
from custom_components.hsem.utils.misc import get_config_value

# Domain lists reused for multiple fields — defined once to avoid repetition.
_STATUS_DOMAINS = ["sensor", "switch", "input_boolean", "binary_sensor", "button"]
_SOC_DOMAINS = ["sensor", "switch", "input_boolean", "input_number"]
_CONNECTED_DOMAINS = ["sensor", "switch", "input_boolean", "button", "binary_sensor"]

# Discharge-power slider selector — identical for both EV steps.
_DISCHARGE_POWER_SELECTOR = selector(
    {
        "number": {
            "min": 50,
            "max": 5000,
            "step": 1,
            "unit_of_measurement": UnitOfPower.WATT,
            "mode": "slider",
        }
    }
)


async def build_ev_charger_schema(  # NOSONAR -- async required by HA config/options flow framework
    config_entry: ConfigEntry | None,
    prefix: str,
    include_primary_fields: bool = False,
) -> vol.Schema:
    """Return the voluptuous schema for an EV charger config flow step.

    Args:
        config_entry: Active config entry; ``None`` for the initial config flow.
        prefix: Field-name prefix, e.g. ``"hsem_ev"`` or ``"hsem_ev_second"``.
        include_primary_fields: When ``True``, adds the two fields that only
            appear in the primary EV step —
            ``hsem_ev_second_enabled`` (enables the second-EV flow step) and
            ``hsem_house_power_includes_ev_charger_power``.

    Returns:
        A ``vol.Schema`` for the EV charger flow step.
    """
    fields: dict = {}

    if include_primary_fields:
        fields[
            vol.Required(
                "hsem_ev_second_enabled",
                default=get_config_value(config_entry, "hsem_ev_second_enabled"),
            )
        ] = selector({"boolean": {}})

    fields[
        vol.Optional(
            f"{prefix}_charger_status",
            default=get_config_value(config_entry, f"{prefix}_charger_status"),
        )
    ] = selector({"entity": {"domain": _STATUS_DOMAINS}})

    fields[
        vol.Optional(
            f"{prefix}_charger_power",
            default=get_config_value(config_entry, f"{prefix}_charger_power"),
        )
    ] = selector({"entity": {"domain": "sensor"}})

    if include_primary_fields:
        fields[
            vol.Required(
                "hsem_house_power_includes_ev_charger_power",
                default=get_config_value(
                    config_entry, "hsem_house_power_includes_ev_charger_power"
                ),
            )
        ] = selector({"boolean": {}})

    fields[
        vol.Required(
            f"{prefix}_charger_force_max_discharge_power",
            default=get_config_value(
                config_entry, f"{prefix}_charger_force_max_discharge_power"
            ),
        )
    ] = selector({"boolean": {}})

    fields[
        vol.Required(
            f"{prefix}_charger_max_discharge_power",
            default=get_config_value(
                config_entry, f"{prefix}_charger_max_discharge_power"
            ),
        )
    ] = _DISCHARGE_POWER_SELECTOR

    fields[
        vol.Optional(
            f"{prefix}_soc",
            default=get_config_value(config_entry, f"{prefix}_soc"),
        )
    ] = selector({"entity": {"domain": _SOC_DOMAINS}})

    fields[
        vol.Optional(
            f"{prefix}_connected",
            default=get_config_value(config_entry, f"{prefix}_connected"),
        )
    ] = selector({"entity": {"domain": _CONNECTED_DOMAINS}})

    fields[
        vol.Required(
            f"{prefix}_allow_charge_past_target_soc",
            default=get_config_value(
                config_entry, f"{prefix}_allow_charge_past_target_soc"
            ),
        )
    ] = selector({"boolean": {}})

    return vol.Schema(fields)


async def validate_ev_charger_input(
    hass: HomeAssistant,
    user_input: dict,
    prefix: str,
    extra_required_fields: list[str] | None = None,
) -> dict[str, str]:
    """Validate user input for an EV charger config flow step.

    Checks that required boolean/numeric fields are present and that any
    supplied entity IDs resolve to existing HA entities.

    Args:
        hass: Home Assistant instance used for entity existence lookups.
        user_input: Dict of field name → value submitted by the user.
        prefix: Field-name prefix, e.g. ``"hsem_ev"`` or ``"hsem_ev_second"``.
        extra_required_fields: Additional required field names beyond the
            standard set derived from *prefix*.  Use this for fields that
            only exist in the primary EV step (e.g.
            ``"hsem_house_power_includes_ev_charger_power"``).

    Returns:
        Dict mapping field names to translation error keys; empty on success.
    """
    standard_required = [
        f"{prefix}_charger_max_discharge_power",
        f"{prefix}_charger_force_max_discharge_power",
        f"{prefix}_allow_charge_past_target_soc",
    ]
    all_required = standard_required + (extra_required_fields or [])

    required_errors: dict[str, str] = {
        f: "required" for f in all_required if f not in user_input
    }

    entity_errors = await async_validate_entity_ids(
        hass,
        user_input,
        required_fields=[],
        optional_fields=[
            f"{prefix}_charger_status",
            f"{prefix}_charger_power",
            f"{prefix}_soc",
            f"{prefix}_connected",
        ],
    )
    return merge_errors(required_errors, entity_errors)

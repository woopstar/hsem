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

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import (
    merge_errors,
    validate_energy_limits,
    validate_power_limits,
)
from custom_components.hsem.utils.misc import get_config_value
from custom_components.hsem.utils.phase_power import EV_PHASE_TOPOLOGIES

# Shared number selectors — defined once for reuse across both EV steps.
_CAPACITY_SELECTOR = selector(
    {
        "number": {
            "min": 0.0,
            "max": 200.0,
            "step": 0.5,
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
            "mode": "box",
        }
    }
)

_POWER_KW_SELECTOR = selector(
    {
        "number": {
            "min": 0.0,
            "max": 50.0,
            "step": 0.1,
            "unit_of_measurement": UnitOfPower.KILO_WATT,
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
            "unit_of_measurement": PERCENTAGE,
            "mode": "slider",
        }
    }
)

_MIN_POWER_W_SELECTOR = selector(
    {
        "number": {
            "min": 0,
            "max": 22000,
            "step": 10,
            "unit_of_measurement": UnitOfPower.WATT,
            "mode": "box",
        }
    }
)

# Extra energy budgeted above the deadline target so normal execution-layer
# friction doesn't turn an exact plan into a missed deadline (issue #845).
_DEADLINE_SAFETY_MARGIN_SELECTOR = selector(
    {
        "number": {
            "min": 0,
            "max": 50,
            "step": 1,
            "unit_of_measurement": PERCENTAGE,
            "mode": "slider",
        }
    }
)

# Minimum amp change the planner must ask for before the live charger command
# is actually moved.  The MILP re-derives the live slot's command from scratch
# every solve and competing whole-amp splits are often within a rounding error
# of each other on cost, so without a deadband the charger is re-commanded
# every few minutes for no economic gain.
_COMMAND_DEADBAND_SELECTOR = selector(
    {
        "number": {
            "min": 0,
            "max": 5,
            "step": 1,
            "unit_of_measurement": UnitOfElectricCurrent.AMPERE,
            "mode": "slider",
        }
    }
)

# Tail of a slot during which a zero command is suppressed while the EV still
# has unmet energy need.  A few seconds of remaining slot cannot hold enough
# energy to clear the charger minimum, so the plan legitimately allocates it
# nothing -- but publishing 0 W stops the session and the restart handshake
# costs more than the stub was ever worth.
_STUB_FLOOR_MINUTES_SELECTOR = selector(
    {
        "number": {
            "min": 0,
            "max": 10,
            "step": 1,
            "unit_of_measurement": UnitOfTime.MINUTES,
            "mode": "slider",
        }
    }
)

# Charger phase topology decides how much of an EV command any single phase
# may be assumed to carry in the hard per-phase fuse rows.  ``single_phase``
# is the safe default: it keeps the pre-feature worst-case envelope.
_PHASE_TOPOLOGY_SELECTOR = selector(
    {
        "select": {
            "multiple": False,
            "translation_key": "ev_charger_phase_topology",
            "mode": "list",
            "options": list(EV_PHASE_TOPOLOGIES),
        }
    }
)


async def build_ev_planned_load_schema(  # NOSONAR
    config_entry: ConfigEntry | None, prefix: str
) -> vol.Schema:
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

    def _v(suffix: str) -> Any:  # NOSONAR -- generic helper; type depends on caller
        return get_config_value(config_entry, _k(suffix))

    return vol.Schema(
        {
            vol.Required(
                _k("enabled"),
                default=_v("enabled"),
            ): selector({"boolean": {}}),
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
                _k("charger_min_power_w"),
                default=_v("charger_min_power_w"),
            ): _MIN_POWER_W_SELECTOR,
            vol.Required(
                _k("charger_phase_topology"),
                default=_v("charger_phase_topology"),
            ): _PHASE_TOPOLOGY_SELECTOR,
            vol.Required(
                _k("deadline_safety_margin_pct"),
                default=_v("deadline_safety_margin_pct"),
            ): _DEADLINE_SAFETY_MARGIN_SELECTOR,
            vol.Required(
                _k("command_deadband_a"),
                default=_v("command_deadband_a"),
            ): _COMMAND_DEADBAND_SELECTOR,
            vol.Required(
                _k("stub_floor_minutes"),
                default=_v("stub_floor_minutes"),
            ): _STUB_FLOOR_MINUTES_SELECTOR,
        }
    )


async def validate_ev_planned_load_schema_input(
    user_input: dict, prefix: str
) -> dict[str, str]:
    """Validate user input for an EV planned load flow step.

    When the feature is disabled (``{prefix}_enabled`` is False), validation
    is skipped — the planner ignores all EV planned load fields.

    Args:
        user_input: Submitted form data.
        prefix: Field-name prefix, e.g. ``"hsem_ev_planned_load"`` or
            ``"hsem_ev_second_planned_load"``.

    Returns:
        A mapping of ``{field_key: error_code}`` for any validation failures.
    """
    if not user_input.get(f"{prefix}_enabled", False):
        return {}

    capacity_errors = validate_energy_limits(
        user_input,
        f"{prefix}_battery_capacity_kwh",
        min_kwh=0.0,
        max_kwh=200.0,
    )
    min_power_errors = validate_power_limits(
        user_input,
        f"{prefix}_charger_min_power_w",
        min_watts=0.0,
        max_watts=22_000.0,
    )

    return merge_errors(capacity_errors, min_power_errors)

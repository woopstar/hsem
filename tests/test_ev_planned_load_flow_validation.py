"""Tests for EV planned load config/options flow validation (issue #891).

``validate_ev_planned_load_schema_input`` used to be a no-op stub that
always returned ``{}`` once the step was enabled, so out-of-range
``battery_capacity_kwh``/``charger_min_power_w`` values submitted through
the config or options flow were silently accepted even though
``validate_power_limits``/``validate_energy_limits`` existed in
``utils/config_validator.py`` for exactly this purpose. These tests cover
the real validation now wired in for both the primary and second EV
planned-load steps.
"""

from __future__ import annotations

import pytest

from custom_components.hsem.flows.ev_planned_load_helpers import (
    validate_ev_planned_load_schema_input,
)

_PREFIXES = ["hsem_ev_planned_load", "hsem_ev_second_planned_load"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", _PREFIXES)
async def test_disabled_step_skips_validation(prefix: str) -> None:
    """Disabled steps are not validated, regardless of field contents."""
    user_input = {
        f"{prefix}_enabled": False,
        f"{prefix}_battery_capacity_kwh": -50.0,
        f"{prefix}_charger_min_power_w": -1.0,
    }
    errors = await validate_ev_planned_load_schema_input(user_input, prefix)
    assert errors == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", _PREFIXES)
async def test_valid_values_pass(prefix: str) -> None:
    """In-range capacity and charger minimum power produce no errors."""
    user_input = {
        f"{prefix}_enabled": True,
        f"{prefix}_battery_capacity_kwh": 75.0,
        f"{prefix}_charger_min_power_w": 1400.0,
    }
    errors = await validate_ev_planned_load_schema_input(user_input, prefix)
    assert errors == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", _PREFIXES)
async def test_battery_capacity_out_of_range_is_rejected(prefix: str) -> None:
    """battery_capacity_kwh must stay within the 0-200 kWh selector bounds."""
    user_input = {
        f"{prefix}_enabled": True,
        f"{prefix}_battery_capacity_kwh": 250.0,
        f"{prefix}_charger_min_power_w": 1400.0,
    }
    errors = await validate_ev_planned_load_schema_input(user_input, prefix)
    assert errors[f"{prefix}_battery_capacity_kwh"] == "energy_out_of_range"


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", _PREFIXES)
async def test_charger_min_power_out_of_range_is_rejected(prefix: str) -> None:
    """charger_min_power_w must stay within the 0-22000 W selector bounds."""
    user_input = {
        f"{prefix}_enabled": True,
        f"{prefix}_battery_capacity_kwh": 75.0,
        f"{prefix}_charger_min_power_w": 25_000.0,
    }
    errors = await validate_ev_planned_load_schema_input(user_input, prefix)
    assert errors[f"{prefix}_charger_min_power_w"] == "power_out_of_range"


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", _PREFIXES)
async def test_both_fields_out_of_range_reports_both_errors(prefix: str) -> None:
    """Both fields are validated independently in a single call."""
    user_input = {
        f"{prefix}_enabled": True,
        f"{prefix}_battery_capacity_kwh": -1.0,
        f"{prefix}_charger_min_power_w": -1.0,
    }
    errors = await validate_ev_planned_load_schema_input(user_input, prefix)
    assert errors[f"{prefix}_battery_capacity_kwh"] == "energy_out_of_range"
    assert errors[f"{prefix}_charger_min_power_w"] == "power_out_of_range"


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", _PREFIXES)
async def test_default_min_power_on_three_phase_topology_is_rejected(
    prefix: str,
) -> None:
    """The documented 1380 W single-phase default computes to 2A on 3-phase.

    Reproduces issue #968: a user who switches ``charger_phase_topology`` to
    ``three_phase_balanced`` without also raising ``charger_min_power_w``
    would previously save a config that silently commands 2 A -- below any
    real EVSE's minimum start current -- and the charger refuses to charge.
    """
    user_input = {
        f"{prefix}_enabled": True,
        f"{prefix}_battery_capacity_kwh": 75.0,
        f"{prefix}_charger_min_power_w": 1380.0,
        f"{prefix}_charger_phase_topology": "three_phase_balanced",
    }
    errors = await validate_ev_planned_load_schema_input(user_input, prefix)
    assert errors[f"{prefix}_charger_min_power_w"] == "ev_min_power_below_start_current"


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", _PREFIXES)
async def test_min_power_raised_for_three_phase_topology_passes(prefix: str) -> None:
    """4140 W (6 A x 230 V x 3 phases) is the correct three-phase minimum."""
    user_input = {
        f"{prefix}_enabled": True,
        f"{prefix}_battery_capacity_kwh": 75.0,
        f"{prefix}_charger_min_power_w": 4140.0,
        f"{prefix}_charger_phase_topology": "three_phase_balanced",
    }
    errors = await validate_ev_planned_load_schema_input(user_input, prefix)
    assert errors == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", _PREFIXES)
async def test_default_min_power_on_single_phase_topology_passes(prefix: str) -> None:
    """The documented default is correct for the (default) single-phase case."""
    user_input = {
        f"{prefix}_enabled": True,
        f"{prefix}_battery_capacity_kwh": 75.0,
        f"{prefix}_charger_min_power_w": 1380.0,
        f"{prefix}_charger_phase_topology": "single_phase",
    }
    errors = await validate_ev_planned_load_schema_input(user_input, prefix)
    assert errors == {}

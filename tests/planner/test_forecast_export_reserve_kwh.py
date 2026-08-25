"""Regression tests for the forecast-export-reserve kWh conversion (issue #807).

``_forecast_export_reserve_kwh()`` converts the configured
``hsem_batteries_forecast_reserve_pct`` (absolute SoC points above the Huawei
hardware end-of-discharge floor) into a model kWh value, while making sure the
dynamic discharge floor is never double-counted and the result never exceeds
the model's usable capacity.
"""

from __future__ import annotations

import pytest

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.candidate_generator import (
    _forecast_export_reserve_kwh,
)


def _input(**overrides: object) -> PlannerInput:
    inp = PlannerInput()
    for key, value in overrides.items():
        setattr(inp, key, value)
    return inp


def test_disabled_by_default_returns_zero() -> None:
    """battery_forecast_reserve_pct=0 (default) protects nothing."""
    inp = _input(
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
    )
    assert _forecast_export_reserve_kwh(inp, usable_kwh=9.0) == pytest.approx(0.0)


def test_configured_pct_converted_to_kwh_above_hardware_floor() -> None:
    """10 SoC points on a 10 kWh battery reserves 1.0 kWh above the hardware floor."""
    inp = _input(
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
        battery_forecast_reserve_pct=10.0,
    )
    assert _forecast_export_reserve_kwh(inp, usable_kwh=9.0) == pytest.approx(1.0)


def test_dynamic_floor_overlap_is_not_double_counted() -> None:
    """A dynamic floor already above the hardware floor shrinks the remaining reserve.

    Hardware floor 10%, dynamic floor already raised to 15%, configured
    reserve target is hardware_floor + 10% = 20%. Only the remaining 5 points
    (20% - 15%) between the dynamic floor and the target must be protected —
    the 5 points already covered by the dynamic floor are not reserved twice.
    """
    inp = _input(
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
        battery_forecast_reserve_pct=10.0,
        dynamic_discharge_floor_pct=15.0,
    )
    assert _forecast_export_reserve_kwh(inp, usable_kwh=9.0) == pytest.approx(0.5)


def test_dynamic_floor_at_or_above_target_reserves_nothing() -> None:
    """When the dynamic floor already meets or exceeds the target, reserve is zero."""
    inp = _input(
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
        battery_forecast_reserve_pct=10.0,
        dynamic_discharge_floor_pct=25.0,
    )
    assert _forecast_export_reserve_kwh(inp, usable_kwh=9.0) == pytest.approx(0.0)


def test_result_clamped_to_usable_capacity() -> None:
    """The reserve can never exceed the model's usable capacity."""
    inp = _input(
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=0.0,
        battery_max_soc_pct=100.0,
        battery_forecast_reserve_pct=50.0,
    )
    assert _forecast_export_reserve_kwh(inp, usable_kwh=2.0) == pytest.approx(2.0)


def test_target_clamped_to_maximum_soc() -> None:
    """The target SoC cannot exceed the configured maximum SoC."""
    inp = _input(
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=90.0,
        battery_max_soc_pct=95.0,
        battery_forecast_reserve_pct=50.0,
    )
    # Target would be 90 + 50 = 140%, clamped to max_soc 95% -> 0.5 kWh reserve.
    assert _forecast_export_reserve_kwh(inp, usable_kwh=9.0) == pytest.approx(0.5)


def test_zero_usable_capacity_returns_zero() -> None:
    """No reserve is computed when the model has no usable capacity."""
    inp = _input(
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
        battery_forecast_reserve_pct=10.0,
    )
    assert _forecast_export_reserve_kwh(inp, usable_kwh=0.0) == pytest.approx(0.0)


def test_zero_rated_capacity_returns_zero() -> None:
    """No reserve is computed when the battery has no rated capacity."""
    inp = _input(
        battery_rated_capacity_kwh=0.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
        battery_forecast_reserve_pct=10.0,
    )
    assert _forecast_export_reserve_kwh(inp, usable_kwh=9.0) == pytest.approx(0.0)


def test_negative_configured_pct_treated_as_zero() -> None:
    """A negative percentage (shouldn't happen post-validation) is clamped to 0."""
    inp = _input(
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
        battery_forecast_reserve_pct=-5.0,
    )
    assert _forecast_export_reserve_kwh(inp, usable_kwh=9.0) == pytest.approx(0.0)


def test_pct_above_fifty_is_clamped() -> None:
    """A configured percentage above the 50% UI ceiling is clamped defensively."""
    inp = _input(
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=0.0,
        battery_max_soc_pct=100.0,
        battery_forecast_reserve_pct=999.0,
    )
    # Clamped to 50% -> 5.0 kWh, well within usable capacity.
    assert _forecast_export_reserve_kwh(inp, usable_kwh=9.0) == pytest.approx(5.0)

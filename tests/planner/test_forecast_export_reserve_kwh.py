"""Regression tests for the forecast-export-reserve kWh conversion (issue #807).

``_forecast_export_reserve_kwh()`` converts the configured
``hsem_batteries_forecast_reserve_pct`` (absolute SoC points above the Huawei
hardware end-of-discharge floor) into a model kWh value above that floor, which
is the model origin (issue #1188), and makes sure the result never exceeds the
model's usable capacity.
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


@pytest.mark.parametrize(
    ("dynamic_floor_pct", "soc_pct"),
    [
        pytest.param(15.0, 50.0, id="floor_below_the_target"),
        pytest.param(25.0, 50.0, id="floor_above_the_target"),
        pytest.param(75.74, 11.0, id="floor_not_reached_issue_1094"),
    ],
)
def test_dynamic_floor_does_not_move_the_reserve(
    dynamic_floor_pct: float, soc_pct: float
) -> None:
    """The reserve is measured from the hardware floor whatever the dynamic floor.

    Hardware floor 10 %, configured reserve 10 points: the protected range is
    10-20 % absolute, 1.0 kWh above the model origin.  Since issue #1188 the
    origin is always the hardware floor and the dynamic floor is a separate
    per-slot bound (``PlannedSlot.discharge_reserve_kwh``), so the two cannot
    be counted twice: both bound the same absolute SoC and the higher binds.
    """
    inp = _input(
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
        battery_forecast_reserve_pct=10.0,
        dynamic_discharge_floor_pct=dynamic_floor_pct,
        battery_soc_pct=soc_pct,
    )
    assert _forecast_export_reserve_kwh(inp, usable_kwh=9.0) == pytest.approx(1.0)


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

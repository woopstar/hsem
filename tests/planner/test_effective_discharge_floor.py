"""Regression tests for ``_resolve_effective_discharge_floor_pct`` (issue #807).

The dynamic discharge floor, Huawei's hardware end-of-discharge floor, and the
configured maximum SoC all share the same absolute-SoC frame. Before this fix,
a stale or oversized dynamic-floor estimate could produce an "effective" floor
above the battery's own ceiling, which would make ``usable_capacity`` and the
MILP's SoC bounds internally inconsistent. This normalizes the three limits so
the effective floor is always finite and bounded between the hardware floor
and the maximum SoC.
"""

from __future__ import annotations

import pytest

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.engine_core import (
    _resolve_effective_discharge_floor_pct,
)


def _input(**overrides: object) -> PlannerInput:
    inp = PlannerInput()
    for key, value in overrides.items():
        setattr(inp, key, value)
    return inp


def test_no_dynamic_floor_returns_hardware_floor_as_effective() -> None:
    """With no dynamic floor configured, effective == hardware floor."""
    inp = _input(
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
        dynamic_discharge_floor_pct=None,
    )
    hardware, effective, maximum = _resolve_effective_discharge_floor_pct(inp)
    assert (hardware, effective, maximum) == pytest.approx((10.0, 10.0, 100.0))


def test_dynamic_floor_above_hardware_floor_raises_effective_floor() -> None:
    """A dynamic floor above the hardware floor becomes the effective floor."""
    inp = _input(
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
        dynamic_discharge_floor_pct=25.0,
    )
    hardware, effective, maximum = _resolve_effective_discharge_floor_pct(inp)
    assert (hardware, effective, maximum) == pytest.approx((10.0, 25.0, 100.0))


def test_dynamic_floor_below_hardware_floor_is_ignored() -> None:
    """A dynamic floor below the hardware floor never lowers the effective floor."""
    inp = _input(
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
        dynamic_discharge_floor_pct=2.0,
    )
    hardware, effective, maximum = _resolve_effective_discharge_floor_pct(inp)
    assert (hardware, effective, maximum) == pytest.approx((10.0, 10.0, 100.0))


def test_stale_dynamic_floor_above_ceiling_is_clamped_to_maximum_soc() -> None:
    """A stale/oversized dynamic-floor estimate cannot exceed the battery ceiling.

    This is the latent bug fixed by this function: previously the effective
    floor was set directly to ``dynamic_discharge_floor_pct`` with no upper
    bound, so a value above ``battery_max_soc_pct`` created an impossible
    floor (min_soc > max_soc) for the cost model and MILP bounds.
    """
    inp = _input(
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=90.0,
        dynamic_discharge_floor_pct=150.0,
    )
    hardware, effective, maximum = _resolve_effective_discharge_floor_pct(inp)
    assert hardware == pytest.approx(10.0)
    assert maximum == pytest.approx(90.0)
    assert effective == pytest.approx(90.0)
    assert effective <= maximum


def test_hardware_floor_is_clamped_to_valid_percent_range() -> None:
    """An out-of-range configured hardware floor is clamped to [0, 100]."""
    inp = _input(
        battery_end_of_discharge_soc_pct=150.0,
        battery_max_soc_pct=100.0,
        dynamic_discharge_floor_pct=None,
    )
    hardware, effective, maximum = _resolve_effective_discharge_floor_pct(inp)
    assert hardware == pytest.approx(100.0)
    assert effective == pytest.approx(100.0)


def test_non_finite_dynamic_floor_falls_back_to_hardware_floor() -> None:
    """A non-finite dynamic floor (NaN/inf) must not poison the effective floor."""
    inp = _input(
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
        dynamic_discharge_floor_pct=float("nan"),
    )
    hardware, effective, maximum = _resolve_effective_discharge_floor_pct(inp)
    assert (hardware, effective, maximum) == pytest.approx((10.0, 10.0, 100.0))


def test_maximum_soc_never_drops_below_hardware_floor() -> None:
    """A misconfigured max SoC below the hardware floor is raised to match it."""
    inp = _input(
        battery_end_of_discharge_soc_pct=20.0,
        battery_max_soc_pct=5.0,
        dynamic_discharge_floor_pct=None,
    )
    hardware, effective, maximum = _resolve_effective_discharge_floor_pct(inp)
    assert hardware == pytest.approx(20.0)
    assert maximum >= hardware

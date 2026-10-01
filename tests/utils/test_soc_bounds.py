"""Tests for utils.soc_bounds — the absolute-SoC origin of the planner model.

Issue #1094: a dynamic discharge floor above the live SoC became the model
origin, so the plan reported the battery at the unreached floor (75.74 %)
with 0.0 kWh while the inverter read 11 %.  The resolver now caps the
dynamic floor at the live SoC without ever lowering the hardware floor.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.hsem.utils.soc_bounds import (
    finite_or,
    reserve_floor_pct,
    resolve_soc_bounds_pct,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(42.5, 42.5, id="float"),
        pytest.param(7, 7.0, id="int"),
        pytest.param("12.5", 12.5, id="numeric_text"),
        pytest.param(0.0, 0.0, id="genuine_zero_survives"),
        pytest.param(None, -1.0, id="none"),
        pytest.param("not a number", -1.0, id="text"),
        pytest.param(float("nan"), -1.0, id="nan"),
        pytest.param(float("inf"), -1.0, id="inf"),
        pytest.param(float("-inf"), -1.0, id="negative_inf"),
    ],
)
def test_finite_or(value: Any, expected: float) -> None:
    """Only finite numbers pass through; everything else yields the fallback."""
    assert finite_or(value, -1.0) == pytest.approx(expected)


def test_reporter_case_caps_dynamic_floor_at_live_soc() -> None:
    """The #1094 numbers: an unreached 75.74 % floor becomes the 11 % live SoC."""
    hardware, effective, maximum = resolve_soc_bounds_pct(5.0, 100.0, 75.74, 11.0)
    assert (hardware, effective, maximum) == pytest.approx((5.0, 11.0, 100.0))


def test_live_soc_above_dynamic_floor_keeps_the_floor() -> None:
    """A floor the battery has already reached is left untouched."""
    _hardware, effective, _maximum = resolve_soc_bounds_pct(5.0, 100.0, 30.0, 80.0)
    assert effective == pytest.approx(30.0)


def test_live_soc_below_hardware_floor_never_lowers_it() -> None:
    """The cap applies to the dynamic floor only — the hardware floor is hard."""
    hardware, effective, _maximum = resolve_soc_bounds_pct(10.0, 100.0, 60.0, 4.0)
    assert hardware == pytest.approx(10.0)
    assert effective == pytest.approx(10.0)


def test_live_soc_does_not_raise_the_floor_without_a_dynamic_floor() -> None:
    """With the dynamic floor disabled the live SoC has no effect at all."""
    _hardware, effective, _maximum = resolve_soc_bounds_pct(10.0, 100.0, None, 80.0)
    assert effective == pytest.approx(10.0)


@pytest.mark.parametrize(
    "soc",
    [
        pytest.param(None, id="none"),
        pytest.param(float("nan"), id="nan"),
        pytest.param("unavailable", id="text"),
    ],
)
def test_unknown_live_soc_leaves_the_dynamic_floor_uncapped(soc: Any) -> None:
    """A missing SoC reading must not collapse the reserve to the hardware floor."""
    _hardware, effective, _maximum = resolve_soc_bounds_pct(10.0, 100.0, 40.0, soc)
    assert effective == pytest.approx(40.0)


@pytest.mark.parametrize(
    ("dynamic", "soc"),
    [
        (75.74, 11.0),
        (150.0, 95.0),
        (150.0, 40.0),
        (2.0, 50.0),
        (None, 0.0),
        (60.0, 100.0),
    ],
)
def test_bounds_are_always_ordered(dynamic: float | None, soc: float) -> None:
    """hardware <= effective <= maximum holds for any dynamic floor and SoC."""
    hardware, effective, maximum = resolve_soc_bounds_pct(10.0, 90.0, dynamic, soc)
    assert hardware <= effective <= maximum


class TestReserveFloorPct:
    """Issue #1221: a reserve is held on top of the configured minimum SoC."""

    @pytest.mark.parametrize(
        ("reserve_kwh", "expected_pct"),
        [
            pytest.param(1.38, 18.8, id="1.2 kWh bridge"),
            pytest.param(1.955, 24.55, id="1.7 kWh bridge"),
            pytest.param(3.795, 42.95, id="3.3 kWh bridge"),
            pytest.param(7.36, 78.6, id="6.4 kWh bridge"),
        ],
    )
    def test_ten_kwh_battery_with_a_five_percent_floor(
        self, reserve_kwh: float, expected_pct: float
    ) -> None:
        """9.5 kWh usable between 5 % and 100 %: ten SoC points per kWh."""
        floor_pct = reserve_floor_pct(reserve_kwh, 9.5, 5.0, 100.0)

        assert floor_pct == pytest.approx(expected_pct)
        # What the planner turns it back into: rated × (floor − hardware) / 100.
        assert 10.0 * (floor_pct - 5.0) / 100.0 == pytest.approx(reserve_kwh)

    def test_no_reserve_is_the_configured_minimum(self) -> None:
        assert reserve_floor_pct(0.0, 9.5, 5.0, 100.0) == pytest.approx(5.0)
        assert reserve_floor_pct(-1.0, 9.5, 5.0, 100.0) == pytest.approx(5.0)

    def test_a_reserve_beyond_the_battery_is_the_maximum(self) -> None:
        assert reserve_floor_pct(12.0, 9.5, 5.0, 100.0) == pytest.approx(100.0)
        assert reserve_floor_pct(12.0, 8.5, 5.0, 90.0) == pytest.approx(90.0)

    def test_a_reduced_maximum_keeps_the_energy(self) -> None:
        """8.5 kWh usable between 5 % and 90 % is still a 10 kWh battery."""
        assert reserve_floor_pct(3.795, 8.5, 5.0, 90.0) == pytest.approx(42.95)

    @pytest.mark.parametrize(
        ("usable_kwh", "minimum_pct", "maximum_pct"),
        [
            pytest.param(0.0, 5.0, 100.0, id="no usable capacity"),
            pytest.param(9.5, 100.0, 100.0, id="no span"),
            pytest.param(9.5, 60.0, 50.0, id="minimum above maximum"),
        ],
    )
    def test_without_capacity_the_floor_is_the_configured_minimum(
        self, usable_kwh: float, minimum_pct: float, maximum_pct: float
    ) -> None:
        assert reserve_floor_pct(
            3.0, usable_kwh, minimum_pct, maximum_pct
        ) == pytest.approx(minimum_pct)

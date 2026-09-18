"""Tests for the MILP solver's pre- and post-solve refusals.

``solve_milp`` returns ``None`` rather than a degraded plan whenever it cannot
trust its own result: scipy missing, nothing to optimise, a bounds layout that
does not match the variable count, or a solution whose battery inventory does
not survive validation. Each of those must fall back to the candidate
generator instead of producing an unexecutable plan.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.milp_optimizer import is_scipy_available, solve_milp
from custom_components.hsem.utils.prices import SlotPrice

_MODULE = "custom_components.hsem.planner.milp_optimizer"
_TZ = ZoneInfo("Europe/Stockholm")
_NOW = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)

pytestmark = pytest.mark.skipif(not is_scipy_available(), reason="scipy unavailable")


def _slot(
    index: int, *, import_price: float = 1.0, export_price: float = 0.5
) -> PlannedSlot:
    """Return one actionable hourly slot with a small house load."""
    start = _NOW + timedelta(hours=index)
    slot = PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=import_price, export_price=export_price),
    )
    slot.avg_house_consumption_kwh = 0.5
    slot.solcast_pv_estimate_kwh = 0.0
    slot.estimated_net_consumption_kwh = 0.5
    return slot


def _solve(slots: list[PlannedSlot] | None = None, **overrides: Any) -> Any:
    """Solve a two-slot horizon with *overrides* applied."""
    kwargs: dict[str, Any] = {
        "current_kwh": 5.0,
        "usable_kwh": 10.0,
        "max_charge_per_slot": 2.0,
        "max_discharge_per_slot": 2.0,
        "charge_efficiency_pct": 100.0,
        "discharge_efficiency_pct": 100.0,
    }
    kwargs.update(overrides)
    return solve_milp(
        slots if slots is not None else [_slot(0), _slot(1)], _NOW, **kwargs
    )


class TestPreSolveRefusals:
    """Nothing is solved when there is nothing the battery could do."""

    def test_without_scipy_the_milp_is_disabled(self) -> None:
        """A missing optional dependency disables the MILP, not the planner."""
        with patch.dict("sys.modules", {"scipy": None, "scipy.optimize": None}):
            assert _solve() is None

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"usable_kwh": 0.0}, id="no_usable_capacity"),
            pytest.param({"max_charge_per_slot": 0.0}, id="no_charge_rate"),
        ],
    )
    def test_a_battery_that_cannot_move_energy_is_skipped(
        self, overrides: dict[str, Any]
    ) -> None:
        """A zero-capacity or zero-rate battery has no decision to make."""
        assert _solve(**overrides) is None

    def test_an_empty_horizon_is_skipped(self) -> None:
        """No slots means no variables to solve for."""
        assert _solve([]) is None

    def test_a_fully_past_horizon_is_skipped(self) -> None:
        """Every slot already ended, so nothing is still actionable."""
        past = [_slot(-3), _slot(-2)]

        assert _solve(past) is None


class TestForecastReserveInput:
    """An unusable forecast reserve is treated as no reserve at all."""

    @pytest.mark.parametrize(
        "reserve",
        [
            pytest.param("not a number", id="unparseable"),
            pytest.param(None, id="missing"),
            pytest.param(float("nan"), id="non_finite"),
        ],
    )
    def test_an_unusable_reserve_does_not_block_the_solve(self, reserve: Any) -> None:
        """Bad data must not become an arbitrary reserve on the battery."""
        result = _solve(battery_export_forecast_reserve_kwh=reserve)

        assert result is not None
        _planned, diagnostics = result
        assert diagnostics["battery_export_forecast_reserve_active"] is False

    def test_a_reserve_above_the_usable_capacity_is_clamped(self) -> None:
        """A reserve larger than the battery is the whole battery."""
        result = _solve(battery_export_forecast_reserve_kwh=1e6)

        assert result is not None


class TestPostSolveRefusals:
    """A solution that cannot be trusted is rejected, not returned."""

    def test_a_bounds_layout_mismatch_is_rejected(self) -> None:
        """Misaligned bounds would apply limits to the wrong variables."""
        from custom_components.hsem.planner.milp._constraints import _build_constraints

        def _short_bounds(*args: Any, **kwargs: Any) -> dict[str, Any]:
            constraints = _build_constraints(*args, **kwargs)
            constraints["bounds"] = constraints["bounds"][:-1]
            return constraints

        with (
            patch(
                "custom_components.hsem.planner.milp._constraints._build_constraints",
                _short_bounds,
            ),
            patch(f"{_MODULE}.log_planner") as log,
        ):
            assert _solve() is None

        assert any(
            "bounds layout mismatch" in str(call.args[1])
            for call in log.call_args_list
            if len(call.args) > 1
        )

    def test_an_unexecutable_inventory_is_rejected(self) -> None:
        """A plan that discharges energy it never had is thrown away."""
        with (
            patch(
                "custom_components.hsem.planner.milp._postwrite_validation"
                ".validate_primary_inventory",
                return_value={"valid": False, "reason": "inventory_underflow"},
            ),
            patch(f"{_MODULE}.log_planner") as log,
        ):
            assert _solve() is None

        assert any(
            "Rejecting executable primary inventory" in str(call.args[1])
            for call in log.call_args_list
            if len(call.args) > 1
        )

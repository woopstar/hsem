"""Regression tests for grouped primary-battery export reserve checkpoints."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.milp._export_reserve import (
    _next_solar_refill_checkpoints,
)
from custom_components.hsem.planner.milp_optimizer import (
    is_scipy_available,
    solve_milp,
)
from custom_components.hsem.utils.prices import SlotPrice

_TZ = ZoneInfo("Europe/Stockholm")
_NOW = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)


def _slot(
    index: int,
    *,
    import_price: float,
    export_price: float,
    consumption_kwh: float,
    pv_kwh: float,
) -> PlannedSlot:
    """Build one fully actionable hourly optimizer slot."""
    start = _NOW + timedelta(hours=index)
    slot = PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=import_price, export_price=export_price),
    )
    slot.avg_house_consumption_kwh = consumption_kwh
    slot.solcast_pv_estimate_kwh = pv_kwh
    slot.estimated_net_consumption_kwh = consumption_kwh - pv_kwh
    return slot


@pytest.mark.parametrize(
    ("pv_avail", "expected"),
    [
        ([0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 0.0], [0, 4, 4, 4, 4, 6, 6]),
        ([0.0, 1.0, 0.0, 1.0, 0.0], [0, 2, 2, 4, 4]),
        ([0.0, 0.0, 1.0, 0.0, 0.0], [1, 1, 4, 4, 4]),
        ([1.0, 1.0, 1.0], [2, 2, 2]),
        ([0.0, 0.0, 0.0], [2, 2, 2]),
    ],
)
def test_checkpoints_group_contiguous_surplus_runs(
    pv_avail: list[float], expected: list[int]
) -> None:
    """Every slot in one surplus run shares its following checkpoint."""
    checkpoints = _next_solar_refill_checkpoints(np.asarray(pv_avail, dtype=float))
    assert checkpoints.tolist() == expected


def _solve(
    *, following_demand_kwh: float, no_export: bool = False
) -> tuple[list[PlannedSlot], dict[str, Any]]:
    """Solve adjacent PV-surplus slots followed by one demand slot."""
    slots = [
        _slot(
            0,
            import_price=2.90,
            export_price=2.90,
            consumption_kwh=0.0,
            pv_kwh=0.26,
        ),
        _slot(
            1,
            import_price=3.00,
            export_price=3.00,
            consumption_kwh=0.0,
            pv_kwh=0.26,
        ),
        _slot(
            2,
            import_price=10.0,
            export_price=0.0,
            consumption_kwh=following_demand_kwh,
            pv_kwh=0.0,
        ),
    ]
    result = solve_milp(
        slots,
        _NOW,
        current_kwh=5.0,
        usable_kwh=5.0,
        max_charge_per_slot=0.01,
        max_discharge_per_slot=5.0,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
        excess_export_discharge_buffer_pct=20.0,
        no_export=no_export,
    )
    assert result is not None
    return result


def _trajectory(planned: list[PlannedSlot]) -> list[float]:
    """Return end-of-slot battery energy from the known 5 kWh initial state."""
    energy = 5.0
    result: list[float] = []
    for slot in planned:
        energy += slot.batteries_charged_kwh - slot.batteries_discharged_kwh
        result.append(energy)
    return result


@pytest.mark.skipif(not is_scipy_available(), reason="scipy unavailable")
def test_adjacent_surplus_cannot_bypass_shared_reserve() -> None:
    """Following demand preserves the common 1 kWh checkpoint reserve."""
    planned, diagnostics = _solve(following_demand_kwh=4.0)

    assert diagnostics["battery_export_reserve_active"] is True
    assert _trajectory(planned) == pytest.approx([5.0, 5.0, 1.0])
    assert [slot.primary_battery_export_kwh for slot in planned] == pytest.approx(
        [0.0, 0.0, 0.0]
    )
    assert [slot.pv_export_kwh for slot in planned] == pytest.approx([0.26, 0.26, 0.0])


@pytest.mark.skipif(not is_scipy_available(), reason="scipy unavailable")
def test_feasible_reserve_exports_battery_at_higher_price() -> None:
    """A feasible common reserve allows export in the better-priced slot."""
    planned, diagnostics = _solve(following_demand_kwh=1.0)

    assert _trajectory(planned) == pytest.approx([5.0, 2.0, 1.0])
    assert [slot.primary_battery_export_kwh for slot in planned] == pytest.approx(
        [0.0, 3.0, 0.0]
    )
    assert [slot.pv_export_kwh for slot in planned] == pytest.approx([0.26, 0.26, 0.0])
    assert diagnostics[
        "battery_export_reserve_min_checkpoint_soc_kwh"
    ] == pytest.approx(1.0)


@pytest.mark.skipif(not is_scipy_available(), reason="scipy unavailable")
def test_no_export_preserves_direct_pv_export() -> None:
    """Battery no-export mode must not suppress direct PV export."""
    planned, _diagnostics = _solve(following_demand_kwh=1.0, no_export=True)

    assert _trajectory(planned) == pytest.approx([5.0, 5.0, 4.0])
    assert [slot.primary_battery_export_kwh for slot in planned] == pytest.approx(
        [0.0, 0.0, 0.0]
    )
    assert [slot.pv_export_kwh for slot in planned] == pytest.approx([0.26, 0.26, 0.0])
    assert [slot.grid_export_kwh for slot in planned] == pytest.approx(
        [0.26, 0.26, 0.0]
    )


# ---------------------------------------------------------------------------
# Immediate forecast-export reserve (issue #807, Stage 1)
#
# Unlike the checkpoint reserve above (which a later solar/grid refill may
# restore before the *next demand window*), the forecast reserve must remain
# immediately after the exporting slot itself — a later refill can never
# justify spending it first.
# ---------------------------------------------------------------------------


def _solve_forecast_reserve(
    *, forecast_reserve_kwh: float, no_export: bool = False
) -> tuple[list[PlannedSlot], dict[str, Any]]:
    """Solve one attractively-priced export slot followed by a neutral slot."""
    slots = [
        _slot(
            0,
            import_price=0.0,
            export_price=5.0,
            consumption_kwh=0.0,
            pv_kwh=0.0,
        ),
        _slot(
            1,
            import_price=0.0,
            export_price=0.0,
            consumption_kwh=0.0,
            pv_kwh=0.0,
        ),
    ]
    result = solve_milp(
        slots,
        _NOW,
        current_kwh=5.0,
        usable_kwh=5.0,
        max_charge_per_slot=0.01,
        max_discharge_per_slot=5.0,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
        battery_export_forecast_reserve_kwh=forecast_reserve_kwh,
        no_export=no_export,
    )
    assert result is not None
    return result


@pytest.mark.skipif(not is_scipy_available(), reason="scipy unavailable")
def test_forecast_reserve_caps_battery_export_in_the_exporting_slot() -> None:
    """Battery-origin export cannot drop post-export SoC below the reserve."""
    planned, diagnostics = _solve_forecast_reserve(forecast_reserve_kwh=3.0)

    # 5.0 kWh starting energy, 3.0 kWh reserve -> at most 2.0 kWh exportable.
    assert planned[0].primary_battery_export_kwh == pytest.approx(2.0)
    assert _trajectory(planned) == pytest.approx([3.0, 3.0])
    assert diagnostics["battery_export_forecast_reserve_active"] is True
    assert diagnostics["battery_export_forecast_reserve_kwh"] == pytest.approx(3.0)
    assert diagnostics[
        "battery_export_forecast_reserve_min_post_export_soc_kwh"
    ] == pytest.approx(3.0)


@pytest.mark.skipif(not is_scipy_available(), reason="scipy unavailable")
def test_zero_forecast_reserve_leaves_export_unconstrained() -> None:
    """A zero (default/disabled) reserve does not activate the mechanism."""
    planned, diagnostics = _solve_forecast_reserve(forecast_reserve_kwh=0.0)

    assert planned[0].primary_battery_export_kwh == pytest.approx(5.0)
    assert diagnostics["battery_export_forecast_reserve_active"] is False


@pytest.mark.skipif(not is_scipy_available(), reason="scipy unavailable")
def test_no_export_disables_forecast_reserve_activation() -> None:
    """The reserve is a battery-export protection; it is inert when export is off."""
    planned, diagnostics = _solve_forecast_reserve(
        forecast_reserve_kwh=3.0, no_export=True
    )

    assert [slot.primary_battery_export_kwh for slot in planned] == pytest.approx(
        [0.0, 0.0]
    )
    assert diagnostics["battery_export_forecast_reserve_active"] is False

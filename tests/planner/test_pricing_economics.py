"""Portable economics invariants from upstream PR #24.

The upstream file imports fork-only modules, so these assert the same
*invariants* against this repository's API rather than copying its code.
Coverage complements ``test_pr24_portable_invariants.py``, which already pins
bounded negative-import charging and same-slot wash-flow exclusion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.cost_helpers import grid_cash_flow_cost
from custom_components.hsem.planner.milp._price_sanitise import sanitize_prices
from custom_components.hsem.planner.milp_optimizer import is_scipy_available, solve_milp
from custom_components.hsem.utils.prices import SlotPrice

_NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)


def _slot(
    offset: int = 0,
    *,
    import_price: float = 0.2,
    export_price: float = 0.0,
    load_kwh: float = 0.0,
    pv_kwh: float = 0.0,
) -> PlannedSlot:
    start = _NOW + timedelta(hours=offset)
    slot = PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=import_price, export_price=export_price),
    )
    slot.avg_house_consumption_kwh = load_kwh
    slot.solcast_pv_estimate_kwh = pv_kwh
    slot.estimated_net_consumption_kwh = load_kwh - pv_kwh
    return slot


# ---------------------------------------------------------------------------
# Auditable meter cash flow
# ---------------------------------------------------------------------------


def test_final_slot_cost_uses_grid_flow_and_signed_import_price() -> None:
    """``estimated_cost_currency`` is recomputable from the published fields."""
    result = solve_milp(
        [
            _slot(0, import_price=0.30, export_price=0.05, load_kwh=1.0),
            _slot(1, import_price=-0.10, export_price=0.05),
            _slot(2, import_price=0.40, export_price=0.05, load_kwh=0.5),
        ],
        _NOW,
        current_kwh=1.0,
        usable_kwh=5.0,
        max_charge_per_slot=2.0,
        max_discharge_per_slot=2.0,
        cycle_cost_per_kwh=0.01,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
    )
    assert result is not None
    slots, _diagnostics = result
    for slot in slots:
        expected = (
            slot.grid_import_kwh * slot.price.import_price
            - slot.grid_export_kwh * slot.price.export_price
        )
        assert slot.estimated_cost_currency == pytest.approx(expected, abs=1e-3), (
            "published slot cost must be reproducible from the published grid "
            "fields and signed prices alone"
        )


def test_cash_flow_helper_keeps_negative_import_signed() -> None:
    """A negative import rate credits the site rather than clamping to zero."""
    assert grid_cash_flow_cost(2.0, 0.0, -0.05, 0.0) == pytest.approx(-0.10)


def test_cash_flow_helper_treats_non_finite_rates_as_zero() -> None:
    """Non-finite rates carry no economic authority."""
    assert grid_cash_flow_cost(2.0, 1.0, float("nan"), float("inf")) == pytest.approx(
        0.0
    )


def test_cash_flow_helper_zeroes_export_below_floor() -> None:
    """Export below the battery-origin floor earns nothing, mirroring the MILP."""
    assert grid_cash_flow_cost(
        0.0, 3.0, 0.20, 0.04, export_min_price=0.10
    ) == pytest.approx(0.0)
    assert grid_cash_flow_cost(
        0.0, 3.0, 0.20, 0.12, export_min_price=0.10
    ) == pytest.approx(-0.36)


# ---------------------------------------------------------------------------
# Effective battery-origin export floor
# ---------------------------------------------------------------------------


def test_effective_export_floor_is_the_max_of_both_floors() -> None:
    """The operative floor combines the site and battery-specific floors."""
    p_imp = np.array([0.2, 0.2, 0.2])
    p_exp = np.array([0.05, 0.12, 0.25])

    _imp, _exp, blocked = sanitize_prices(
        p_imp.copy(),
        p_exp.copy(),
        min_export_price=0.10,
        battery_export_min_price=0.20,
    )
    # max(0.10, 0.20) == 0.20 governs, so only the 0.25 slot is free.
    assert blocked.tolist() == [True, True, False]


def test_effective_export_floor_honours_the_depreciation_threshold() -> None:
    """A depreciation-derived floor above the configured one still governs."""
    _imp, _exp, blocked = sanitize_prices(
        np.array([0.2, 0.2]),
        np.array([0.08, 0.15]),
        min_export_price=0.12,
        battery_export_min_price=0.0,
    )
    assert blocked.tolist() == [True, False]


def test_export_floor_leaves_prices_undistorted() -> None:
    """The floor blocks battery export; it never rewrites the market rate."""
    p_imp = np.array([-0.05, 0.30])
    p_exp = np.array([0.02, 0.40])
    imp_out, exp_out, _blocked = sanitize_prices(
        p_imp.copy(),
        p_exp.copy(),
        min_export_price=0.10,
        battery_export_min_price=0.10,
    )
    assert imp_out.tolist() == pytest.approx([-0.05, 0.30])
    assert exp_out.tolist() == pytest.approx([0.02, 0.40])


def test_zero_floor_blocks_nothing() -> None:
    """With both floors at zero no slot is masked."""
    _imp, _exp, blocked = sanitize_prices(
        np.array([0.2, 0.2]),
        np.array([0.0, 0.5]),
        min_export_price=0.0,
        battery_export_min_price=0.0,
    )
    assert not blocked.any()


def test_battery_export_floor_does_not_block_direct_pv_export() -> None:
    """PV export and its revenue survive a floor that blocks battery export."""
    result = solve_milp(
        [_slot(0, import_price=0.30, export_price=0.02, pv_kwh=3.0)],
        _NOW,
        # Battery starts empty, so any export in this slot must be direct PV.
        current_kwh=0.0,
        usable_kwh=2.0,
        max_charge_per_slot=2.0,
        max_discharge_per_slot=2.0,
        cycle_cost_per_kwh=0.01,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
        battery_export_min_price=0.50,
    )
    assert result is not None
    slots, _diagnostics = result
    assert slots[0].grid_export_kwh > 0.0, (
        "a battery-origin export floor must never suppress direct PV export"
    )


# ---------------------------------------------------------------------------
# Extreme prices stay inside physical bounds
# ---------------------------------------------------------------------------


def test_extreme_negative_import_cannot_charge_above_ceiling() -> None:
    """An enormous consumption credit still respects usable capacity."""
    result = solve_milp(
        [_slot(0, import_price=-100.0), _slot(1, import_price=-100.0)],
        _NOW,
        current_kwh=0.0,
        usable_kwh=3.0,
        max_charge_per_slot=10.0,
        max_discharge_per_slot=10.0,
        cycle_cost_per_kwh=0.01,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
    )
    assert result is not None
    slots, diagnostics = result
    inventory = 0.0
    for slot in slots:
        inventory += slot.batteries_charged_kwh - slot.batteries_discharged_kwh
        assert inventory <= 3.0 + 1e-6, "SoC exceeded usable capacity"
        assert inventory >= -1e-6, "SoC fell below zero"
    assert diagnostics["primary_postwrite_inventory_validation"]["valid"] is True


def test_extreme_export_price_cannot_discharge_below_floor() -> None:
    """An enormous export rate still cannot drain past empty."""
    result = solve_milp(
        [_slot(0, import_price=0.30, export_price=100.0)],
        _NOW,
        current_kwh=1.5,
        usable_kwh=5.0,
        max_charge_per_slot=10.0,
        max_discharge_per_slot=10.0,
        cycle_cost_per_kwh=0.01,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
    )
    assert result is not None
    slots, diagnostics = result
    assert slots[0].batteries_discharged_kwh <= 1.5 + 1e-6, (
        "discharge cannot exceed the energy actually stored"
    )
    assert diagnostics["primary_postwrite_inventory_validation"]["valid"] is True

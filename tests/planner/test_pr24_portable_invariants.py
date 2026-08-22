"""Portable PR #24 planner economics and physical-safety regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.milp._postwrite_validation import (
    validate_primary_inventory,
)
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


def test_signed_negative_import_is_finite_and_profitable() -> None:
    """A bounded negative import price can justify finite battery charging."""
    result = solve_milp(
        [_slot(import_price=-0.05)],
        _NOW,
        current_kwh=0.0,
        usable_kwh=2.0,
        max_charge_per_slot=2.0,
        max_discharge_per_slot=2.0,
        cycle_cost_per_kwh=0.02,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
    )
    assert result is not None
    slots, diagnostics = result
    assert slots[0].batteries_charged_kwh == pytest.approx(2.0)
    assert slots[0].grid_import_kwh == pytest.approx(2.0)
    assert slots[0].grid_export_kwh == pytest.approx(0.0)
    assert slots[0].estimated_cost_currency == pytest.approx(-0.10)
    assert diagnostics["primary_postwrite_inventory_validation"]["valid"] is True


def test_export_above_import_has_no_same_slot_wash_flow() -> None:
    """Direction binaries preserve the real rate without simultaneous flows."""
    result = solve_milp(
        [_slot(import_price=0.05, export_price=0.10)],
        _NOW,
        current_kwh=1.0,
        usable_kwh=1.0,
        max_charge_per_slot=1.0,
        max_discharge_per_slot=1.0,
        cycle_cost_per_kwh=0.0,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
    )
    assert result is not None
    slots, _diagnostics = result
    assert slots[0].grid_export_kwh == pytest.approx(1.0)
    assert slots[0].grid_import_kwh == pytest.approx(0.0)


def test_hard_fuse_row_allows_baseline_but_blocks_extra_charge() -> None:
    """Controllable charging cannot worsen an unavoidable house overload."""
    result = solve_milp(
        [_slot(import_price=-1.0, load_kwh=2.0)],
        _NOW,
        current_kwh=0.0,
        usable_kwh=2.0,
        max_charge_per_slot=2.0,
        max_discharge_per_slot=0.0,
        main_fuse_amps=1.0,
        main_fuse_phases=3,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
    )
    assert result is not None
    slots, diagnostics = result
    assert slots[0].grid_import_kwh == pytest.approx(2.0)
    assert slots[0].batteries_charged_kwh == pytest.approx(0.0)
    assert diagnostics["total_fuse_violation_kwh"] == pytest.approx(1.31)


def test_fixed_unmanaged_session_is_accounted_without_command() -> None:
    """A disabled smart EV remains fixed site demand but emits zero watts."""
    ev = EVConfig(
        enabled=True,
        capacity_kwh=12.0,
        max_charge_per_slot=6.0,
        charger_efficiency=1.0,
        session_charge_kw=6.0,
        fixed_session_only=True,
    )
    result = solve_milp(
        [_slot(0), _slot(1), _slot(2)],
        _NOW,
        current_kwh=0.0,
        usable_kwh=2.0,
        max_charge_per_slot=1.0,
        max_discharge_per_slot=0.0,
        ev_configs=[ev],
    )
    assert result is not None
    slots, _diagnostics = result
    assert [slot.ev_total_planned_load_kwh for slot in slots[:2]] == pytest.approx(
        [6.0, 6.0]
    )
    assert [slot.ev_charger_calculated_power for slot in slots] == [0.0, 0.0, 0.0]
    assert slots[2].ev_total_planned_load_kwh == pytest.approx(0.0)


def test_postwrite_inventory_validation_is_cumulative() -> None:
    """Individually plausible rounded discharges cannot overdraw the horizon."""
    slots = [_slot(0), _slot(1)]
    slots[0].batteries_discharged_kwh = 0.6
    slots[1].batteries_discharged_kwh = 0.6
    validation = validate_primary_inventory(
        slots,
        [0, 1],
        current_kwh=1.0,
        usable_kwh=1.0,
    )
    assert validation["valid"] is False
    assert validation["reason"] == "primary_inventory_below_floor"
    assert validation["slot"] == 1

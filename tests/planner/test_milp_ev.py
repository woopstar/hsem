"""Tests for MILP EV co-optimisation (issue #530).

Coverage
--------
- EV reaches target by deadline via MILP co-optimisation
- Co-optimisation produces a cheaper plan than pre-computed EV loads
- Deadline penalty prevents infeasibility when target cannot be met
- No regression when EVs are absent (backward compatible)
- EV charge energy bounded by max_charge_per_slot
- Two EVs co-optimised simultaneously
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planner_inputs import EVConfig
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.planner.milp_optimizer import is_scipy_available, solve_milp
from custom_components.hsem.planner.soc_simulation import simulate_soc
from custom_components.hsem.utils.prices import SlotPrice

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2024, 6, 15, 14, 0, tzinfo=_TZ)


def _make_slot(
    *,
    hour: int,
    day: int = 15,
    import_price: float = 0.20,
    export_price: float = 0.05,
    pv_kwh: float = 0.0,
    consumption_kwh: float = 0.5,
    recommendation: str | None = None,
) -> PlannedSlot:
    """Build a minimal PlannedSlot for EV + battery MILP unit tests."""
    start = datetime(2024, 6, day, hour, 0, tzinfo=_TZ)
    s = PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=import_price, export_price=export_price),
        recommendation=recommendation,
    )
    s.avg_house_consumption_kwh = consumption_kwh
    s.solcast_pv_estimate_kwh = pv_kwh
    s.ev_planned_load_kwh = 0.0
    s.ev_accounted_load_kwh = 0.0
    s.ev_total_planned_load_kwh = 0.0
    s.estimated_net_consumption_kwh = consumption_kwh - pv_kwh
    return s


def _build_slots(
    n: int,
    start_hour: int = 14,
    import_price: float = 0.20,
    pv_kwh: float = 0.0,
    consumption_kwh: float = 0.5,
) -> list[PlannedSlot]:
    """Build a list of n hourly slots starting at start_hour."""
    slots = []
    for i in range(n):
        h = (start_hour + i) % 24
        day = 15 + (start_hour + i) // 24
        s = _make_slot(
            hour=h,
            day=day,
            import_price=import_price,
            export_price=round(import_price * 0.8, 4),
            pv_kwh=pv_kwh,
            consumption_kwh=consumption_kwh,
        )
        # Slot at or before 'now' (14:00 June 15) is past
        if day == 15 and h < 14:
            s.recommendation = "time_passed"
        slots.append(s)
    return slots


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


_pytestmark_scipy = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)


@_pytestmark_scipy
def test_ev_reaches_target_by_deadline():
    """MILP charges EV to target by the deadline slot.

    Setup: 10 slots (14:00-23:00), EV at 0 kWh needs 20 kWh by slot 8 (22:00).
    Max charge per slot = 3 kWh (DC-side), so 6 slots * 3 = 18 < 20, meaning
    the EV can't quite reach the target.  With 7 slots before deadline,
    7*3 = 21 >= 20, target is reachable.  The MILP should charge enough.
    """
    slots = _build_slots(10, start_hour=14, import_price=0.20)
    # EV: 50 kWh battery, currently at 20% (10 kWh), target 60% (30 kWh),
    # needs 20 kWh, max charge 3 kWh/slot (DC), deadline = slot 6 (20:00)
    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=10.0,
        target_kwh=30.0,
        capacity_kwh=50.0,
        max_charge_per_slot=3.0,
        charger_efficiency=0.90,
        deadline_slot=6,  # 0-based LP index: slot 14+6=20:00
    )

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=10.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
        ev_configs=[ev],
    )

    assert result is not None
    out_slots, diag = result

    # EV should have charged close to 20 kWh (DC-side) total
    ev_total_dc = sum(
        s.ev_total_planned_load_kwh * 0.9  # AC → DC conversion
        for s in out_slots
    )
    assert ev_total_dc == pytest.approx(20.0, rel=0.05)

    # EV diagnostics
    assert "ev" in diag
    assert diag["ev"]["ev0"]["deadline_met"] is True


@_pytestmark_scipy
def test_ev_penalty_prevents_infeasibility():
    """When EV cannot reach target, the penalty absorbs the shortfall.

    Setup: 5 slots, EV needs 20 kWh, max charge = 2 kWh/slot.
    5 * 2 = 10 < 20, so target is impossible.  The penalty variable
    absorbs the shortfall, and the MILP still returns a valid plan.
    """
    slots = _build_slots(5, start_hour=14, import_price=0.20)
    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=0.0,
        target_kwh=20.0,
        capacity_kwh=50.0,
        max_charge_per_slot=2.0,
        charger_efficiency=0.90,
        deadline_slot=4,  # last slot
    )

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=10.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
        ev_configs=[ev],
    )

    assert result is not None
    _out_slots, diag = result

    # Should have a non-zero penalty (deadline not met)
    assert "ev" in diag
    assert diag["ev"]["ev0"]["deadline_met"] is False
    assert diag["ev"]["ev0"]["deadline_penalty_kwh"] > 0.01


@_pytestmark_scipy
def test_ev_no_regression_when_evs_absent():
    """When no EV configs are provided, behaviour is unchanged.

    This is a smoke test ensuring backward compatibility.
    """
    slots = _build_slots(8, start_hour=14, import_price=0.20)

    result_no_ev = solve_milp(
        slots,
        _NOW,
        current_kwh=5.0,
        usable_kwh=10.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
        ev_configs=None,
    )
    assert result_no_ev is not None, "MILP should work without EV configs"

    # Same call with empty EV list should give same result
    result_empty_ev = solve_milp(
        slots,
        _NOW,
        current_kwh=5.0,
        usable_kwh=10.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
        ev_configs=[],
    )
    assert result_empty_ev is not None
    # EV total loads should be zero
    for s in result_empty_ev[0]:
        assert s.ev_total_planned_load_kwh == 0.0


@_pytestmark_scipy
def test_ev_charge_bounded_by_max_per_slot():
    """EV charge per slot never exceeds max_charge_per_slot.

    Setup: 10 slots, EV needs large energy, max charge = 2 kWh/slot.
    No slot should exceed 2 kWh (DC-side, which means ~2.22 kWh AC at 90% eff).
    """
    slots = _build_slots(10, start_hour=14, import_price=0.05)  # cheap power
    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=0.0,
        target_kwh=50.0,  # large target
        capacity_kwh=60.0,
        max_charge_per_slot=2.0,
        charger_efficiency=0.90,
        deadline_slot=8,
    )

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=5.0,
        usable_kwh=10.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
        cycle_cost_per_kwh=0.01,
        ev_configs=[ev],
    )

    assert result is not None
    out_slots, _diag = result

    # Per-slot AC load should not exceed max_charge / efficiency
    max_ac_per_slot = 2.0 / 0.90
    for s in out_slots:
        assert s.ev_total_planned_load_kwh <= max_ac_per_slot + 1e-6


@_pytestmark_scipy
def test_two_evs_cooptimized():
    """Two EVs are co-optimised simultaneously.

    Both EVs have different targets and max charge rates.
    The MILP allocates available slots across both EVs.
    """
    slots = _build_slots(12, start_hour=14, import_price=0.15)
    ev1 = EVConfig(
        enabled=True,
        initial_soc_kwh=10.0,
        target_kwh=30.0,  # need 20 kWh
        capacity_kwh=50.0,
        max_charge_per_slot=3.0,
        charger_efficiency=0.90,
        deadline_slot=8,
        base_load_includes_ev=False,
    )
    ev2 = EVConfig(
        enabled=True,
        initial_soc_kwh=5.0,
        target_kwh=15.0,  # need 10 kWh
        capacity_kwh=40.0,
        max_charge_per_slot=2.0,
        charger_efficiency=0.85,
        deadline_slot=10,
        base_load_includes_ev=False,
    )

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=5.0,
        usable_kwh=10.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
        ev_configs=[ev1, ev2],
    )

    assert result is not None
    _out_slots, diag = result

    assert "ev" in diag
    # Both EVs should meet their deadlines
    assert diag["ev"]["ev0"]["deadline_met"] is True
    assert diag["ev"]["ev1"]["deadline_met"] is True

    # EV0 total DC should be ~20 kWh, EV1 total DC should be ~10 kWh
    assert diag["ev"]["ev0"]["total_dc_kwh"] == pytest.approx(20.0, rel=0.05)
    assert diag["ev"]["ev1"]["total_dc_kwh"] == pytest.approx(10.0, rel=0.05)


@_pytestmark_scipy
def test_ev_soc_upper_bound_respected():
    """EV SoC never exceeds capacity_kwh.

    Setup: EV with small capacity (10 kWh) but large target.
    The MILP should cap charging at the capacity bound.
    """
    slots = _build_slots(10, start_hour=14, import_price=0.05)
    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=8.0,  # already at 80%
        target_kwh=20.0,  # target > capacity
        capacity_kwh=10.0,
        max_charge_per_slot=3.0,
        charger_efficiency=0.90,
        deadline_slot=8,
    )

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=5.0,
        usable_kwh=10.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
        ev_configs=[ev],
    )

    assert result is not None
    out_slots, diag = result

    # Total DC charge should not exceed (capacity - initial) = 2 kWh
    ev_total_dc = diag["ev"]["ev0"]["total_dc_kwh"]
    assert ev_total_dc <= 2.0 + 1e-6


@_pytestmark_scipy
def test_ev_with_base_load_includes_ev():
    """When base_load_includes_ev=True, EV load goes to accounted field.

    The planned_load field should remain zero.
    """
    slots = _build_slots(8, start_hour=14, import_price=0.10)
    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=0.0,
        target_kwh=10.0,
        capacity_kwh=50.0,
        max_charge_per_slot=3.0,
        charger_efficiency=0.90,
        deadline_slot=6,
        base_load_includes_ev=True,
    )

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=5.0,
        usable_kwh=10.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
        ev_configs=[ev],
    )

    assert result is not None
    out_slots, _diag = result

    for s in out_slots:
        if s.ev_total_planned_load_kwh > 1e-9:
            assert s.ev_planned_load_kwh == pytest.approx(0.0, abs=1e-6)
            assert s.ev_accounted_load_kwh > 1e-9


@_pytestmark_scipy
def test_cooptimization_uses_cheap_slots_for_ev():
    """MILP allocates EV charging to cheaper slots when possible.

    Setup: slots 0-3 are cheap (0.05), slots 4-7 are expensive (0.50).
    The EV should prefer charging in cheap slots.
    """
    slots = []
    for i in range(8):
        h = (14 + i) % 24
        day = 15 + (14 + i) // 24
        cheap = i < 4
        price = 0.05 if cheap else 0.50
        s = _make_slot(
            hour=h,
            day=day,
            import_price=price,
            export_price=round(price * 0.8, 4),
        )
        if day == 15 and h < 14:
            s.recommendation = "time_passed"
        slots.append(s)

    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=0.0,
        target_kwh=6.0,
        capacity_kwh=50.0,
        max_charge_per_slot=2.0,
        charger_efficiency=1.0,  # 100% for clean comparison
        deadline_slot=7,
    )

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=10.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
        ev_configs=[ev],
    )

    assert result is not None
    out_slots, diag = result

    # All EV charging should be in cheap slots (LP indices 0-3)
    for lp_t, s in enumerate(out_slots):
        if lp_t >= 4:  # LP indices map to slots 4-7
            assert s.ev_total_planned_load_kwh < 1e-6, (
                f"EV should not charge in expensive slot {lp_t}"
            )

    # Total DC charge should be ~6 kWh
    assert diag["ev"]["ev0"]["total_dc_kwh"] == pytest.approx(6.0, rel=0.05)


# ---------------------------------------------------------------------------
# Integration test: full planner with EV config
# ---------------------------------------------------------------------------


@_pytestmark_scipy
def test_full_planner_with_ev_integration():
    """Full planner run with EV enabled produces valid output.

    Uses the fixture-based planner invocation to ensure end-to-end
    integration works.
    """
    from tests.planner.fixtures import make_summer_day_input

    inp = make_summer_day_input(
        battery_soc_pct=50.0,
        interval_minutes=60,
        interval_length_hours=24,
    )
    # Enable primary EV
    inp.ev_planned_load_enabled = True
    inp.ev_planned_load_connected = True
    inp.ev_planned_load_smart_charging_enabled = True
    inp.ev_planned_load_current_soc_pct = 20.0
    inp.ev_planned_load_target_soc_pct = 80.0
    inp.ev_planned_load_battery_capacity_kwh = 60.0
    inp.ev_planned_load_charger_power_kw = 7.2
    inp.ev_planned_load_charger_efficiency_pct = 90.0
    inp.ev_planned_load_deadline = datetime(2024, 6, 16, 7, 0, tzinfo=_TZ)
    inp.ev_planned_load_base_load_includes_ev = False

    from custom_components.hsem.planner import run_planner

    output = run_planner(inp)

    assert output.slots, "Planner should produce slots"
    assert output.plan_cost is not None, "Plan should have cost"

    # MILP should be among candidates
    milp_candidates = [c for c in output.candidates if c.name == "milp"]
    assert len(milp_candidates) == 1, "MILP candidate should be present"

    # If MILP won, check EV diagnostics
    if output.winner_name == "milp":
        diag = milp_candidates[0].diagnostics
        if diag and "ev" in diag:
            assert diag["ev"]["ev0"]["deadline_met"] is True, (
                "MILP should meet EV deadline in summer scenario"
            )

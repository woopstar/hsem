"""Tests for session-aware EV demand in MILP (issue #615, #639).

Coverage
--------
- session_charge_kw overrides probabilistic demand for first 2 hours
  (resolution-dependent: 8 slots at 15-min, 4 at 30-min, 2 at 60-min)
- Fallback to normal EV co-optimisation beyond the 2-hour session window
- Grid-charging battery is blocked during session demand slots
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.engine_ev_milp import _build_ev_configs_for_milp
from custom_components.hsem.planner.milp_optimizer import is_scipy_available, solve_milp
from custom_components.hsem.utils.phase_power import EV_TOPOLOGY_THREE_PHASE_BALANCED
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
    interval_minutes: int = 60,
) -> PlannedSlot:
    """Build a minimal PlannedSlot for session EV unit tests."""
    start = datetime(2024, 6, day, hour, 0, tzinfo=_TZ)
    s = PlannedSlot(
        start=start,
        end=start + timedelta(minutes=interval_minutes),
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
    interval_minutes: int = 60,
) -> list[PlannedSlot]:
    """Build a list of n slots starting at start_hour."""
    slots = []
    current = datetime(2024, 6, 15, start_hour, 0, tzinfo=_TZ)
    for _i in range(n):
        day = current.day
        h = current.hour
        s = _make_slot(
            hour=h,
            day=day,
            import_price=import_price,
            export_price=round(import_price * 0.8, 4),
            pv_kwh=pv_kwh,
            consumption_kwh=consumption_kwh,
            interval_minutes=interval_minutes,
        )
        # Slot at or before 'now' (14:00 June 15) is past
        if current <= _NOW:
            s.recommendation = "time_passed"
        slots.append(s)
        current += timedelta(minutes=interval_minutes)
    return slots


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

_pytestmark_scipy = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)


@_pytestmark_scipy
def test_session_charge_overrides_probabilistic_demand():
    """Unmanaged session EV charge is fixed for two hours of future slots.

    At 60-min resolution, this covers 2 slots.  The EV config has
    session_charge_kw=6.0 and fixed_session_only=True (HSEM cannot stop
    this charger), so those 2 hourly slots should each show 6.0 * 1h =
    6.0 kWh AC load.  Beyond the 2-hour session window, the MILP decides
    EV charging as usual.
    """
    # 16 hourly slots starting at 14:00 (slot 0 = 14-15, past)
    slots = _build_slots(16, start_hour=14, import_price=0.20, interval_minutes=60)

    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=10.0,
        target_kwh=30.0,  # needs 20 kWh, more than session provides
        capacity_kwh=50.0,
        max_charge_per_slot=10.0,  # DC kWh per slot
        charger_efficiency=0.90,
        deadline_slot=14,
        session_charge_kw=6.0,  # 6 kW AC
        fixed_session_only=True,
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
    out_slots, _diag = result

    # At 60-min resolution, session covers first 2 future slots
    # (LP slots 0-1 → real slots 14:00-16:00; _NOW is at the slot-0
    # boundary, so future_idx == range(m) and out[lp_t] is LP slot lp_t).
    session_ac_per_slot = 6.0 * 1.0  # kW * hours = kWh AC

    for lp_idx in range(2):
        slot = out_slots[lp_idx]  # LP slot lp_idx == out_slots[lp_idx]
        assert slot.ev_total_planned_load_kwh == pytest.approx(
            session_ac_per_slot, rel=0.05
        ), (
            f"Slot at {slot.start}: expected {session_ac_per_slot} kWh AC, "
            f"got {slot.ev_total_planned_load_kwh}"
        )


@_pytestmark_scipy
def test_session_ev_fallback_beyond_session_window():
    """A managed session stays fully flexible, including the current slot.

    The measured 6 kW is telemetry, not actuator intent (issue #797): the
    MILP is free to skip the current slot and schedule the target into
    later, cheaper/flexible slots before the deadline.
    """
    slots = _build_slots(20, start_hour=14, import_price=0.20, interval_minutes=60)

    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=0.0,
        target_kwh=60.0,  # 60 kWh needed
        capacity_kwh=80.0,
        max_charge_per_slot=10.0,
        charger_efficiency=0.90,
        deadline_slot=18,
        session_charge_kw=6.0,  # 6 kW AC → 6 kWh AC/h → 5.4 kWh DC/h
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
    out_slots, _diag = result

    # Compute total DC-side EV charge across all slots
    ev_total_dc = sum(
        s.ev_total_planned_load_kwh * 0.9  # AC → DC
        for s in out_slots
    )
    assert ev_total_dc == pytest.approx(60.0, rel=0.05), (
        f"Expected ~60 kWh DC total EV charge, got {ev_total_dc}"
    )

    # The measured 6 kW session never pins slot 0 — it is telemetry only.
    assert out_slots[0].ev_charger_calculated_power == pytest.approx(0.0)


@_pytestmark_scipy
def test_grid_charging_blocked_during_session_demand():
    """Battery grid-charging is blocked during session EV demand slots.

    Even when import prices are low enough to charge the battery from grid,
    the session-demand constraint prevents BatteriesChargeGrid in session slots.
    The battery can still use BatteriesChargeSolar if PV surplus beyond the
    session EV demand is available.
    """
    # Build slots with high PV in early slots to provide battery charging opportunity.
    # _NOW is at the slot-0 boundary, so LP slots 0-1 are real slots 14:00-16:00
    # (the session slots at 60-min).  Slot 16:00+ has no PV for charging.
    slots = _build_slots(12, start_hour=14, import_price=0.05, interval_minutes=60)

    # Give session slots generous PV: enough to cover EV (6 kWh) AND battery (5 kWh)
    for i in range(2):
        slots[i].solcast_pv_estimate_kwh = 15.0
        slots[i].estimated_net_consumption_kwh = (
            slots[i].avg_house_consumption_kwh - 15.0
        )
    # Beyond session slots: no PV for charging
    for i in range(2, 12):
        slots[i].solcast_pv_estimate_kwh = 0.0
        slots[i].estimated_net_consumption_kwh = slots[i].avg_house_consumption_kwh

    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=10.0,
        target_kwh=50.0,
        capacity_kwh=80.0,
        max_charge_per_slot=10.0,
        charger_efficiency=0.90,
        deadline_slot=10,
        session_charge_kw=6.0,
        fixed_session_only=True,
    )

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=2.0,  # battery has room to charge
        usable_kwh=10.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=None,
        ev_configs=[ev],
    )

    assert result is not None
    out_slots, _diag = result

    # Check that session-demand slots (LP 0-1, slots 14:00-16:00 at 60-min)
    # don't get BatteriesChargeGrid.  They may get BatteriesChargeSolar if
    # PV available.
    for lp_idx in range(2):
        slot = out_slots[lp_idx]
        rec = slot.recommendation
        assert rec != "batteries_charge_grid", (
            f"Slot at {slot.start} has BatteriesChargeGrid during session demand"
        )
        # Battery charge, if any, should be via solar
        if slot.batteries_charged_kwh > 0:
            assert rec == "batteries_charge_solar", (
                f"Slot at {slot.start}: battery charged {slot.batteries_charged_kwh} kWh "
                f"with recommendation={rec}, expected batteries_charge_solar"
            )


# ---------------------------------------------------------------------------
# Regression tests for SESSION_SLOTS resolution behaviour (issue #639)
# ---------------------------------------------------------------------------


def _session_slot_count(interval_minutes: int) -> int:
    """Return expected SESSION_SLOTS for a given interval_minutes.

    2 hours / (interval_minutes / 60) => rounded integer slot count.
    """
    slot_hours = interval_minutes / 60.0
    return round(2.0 / slot_hours)


@_pytestmark_scipy
def test_session_slots_at_15min_resolution():
    """At 15-min resolution, session EV demand covers first 8 slots."""
    interval_minutes = 15
    expected_slots = _session_slot_count(interval_minutes)
    assert expected_slots == 8

    # Build enough slots: 16 slots × 15 min = 4 hours coverage
    slots = _build_slots(
        16, start_hour=14, import_price=0.20, interval_minutes=interval_minutes
    )

    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=10.0,
        target_kwh=30.0,
        capacity_kwh=50.0,
        max_charge_per_slot=3.0,
        charger_efficiency=0.90,
        deadline_slot=14,
        session_charge_kw=6.0,
        fixed_session_only=True,
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
    out_slots, _diag = result

    # At 15-min, slot_hours = 0.25, so session demand per slot is 6.0 * 0.25 = 1.5 kWh AC
    session_ac_per_slot = 6.0 * 0.25

    for lp_idx in range(expected_slots):
        slot = out_slots[lp_idx]
        assert slot.ev_total_planned_load_kwh == pytest.approx(
            session_ac_per_slot, rel=0.05
        ), (
            f"Slot at {slot.start} (LP {lp_idx}): expected {session_ac_per_slot} kWh AC, "
            f"got {slot.ev_total_planned_load_kwh}"
        )


@_pytestmark_scipy
def test_session_slots_at_30min_resolution():
    """At 30-min resolution, session EV demand covers first 4 slots."""
    interval_minutes = 30
    expected_slots = _session_slot_count(interval_minutes)
    assert expected_slots == 4

    # 8 slots × 30 min = 4 hours coverage
    slots = _build_slots(
        8, start_hour=14, import_price=0.20, interval_minutes=interval_minutes
    )

    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=10.0,
        target_kwh=30.0,
        capacity_kwh=50.0,
        max_charge_per_slot=5.0,
        charger_efficiency=0.90,
        deadline_slot=6,
        session_charge_kw=6.0,
        fixed_session_only=True,
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
    out_slots, _diag = result

    # At 30-min, slot_hours = 0.5, so session demand per slot is 6.0 * 0.5 = 3.0 kWh AC
    session_ac_per_slot = 6.0 * 0.5

    for lp_idx in range(expected_slots):
        slot = out_slots[lp_idx]
        assert slot.ev_total_planned_load_kwh == pytest.approx(
            session_ac_per_slot, rel=0.05
        ), (
            f"Slot at {slot.start} (LP {lp_idx}): expected {session_ac_per_slot} kWh AC, "
            f"got {slot.ev_total_planned_load_kwh}"
        )


@_pytestmark_scipy
def test_session_slots_at_60min_resolution():
    """At 60-min resolution, session EV demand covers first 2 slots."""
    interval_minutes = 60
    expected_slots = _session_slot_count(interval_minutes)
    assert expected_slots == 2

    # 8 slots × 60 min = 8 hours coverage
    slots = _build_slots(
        8, start_hour=14, import_price=0.20, interval_minutes=interval_minutes
    )

    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=10.0,
        target_kwh=30.0,
        capacity_kwh=50.0,
        max_charge_per_slot=10.0,
        charger_efficiency=0.90,
        deadline_slot=6,
        session_charge_kw=6.0,
        fixed_session_only=True,
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
    out_slots, _diag = result

    # At 60-min, slot_hours = 1.0, so session demand per slot is 6.0 * 1.0 = 6.0 kWh AC
    session_ac_per_slot = 6.0 * 1.0

    for lp_idx in range(expected_slots):
        slot = out_slots[lp_idx]
        assert slot.ev_total_planned_load_kwh == pytest.approx(
            session_ac_per_slot, rel=0.05
        ), (
            f"Slot at {slot.start} (LP {lp_idx}): expected {session_ac_per_slot} kWh AC, "
            f"got {slot.ev_total_planned_load_kwh}"
        )


# ---------------------------------------------------------------------------
# Session certainty bounded by control authority (issue #789)
# ---------------------------------------------------------------------------


@_pytestmark_scipy
def test_partial_current_managed_session_uses_planner_power() -> None:
    """A managed current-slot command is independent of measured session power."""
    now = _NOW + timedelta(minutes=50)
    slots = _build_slots(5, start_hour=14, import_price=0.20, interval_minutes=60)
    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=0.0,
        target_kwh=1.0,
        capacity_kwh=50.0,
        max_charge_per_slot=10.0,
        charger_efficiency=1.0,
        charger_min_power_w=1000.0,
        deadline_slot=4,
        session_charge_kw=3.62,
    )

    result = solve_milp(
        slots,
        now,
        current_kwh=0.0,
        usable_kwh=10.0,
        max_charge_per_slot=1.0,
        max_discharge_per_slot=0.0,
        ev_configs=[ev],
    )

    assert result is not None
    out, _diagnostics = result
    # HSEM controls this charger, so no two-hour reservation is manufactured.
    assert all(slot.ev_charger_calculated_power == 0 for slot in out[1:])


@_pytestmark_scipy
def test_managed_session_does_not_expand_to_nine_fixed_slots() -> None:
    """A measured 8.899 kW session cannot create the former nine-slot blowout."""
    now = _NOW + timedelta(minutes=5)
    slots = _build_slots(
        12,
        start_hour=14,
        import_price=0.20,
        consumption_kwh=0.0,
        interval_minutes=15,
    )
    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=0.0,
        target_kwh=6.3,
        capacity_kwh=64.0,
        max_charge_per_slot=2.76,
        charger_efficiency=1.0,
        charger_min_power_w=4140.0,
        deadline_slot=8,
        session_charge_kw=8.899,
        charger_phase_topology=EV_TOPOLOGY_THREE_PHASE_BALANCED,
    )

    result = solve_milp(
        slots,
        now,
        current_kwh=0.0,
        usable_kwh=10.0,
        max_charge_per_slot=1.0,
        max_discharge_per_slot=0.0,
        ev_configs=[ev],
    )

    assert result is not None
    out, _diagnostics = result
    active_commands = [
        slot.ev_charger_calculated_power
        for slot in out
        if slot.ev_charger_calculated_power > 1e-9
    ]
    total_dc = sum(slot.ev_total_planned_load_kwh for slot in out)
    # Every active command lies on the 3-amp-step (three-phase 690 W)
    # lattice between the startup minimum and rated nameplate; the measured
    # 8.899 kW session never pins slot 0 (issue #797).
    assert all(
        4_140.0 <= command <= 11_040.0 and command % 690.0 == pytest.approx(0.0)
        for command in active_commands
    )
    assert len(active_commands) <= 4
    assert 6.3 <= total_dc <= 6.3 + 1.035 + 1e-9
    assert [slot.ev_total_planned_load_kwh for slot in out[9:]] == pytest.approx(
        [0.0, 0.0, 0.0]
    )


@_pytestmark_scipy
def test_unmanaged_partial_session_retains_two_hour_certainty_window() -> None:
    """An unmanaged charger retains observed power across its bounded window."""
    now = _NOW + timedelta(minutes=50)
    slots = _build_slots(5, start_hour=14, import_price=0.20, interval_minutes=60)
    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=0.0,
        target_kwh=0.0,
        capacity_kwh=20.0,
        max_charge_per_slot=6.0,
        charger_efficiency=1.0,
        charger_min_power_w=0.0,
        deadline_slot=None,
        session_charge_kw=6.0,
        fixed_session_only=True,
    )

    result = solve_milp(
        slots,
        now,
        current_kwh=0.0,
        usable_kwh=10.0,
        max_charge_per_slot=1.0,
        max_discharge_per_slot=0.0,
        ev_configs=[ev],
    )

    assert result is not None
    out, _diagnostics = result
    assert [slot.ev_total_planned_load_kwh for slot in out[:3]] == pytest.approx(
        [1.0, 6.0, 6.0]
    )
    assert [slot.ev_charger_calculated_power for slot in out[:3]] == pytest.approx(
        [0.0, 0.0, 0.0]
    )
    assert out[3].ev_total_planned_load_kwh == pytest.approx(0.0)


@_pytestmark_scipy
def test_managed_session_reaches_target_with_bounded_amp_overshoot() -> None:
    """An executable plan reaches target within one activation quantum."""
    slots = _build_slots(4, start_hour=14, import_price=0.20, interval_minutes=60)
    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=0.0,
        target_kwh=1.0,
        capacity_kwh=10.0,
        max_charge_per_slot=6.0,
        charger_efficiency=1.0,
        charger_min_power_w=0.0,
        deadline_slot=3,
        session_charge_kw=6.0,
    )

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=10.0,
        max_charge_per_slot=1.0,
        max_discharge_per_slot=0.0,
        ev_configs=[ev],
    )

    assert result is not None
    out, diagnostics = result
    # One executable single-phase amp is 0.23 kWh in an hourly slot. The
    # target-cap constraint now permits one activation quantum (the
    # charger's startup minimum) above the exact 1.0 kWh target, so the
    # nearest reachable whole-amp point (5 A / 1.15 kWh) is used instead of
    # rounding down to an avoidable shortfall (issue #797).
    total_dc = sum(slot.ev_total_planned_load_kwh for slot in out)
    assert 1.0 <= total_dc <= 1.23 + 1e-9
    assert max(slot.ev_charger_calculated_power for slot in out) == pytest.approx(
        1_150.0
    )
    assert diagnostics["ev"]["ev0"]["deadline_penalty_kwh"] == pytest.approx(0.0)
    assert diagnostics["ev"]["ev0"]["deadline_met"] is True


@_pytestmark_scipy
def test_managed_session_never_waives_post_deadline_zero() -> None:
    """A live charger cannot create fixed demand after its deadline slot."""
    slots = _build_slots(4, start_hour=14, import_price=0.20, interval_minutes=60)
    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=0.0,
        target_kwh=12.0,
        capacity_kwh=20.0,
        max_charge_per_slot=6.0,
        charger_efficiency=1.0,
        charger_min_power_w=0.0,
        deadline_slot=0,
        session_charge_kw=6.0,
    )

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=10.0,
        max_charge_per_slot=1.0,
        max_discharge_per_slot=0.0,
        ev_configs=[ev],
    )

    assert result is not None
    out, diagnostics = result
    assert out[0].ev_charger_calculated_power == pytest.approx(5980.0)
    assert [slot.ev_total_planned_load_kwh for slot in out[1:]] == pytest.approx(
        [0.0, 0.0, 0.0]
    )
    assert diagnostics["ev"]["ev0"]["deadline_penalty_kwh"] == pytest.approx(6.02)


@_pytestmark_scipy
def test_mixed_managed_and_unmanaged_sessions_keep_distinct_windows() -> None:
    """An unmanaged EV's window cannot freeze the managed EV for two hours."""
    slots = _build_slots(4, start_hour=14, import_price=0.20, consumption_kwh=0.0)
    slots[1].price = SlotPrice(import_price=2.0, export_price=0.0)
    slots[2].price = SlotPrice(import_price=0.01, export_price=0.0)
    managed = EVConfig(
        enabled=True,
        initial_soc_kwh=0.0,
        target_kwh=12.0,
        capacity_kwh=20.0,
        max_charge_per_slot=6.0,
        charger_efficiency=1.0,
        charger_min_power_w=0.0,
        deadline_slot=2,
        session_charge_kw=6.0,
    )
    unmanaged = EVConfig(
        enabled=True,
        initial_soc_kwh=0.0,
        target_kwh=0.0,
        capacity_kwh=10.0,
        max_charge_per_slot=3.0,
        charger_efficiency=1.0,
        charger_min_power_w=0.0,
        deadline_slot=None,
        session_charge_kw=3.0,
        fixed_session_only=True,
        is_second=True,
    )

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=10.0,
        max_charge_per_slot=1.0,
        max_discharge_per_slot=0.0,
        ev_configs=[managed, unmanaged],
    )

    assert result is not None
    out, _diagnostics = result
    # The unmanaged EV remains fixed in slot 1. At most one executable amp of
    # managed energy may supplement it to close the target lattice; the live
    # 6 kW observation never freezes the managed slot (issue #797).
    managed_slot_1_kwh = out[1].ev_charger_calculated_power / 1000.0
    assert out[1].ev_total_planned_load_kwh == pytest.approx(3.0 + managed_slot_1_kwh)
    assert managed_slot_1_kwh <= 0.23 + 1e-9
    assert out[1].ev_second_charger_calculated_power == pytest.approx(0.0)
    assert out[2].ev_charger_calculated_power == pytest.approx(5980.0)


@_pytestmark_scipy
def test_managed_session_below_startup_minimum_emits_no_invalid_command() -> None:
    """A low measurement cannot ratchet a managed command below its minimum."""
    slots = _build_slots(4, start_hour=14, import_price=0.20, interval_minutes=60)
    slots[0].price = SlotPrice(import_price=0.01, export_price=0.0)
    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=0.0,
        target_kwh=4.0,
        capacity_kwh=10.0,
        max_charge_per_slot=2.0,
        charger_efficiency=1.0,
        charger_min_power_w=1000.0,
        deadline_slot=3,
        session_charge_kw=0.5,
    )

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=10.0,
        max_charge_per_slot=1.0,
        max_discharge_per_slot=0.0,
        ev_configs=[ev],
    )

    assert result is not None
    out, _diagnostics = result
    # The 0.5 kW measurement is telemetry only; the managed command is
    # either zero or at/above the charger's startup minimum (5 A / 1150 W
    # single-phase), never a fractional/sub-minimum publish (issue #797).
    active_commands = [
        slot.ev_charger_calculated_power
        for slot in out
        if slot.ev_charger_calculated_power > 0.0
    ]
    assert out[0].ev_charger_calculated_power >= 1_150.0
    assert all(command >= 1_150.0 for command in active_commands)
    assert all(command % 230.0 == pytest.approx(0.0) for command in active_commands)


@_pytestmark_scipy
def test_at_target_managed_live_session_does_not_create_fixed_demand() -> None:
    """Lingering managed power telemetry cannot author two hours of demand."""
    slots = _build_slots(4, start_hour=14, import_price=0.20, interval_minutes=60)
    inp = PlannerInput(
        now_iso=_NOW.isoformat(),
        interval_minutes=60,
        ev_planned_load_enabled=True,
        ev_planned_load_connected=True,
        ev_planned_load_smart_charging_enabled=True,
        ev_planned_load_current_soc_pct=80.0,
        ev_planned_load_target_soc_pct=80.0,
        ev_planned_load_battery_capacity_kwh=50.0,
        ev_planned_load_charger_power_kw=6.0,
        ev_planned_load_charger_efficiency_pct=100.0,
        ev_session_charge_kw=6.0,
    )

    configs = _build_ev_configs_for_milp(inp, slots, _NOW)

    assert configs is not None
    assert len(configs) == 1
    # A managed session at/above target becomes a command-zero sentinel
    # (issue #797): it retains its current-slot discharge permission/ceiling
    # without ever manufacturing a two-hour fixed-demand reservation.
    assert configs[0].max_charge_per_slot == pytest.approx(0.0)
    assert configs[0].target_kwh == pytest.approx(configs[0].initial_soc_kwh)
    assert configs[0].fixed_session_only is False
    assert configs[0].session_charge_kw == pytest.approx(6.0)

    for slot in slots:
        slot.avg_house_consumption_kwh = 1.0
        slot.estimated_net_consumption_kwh = 1.0
        slot.price = SlotPrice(import_price=1.0, export_price=0.0)
    result = solve_milp(
        slots,
        _NOW,
        current_kwh=5.0,
        usable_kwh=5.0,
        max_charge_per_slot=1.0,
        max_discharge_per_slot=2.0,
        ev_configs=configs,
        no_export=True,
    )

    assert result is not None
    out, _diagnostics = result
    assert all(slot.ev_charger_calculated_power == 0.0 for slot in out)
    assert out[0].batteries_discharged_kwh == pytest.approx(0.0)
    assert any(slot.batteries_discharged_kwh > 0.5 for slot in out[1:])

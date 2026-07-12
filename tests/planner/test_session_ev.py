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
from custom_components.hsem.planner.milp_optimizer import is_scipy_available, solve_milp
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
    """Session EV charge at 6 kW is fixed for first 2 hours of future slots.

    At 60-min resolution, this covers 2 slots.  The EV config has
    session_charge_kw=6.0, so those 2 hourly slots should each show
    6.0 * 1h = 6.0 kWh AC load.  Beyond the 2-hour session window,
    the MILP decides EV charging as usual.
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
    # (LP slots 0-1 → real slots 15:00-16:00)
    session_ac_per_slot = 6.0 * 1.0  # kW * hours = kWh AC

    for lp_idx in range(2):
        slot = out_slots[lp_idx + 1]  # +1 because slot 0 is past
        assert slot.ev_total_planned_load_kwh == pytest.approx(
            session_ac_per_slot, rel=0.05
        ), (
            f"Slot at {slot.start}: expected {session_ac_per_slot} kWh AC, "
            f"got {slot.ev_total_planned_load_kwh}"
        )


@_pytestmark_scipy
def test_session_ev_fallback_beyond_session_window():
    """Beyond the 2-hour session window, EV charging falls back to MILP decision.

    At 60-min resolution, the session window is 2 slots.  The EV has a
    deadline target that the session demand won't meet alone, so the MILP
    should charge the remaining energy in slots beyond the session window.
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

    # At 60-min, first 2 slots: session demand provides 6 kWh AC each
    # = 12 kWh total AC = 10.8 kWh DC.  Target is 60 kWh DC.
    # The remaining 60 - 10.8 = 49.2 kWh DC must be charged in later slots.

    # Compute total DC-side EV charge across all slots
    ev_total_dc = sum(
        s.ev_total_planned_load_kwh * 0.9  # AC → DC
        for s in out_slots
    )
    assert ev_total_dc == pytest.approx(60.0, rel=0.05), (
        f"Expected ~60 kWh DC total EV charge, got {ev_total_dc}"
    )

    # Verify session slots have session demand (first 2 future slots)
    session_ac = 6.0 * 1.0  # kWh AC per session slot
    for lp_idx in range(2):
        slot = out_slots[lp_idx + 1]
        assert slot.ev_total_planned_load_kwh == pytest.approx(session_ac, rel=0.05)


@_pytestmark_scipy
def test_grid_charging_blocked_during_session_demand():
    """Battery grid-charging is blocked during session EV demand slots.

    Even when import prices are low enough to charge the battery from grid,
    the session-demand constraint prevents BatteriesChargeGrid in session slots.
    The battery can still use BatteriesChargeSolar if PV surplus beyond the
    session EV demand is available.
    """
    # Build slots with high PV in early slots to provide battery charging opportunity.
    # Slots 15:00-16:00 (LP 0-1, 2 session slots at 60-min) have session EV +
    # enough PV for battery too.  Slot 17:00+ has only moderate PV.
    slots = _build_slots(12, start_hour=14, import_price=0.05, interval_minutes=60)

    # Give session slots generous PV: enough to cover EV (6 kWh) AND battery (5 kWh)
    for i in range(2):
        slots[i + 1].solcast_pv_estimate_kwh = 15.0
        slots[i + 1].estimated_net_consumption_kwh = (
            slots[i + 1].avg_house_consumption_kwh - 15.0
        )
    # Beyond session slots: no PV for charging
    for i in range(2, 11):
        slots[i + 1].solcast_pv_estimate_kwh = 0.0
        slots[i + 1].estimated_net_consumption_kwh = slots[
            i + 1
        ].avg_house_consumption_kwh

    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=10.0,
        target_kwh=50.0,
        capacity_kwh=80.0,
        max_charge_per_slot=10.0,
        charger_efficiency=0.90,
        deadline_slot=10,
        session_charge_kw=6.0,
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

    # Check that session-demand slots (LP 0-1, slots 15:00-16:00 at 60-min)
    # don't get BatteriesChargeGrid.  They may get BatteriesChargeSolar if
    # PV available.
    for lp_idx in range(2):
        slot = out_slots[lp_idx + 1]
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

"""Physical planner regression for the 2026-08-23 live-slot incident."""

from __future__ import annotations

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner import run_planner
from tests.planner.fixtures import make_summer_day_input

_NOW_ISO = "2026-08-23T09:15:05+02:00"


def _current_slot_for_live_power(house_w: float, solar_w: float) -> PlannedSlot:
    """Solve the reported 15-minute slot with one authoritative live pair."""
    planner_input = make_summer_day_input(
        now_iso=_NOW_ISO,
        interval_minutes=15,
        interval_length_hours=24,
        battery_soc_pct=10.0,
    )
    planner_input.house_power_includes_ev = False
    planner_input.live_house_consumption_w = house_w
    planner_input.live_house_consumption_available = True
    planner_input.live_solar_production_w = solar_w
    # With no value for immediate export, a genuine live surplus should refill
    # the empty battery instead of being sold. This isolates the live balance
    # transition from unrelated price arbitrage.
    for point in planner_input.price_points:
        point.export_price = 0.0

    output = run_planner(planner_input)
    return next(
        slot
        for slot in output.slots
        if slot.start.hour == 9 and slot.start.minute == 15
    )


def test_sustained_august_23_samples_correct_wait_to_solar_charge() -> None:
    """Robust 1950/2550 W authority reverses the transient 3390/2360 W plan.

    The transient boundary sample (a single low-house/low-solar tick right at
    the slot edge) reads as net demand; the sustained, corrected sample reads
    as a genuine PV surplus.  Both readings are injected as authoritative
    (``live_house_consumption_available=True``), so the current slot's
    ``avg_house_consumption_kwh`` / ``solcast_pv_estimate_kwh`` must track the
    live input exactly rather than falling back to forecast, and the sign of
    ``estimated_net_consumption_kwh`` must flip between the two readings.
    """
    boundary = _current_slot_for_live_power(3390.0, 2360.0)
    sustained = _current_slot_for_live_power(1950.0, 2550.0)

    assert boundary.avg_house_consumption_kwh == pytest.approx(0.848)
    assert boundary.solcast_pv_estimate_kwh == pytest.approx(0.590)
    assert boundary.estimated_net_consumption_kwh > 0.0

    assert sustained.avg_house_consumption_kwh == pytest.approx(0.487)
    assert sustained.solcast_pv_estimate_kwh == pytest.approx(0.637)
    assert sustained.estimated_net_consumption_kwh == pytest.approx(-0.150)

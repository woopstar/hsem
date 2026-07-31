"""Regression tests for issue #592 follow-up fixes.

Covers:
- The MILP ``no_export`` constraint (battery must never export to the grid
  when excess export is disabled).
- The live-injection spike cap in ``_inject_live_data_into_current_slot``
  (unmetered EV load must not inflate the current slot's house consumption).
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.engine_population import (
    _inject_live_data_into_current_slot,
)
from custom_components.hsem.planner.milp_optimizer import (
    is_scipy_available,
    solve_milp,
)
from custom_components.hsem.utils.prices import SlotPrice

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2024, 6, 15, 12, 0, tzinfo=_TZ)


def _make_slot(
    *,
    hour: int,
    import_price: float = 0.50,
    export_price: float = 0.40,
    pv_kwh: float = 0.0,
    consumption_kwh: float = 0.3,
) -> PlannedSlot:
    start = datetime(2024, 6, 15, hour, 0, tzinfo=_TZ)
    s = PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=import_price, export_price=export_price),
    )
    s.avg_house_consumption_kwh = consumption_kwh
    s.solcast_pv_estimate_kwh = pv_kwh
    s.ev_planned_load_kwh = 0.0
    s.estimated_net_consumption_kwh = consumption_kwh - pv_kwh
    return s


# ---------------------------------------------------------------------------
# no_export constraint (issue #592 — battery dumped 5 kW into the grid
# while excess export was disabled)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not is_scipy_available(), reason="scipy not available")
def test_no_export_caps_discharge_at_house_load():
    """With no_export=True the battery covers house load but cannot export."""
    # Fully charged battery, expensive prices, export price nearly as high —
    # without the constraint the LP would dump the battery into the grid.
    slots = [
        _make_slot(hour=h, import_price=3.0, export_price=2.9, consumption_kwh=0.4)
        for h in range(12, 16)
    ]

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=9.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        no_export=True,
    )
    assert milp_result is not None
    result, _diag = milp_result

    for s in result:
        # Discharge may at most cover the slot's house load (0.4 kWh AC,
        # divided by discharge efficiency on the DC side).
        assert s.batteries_discharged_kwh <= 0.4 / 0.97 + 1e-6
        # The battery must not contribute to grid export.
        assert s.grid_export_kwh <= 1e-6


@pytest.mark.skipif(not is_scipy_available(), reason="scipy not available")
def test_no_export_blocks_discharge_in_pv_surplus_slot():
    """With no_export=True and base_load=0 (PV surplus), discharge cap is 0."""
    slots = [
        _make_slot(hour=12, pv_kwh=3.0, consumption_kwh=0.4),
        _make_slot(hour=13, pv_kwh=3.0, consumption_kwh=0.4),
    ]

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=9.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        no_export=True,
    )
    assert milp_result is not None
    result, _diag = milp_result

    for s in result:
        assert s.batteries_discharged_kwh <= 1e-6


@pytest.mark.skipif(not is_scipy_available(), reason="scipy not available")
def test_export_allowed_when_no_export_false():
    """Sanity check: with no_export=False the LP may export battery energy."""
    slots = [
        _make_slot(hour=h, import_price=3.0, export_price=2.9, consumption_kwh=0.4)
        for h in range(12, 16)
    ]

    milp_result = solve_milp(
        slots,
        _NOW,
        current_kwh=9.0,
        usable_kwh=9.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        no_export=False,
    )
    assert milp_result is not None
    result, _diag = milp_result

    total_export = sum(float(s.grid_export_kwh) for s in result)
    assert total_export > 0.1, "LP should export battery energy when allowed"


# ---------------------------------------------------------------------------
# Live injection spike cap (issue #592 — unmetered EV load inflating the
# current slot; zero-forecast edge case where the 3x ratio test is degenerate)
# ---------------------------------------------------------------------------


class _Inp:
    """Minimal planner-input stand-in for live injection tests."""

    def __init__(
        self,
        *,
        live_house_w: float,
        live_solar_w: float = 0.0,
        includes_ev: bool = True,
        ev_kw: float | None = None,
    ) -> None:
        self.interval_minutes = 60
        self.live_solar_production_w = live_solar_w
        self.live_house_consumption_w = live_house_w
        self.house_power_includes_ev = includes_ev
        self.ev_session_charge_kw = ev_kw
        self.ev_second_session_charge_kw = None


def _current_slot(forecast_kwh: float) -> PlannedSlot:
    start = datetime(2024, 6, 15, 12, 0, tzinfo=_TZ)
    s = PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=0.5, export_price=0.4),
    )
    s.avg_house_consumption_kwh = forecast_kwh
    s.solcast_pv_estimate_kwh = 0.0
    return s


def test_live_injection_caps_spike_at_forecast():
    """A >3x live spike is capped at the forecast (unmetered EV load)."""
    slot = _current_slot(forecast_kwh=0.4)
    inp = _Inp(live_house_w=4000.0)  # 4 kW — 10x the 0.4 kWh forecast
    _inject_live_data_into_current_slot([slot], inp, _NOW)  # type: ignore[arg-type]
    assert slot.avg_house_consumption_kwh == pytest.approx(0.4)


def test_live_injection_caps_spike_when_forecast_zero():
    """With a ~0 forecast the ratio test is degenerate; an absolute floor
    must still cap a multi-kW EV spike."""
    slot = _current_slot(forecast_kwh=0.0)
    inp = _Inp(live_house_w=3600.0)  # 3.6 kW EV spike, no forecast
    _inject_live_data_into_current_slot([slot], inp, _NOW)  # type: ignore[arg-type]
    assert slot.avg_house_consumption_kwh <= 0.05 + 1e-9


def test_live_injection_subtracts_known_ev_power():
    """Known EV power is subtracted before injection (Layer 1)."""
    slot = _current_slot(forecast_kwh=0.4)
    # House CT reads 4.0 kW total, EV sensor reports 3.6 kW → 0.4 kW house.
    inp = _Inp(live_house_w=4000.0, ev_kw=3.6)
    _inject_live_data_into_current_slot([slot], inp, _NOW)  # type: ignore[arg-type]
    assert slot.avg_house_consumption_kwh == pytest.approx(0.4)


def test_live_injection_normal_load_passes_through():
    """A normal live reading within 3x of forecast is injected unchanged."""
    slot = _current_slot(forecast_kwh=0.4)
    inp = _Inp(live_house_w=600.0)  # 0.6 kWh — 1.5x forecast, fine
    _inject_live_data_into_current_slot([slot], inp, _NOW)  # type: ignore[arg-type]
    assert slot.avg_house_consumption_kwh == pytest.approx(0.6)

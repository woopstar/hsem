"""Regression coverage for normalized house/EV load accounting (issue #1080)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.engine_ev import _build_and_inject_for_ev
from custom_components.hsem.planner.ev_load_accounting import split_house_and_ev_load
from custom_components.hsem.planner.milp._ev_net_load import (
    resolve_active_evs_and_net_load,
)
from custom_components.hsem.planner.soc_simulation import simulate_soc
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations
from tests.planner.fixtures import make_flat_price_input

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2026, 9, 19, 6, 0, tzinfo=_TZ)


def _slot(
    *, house_kwh: float, planned_ev_kwh: float, accounted_ev_kwh: float
) -> PlannedSlot:
    slot = PlannedSlot(
        start=_NOW,
        end=_NOW + timedelta(minutes=15),
        price=SlotPrice(import_price=2.0, export_price=1.0),
    )
    slot.avg_house_consumption_kwh = house_kwh
    slot.ev_planned_load_kwh = planned_ev_kwh
    slot.ev_accounted_load_kwh = accounted_ev_kwh
    slot.ev_total_planned_load_kwh = planned_ev_kwh + accounted_ev_kwh
    slot.recommendation = Recommendations.BatteriesDischargeMode.value
    return slot


def _simulate(slot: PlannedSlot) -> None:
    simulate_soc(
        [slot],
        _NOW,
        current_kwh=4.0,
        usable_kwh=5.0,
        max_capacity_kwh=5.0,
        max_charge_per_slot=1.25,
        max_discharge_per_slot=1.25,
        rated_kwh=5.0,
        discharge_efficiency_pct=100.0,
    )


def test_milp_mixed_two_ev_accounting_keeps_embedded_ev_subtraction() -> None:
    """A removed primary session cannot erase the accounted second EV."""
    slot = _slot(house_kwh=0.282, planned_ev_kwh=0.428, accounted_ev_kwh=0.200)
    primary = EVConfig(
        enabled=True,
        capacity_kwh=10.0,
        max_charge_per_slot=1.0,
        base_load_includes_ev=True,
        current_session_removed_from_base=True,
        session_charge_kw=1.712,
    )
    second = EVConfig(
        enabled=True,
        capacity_kwh=10.0,
        max_charge_per_slot=1.0,
        base_load_includes_ev=True,
        session_charge_kw=0.8,
        is_second=True,
    )

    resolved = resolve_active_evs_and_net_load(
        ev_configs=[primary, second],
        slots=[slot],
        future_idx=[0],
        now=_NOW,
        net_load=np.zeros(1),
        pv_avail=np.zeros(1),
        base_load=np.zeros(1),
    )

    assert resolved.net_load[0] == pytest.approx(0.082)
    assert resolved.base_load[0] == pytest.approx(0.082)


def test_current_removed_session_is_injected_as_separate_ev_load() -> None:
    """Pre-MILP planning applies current-slot removal provenance per EV."""
    slot = _slot(house_kwh=0.082, planned_ev_kwh=0.0, accounted_ev_kwh=0.0)
    raw = [0.0]
    injected = [0.0]

    plan = _build_and_inject_for_ev(
        enabled=True,
        connected=True,
        smart=True,
        soc=0.0,
        target=80.0,
        cap_kwh=10.0,
        pwr_kw=3.0,
        eff=100.0,
        min_pwr_w=1_380.0,
        deadline=slot.end,
        base_includes=True,
        current_session_removed_from_base=True,
        allow_past_target=False,
        label="primary",
        now=_NOW,
        slots=[slot],
        slot_starts=[slot.start],
        slot_ends=[slot.end],
        slot_prices=[slot.price.import_price],
        slot_net_surplus=[0.0],
        combined_ev_raw_load=raw,
        combined_ev_injected_load=injected,
        warnings=[],
    )

    assert plan is not None
    assert raw[0] > 1e-9
    assert injected[0] == pytest.approx(raw[0])


def test_production_planner_preserves_reported_house_load_and_cost_identity() -> None:
    """Production wiring keeps 0.082 kWh house demand beside a 0.628 kWh EV."""
    inp = make_flat_price_input(
        now_iso=_NOW.isoformat(),
        import_price=2.0,
        battery_soc_pct=80.0,
        interval_minutes=15,
        interval_length_hours=12,
    )
    for average in inp.consumption_averages:
        if average.hour == _NOW.hour:
            average.avg_1d = 0.328
            average.avg_3d = 0.328
            average.avg_7d = 0.328
            average.avg_14d = 0.328
    inp.house_power_includes_ev = True
    inp.live_house_consumption_available = True
    inp.live_house_consumption_w = 2_840.0
    inp.ev_session_charge_kw = 2.512
    inp.ev_planned_load_base_load_includes_ev = False
    inp.ev_planned_load_force_max_discharge_power = True
    inp.ev_planned_load_max_discharge_power_w = 5_000.0

    output = run_planner(inp)
    current = next(slot for slot in output.slots if slot.start == _NOW)
    winner = next(
        candidate
        for candidate in output.candidates
        if candidate.name == output.winner_name
    )

    assert current.avg_house_consumption_kwh == pytest.approx(0.082)
    assert current.ev_planned_load_kwh == pytest.approx(0.628)
    assert current.ev_accounted_load_kwh == pytest.approx(0.0)
    assert current.batteries_discharged_kwh == pytest.approx(0.082 / 0.97, abs=1e-3)
    assert current.grid_import_kwh == pytest.approx(0.628, abs=1e-3)
    assert winner.slots is output.slots
    assert winner._cost is not None
    assert output.plan_cost is not None
    assert winner._cost.total_cost == pytest.approx(output.plan_cost.total_cost)
    for slot in output.slots:
        assert 0.0 <= slot.estimated_battery_soc_pct <= 100.0


def test_reported_removed_session_preserves_house_discharge() -> None:
    """0.082 kWh house plus 0.628 kWh EV never becomes -0.546 kWh house."""
    slot = _slot(house_kwh=0.082, planned_ev_kwh=0.628, accounted_ev_kwh=0.0)

    house_load, ev_load = split_house_and_ev_load(slot)
    _simulate(slot)

    assert house_load == pytest.approx(0.082)
    assert ev_load == pytest.approx(0.628)
    assert slot.batteries_discharged_kwh == pytest.approx(0.082)
    assert slot.grid_import_kwh == pytest.approx(0.628)
    assert slot.ev_total_planned_load_kwh == pytest.approx(
        slot.ev_planned_load_kwh + slot.ev_accounted_load_kwh
    )
    assert 0.0 <= slot.estimated_battery_soc_pct <= 100.0


def test_embedded_session_produces_same_pure_house_split() -> None:
    """An actually embedded EV is subtracted exactly once from the baseline."""
    slot = _slot(house_kwh=0.710, planned_ev_kwh=0.0, accounted_ev_kwh=0.628)

    house_load, ev_load = split_house_and_ev_load(slot)
    _simulate(slot)

    assert house_load == pytest.approx(0.082)
    assert ev_load == pytest.approx(0.628)
    assert slot.batteries_discharged_kwh == pytest.approx(0.082)
    assert slot.grid_import_kwh == pytest.approx(0.628)


def test_two_ev_mixed_accounting_preserves_energy_balance() -> None:
    """One normalized-out EV and one embedded EV retain separate accounting."""
    slot = _slot(house_kwh=0.282, planned_ev_kwh=0.428, accounted_ev_kwh=0.200)

    house_load, ev_load = split_house_and_ev_load(slot)
    _simulate(slot)

    assert house_load == pytest.approx(0.082)
    assert ev_load == pytest.approx(0.628)
    assert slot.batteries_discharged_kwh == pytest.approx(0.082)
    assert slot.grid_import_kwh == pytest.approx(0.628)
    supply = slot.grid_import_kwh + slot.batteries_discharged_kwh
    demand = house_load + ev_load
    assert supply == pytest.approx(demand, abs=1e-6)
    assert slot.estimated_battery_capacity_kwh == pytest.approx(3.918)

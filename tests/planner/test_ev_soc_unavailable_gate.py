"""Regression tests for the unavailable-EV-SoC safety gate (issue #988).

An ``unavailable``/``unknown`` EV SoC sensor used to be coerced to ``0.0``
at two layers (``state_collector.py`` and ``coordinator_builder.py``), so
after an HA restart a car at 83 % SoC looked empty to the planner — which
then scheduled ~69 kWh against a 07:00 deadline and charged straight
through the evening price peak to 100 %.

``convert_to_float()`` deliberately returns ``None`` for missing data so
callers can tell *unknown* from *real zero*. These tests pin the contract:

- ``None`` propagates through ``LiveState`` and ``PlannerInput`` unchanged;
- the baseline EV planner returns an inert ``unavailable`` plan;
- the MILP excludes the EV (logged in the ``[milp_ev] … excluded`` style);
- a genuine ``0.0`` reading is still honoured as an empty battery;
- a charge-past-target EV can never carry session pins, so the surplus-only
  constraint constrains every slot regardless of how session pinning
  evolves.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from homeassistant.const import STATE_UNAVAILABLE

from custom_components.hsem.coordinator_builder import build_planner_input
from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.engine_core import _build_ev_configs_for_milp
from custom_components.hsem.planner.ev_planner import (
    EVPlannerInput,
    build_ev_charging_plan,
)
from custom_components.hsem.planner.milp._session_window import (
    resolve_session_windows,
)
from custom_components.hsem.utils.prices import SlotPrice

_UTC = UTC
_NOW = datetime(2024, 6, 15, 19, 45, tzinfo=_UTC)  # incident timing: 19:45


def _dt(h: int) -> datetime:
    """Return a datetime on 2024-06-15 at hour h (UTC)."""
    return datetime(2024, 6, 15, h % 24, 0, 0, tzinfo=_UTC) + timedelta(days=h // 24)


def _ev_planner_input(**overrides: Any) -> EVPlannerInput:
    """Minimal EVPlannerInput: connected, smart, below target, 07:00 deadline."""
    kwargs: dict[str, Any] = {
        "enabled": True,
        "ev_connected": True,
        "smart_charging_enabled": True,
        "current_soc_pct": 20.0,
        "target_soc_pct": 80.0,
        "battery_capacity_kwh": 86.5,
        "charger_power_kw": 11.0,
        "charger_efficiency_pct": 92.0,
        "charger_min_power_w": 1380.0,
        "deadline": _NOW + timedelta(hours=11, minutes=15),  # 07:00 next day
        "now": _NOW,
    }
    kwargs.update(overrides)
    return EVPlannerInput(**kwargs)


def _slot_pairs(n: int = 24) -> tuple[list[datetime], list[datetime]]:
    starts = [_NOW + timedelta(hours=i) for i in range(n)]
    ends = [s + timedelta(hours=1) for s in starts]
    return starts, ends


def _planned_slots(n: int = 24, import_price: float = 2.0) -> list[PlannedSlot]:
    return [
        PlannedSlot(
            start=_NOW + timedelta(hours=i),
            end=_NOW + timedelta(hours=i + 1),
            price=SlotPrice(import_price=import_price, export_price=0.10),
        )
        for i in range(n)
    ]


class TestEvPlannerSocGuard:
    """build_ev_charging_plan must refuse to plan on an unknown SoC."""

    def test_unavailable_soc_returns_inert_unavailable_plan(self):
        starts, ends = _slot_pairs()
        plan = build_ev_charging_plan(
            _ev_planner_input(current_soc_pct=None),
            slots_start=starts,
            slots_end=ends,
            slot_net_surplus_kwh=[0.0] * len(starts),
            slot_import_price=[2.0] * len(starts),
        )

        assert plan.state == STATE_UNAVAILABLE
        assert plan.charging_slots == []
        assert plan.total_kwh_needed == pytest.approx(0.0)
        assert plan.current_slot_planned_load_kwh == pytest.approx(0.0)

    def test_real_zero_soc_still_plans_charging(self):
        """A genuine 0 % reading is an empty battery, not missing data."""
        starts, ends = _slot_pairs()
        plan = build_ev_charging_plan(
            _ev_planner_input(current_soc_pct=0.0),
            slots_start=starts,
            slots_end=ends,
            slot_net_surplus_kwh=[0.0] * len(starts),
            slot_import_price=[2.0] * len(starts),
        )

        assert plan.state in ("waiting", "charging")
        assert plan.charging_slots, "a real 0 % SoC must still plan charging"
        assert plan.total_kwh_needed == pytest.approx(0.8 * 86.5, rel=1e-3)


class TestPlannerInputPropagation:
    """LiveState/PlannerInput must keep an unavailable SoC as None."""

    @staticmethod
    def _build(live: LiveState) -> PlannerInput:
        cfg = SensorConfig()
        cfg.ev_planned_load_enabled = True
        cfg.ev_second_planned_load_enabled = True
        return build_planner_input(
            cfg=cfg,
            live=live,
            hourly_recommendations=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
        )

    def test_unavailable_soc_propagates_none(self):
        live = LiveState()
        live.ev_planned_load_current_soc_pct = None
        assert self._build(live).ev_planned_load_current_soc_pct is None

    def test_real_zero_soc_propagates_zero(self):
        live = LiveState()
        live.ev_planned_load_current_soc_pct = 0.0
        assert self._build(live).ev_planned_load_current_soc_pct == pytest.approx(0.0)

    def test_unavailable_second_ev_soc_propagates_none(self):
        live = LiveState()
        live.ev_second_planned_load_current_soc_pct = None
        assert self._build(live).ev_second_planned_load_current_soc_pct is None

    def test_live_state_soc_defaults_to_none(self):
        """The LiveState default is None (unknown), not a fabricated 0 %."""
        assert LiveState().ev_planned_load_current_soc_pct is None
        assert LiveState().ev_second_planned_load_current_soc_pct is None


_BASE_MILP_INPUT = PlannerInput(
    now_iso=_NOW.isoformat(),
    interval_minutes=60,
    interval_length_hours=24,
    ev_planned_load_enabled=True,
    ev_planned_load_connected=True,
    ev_planned_load_smart_charging_enabled=True,
    ev_planned_load_current_soc_pct=20.0,
    ev_planned_load_target_soc_pct=80.0,
    ev_planned_load_battery_capacity_kwh=86.5,
    ev_planned_load_charger_power_kw=11.0,
    ev_planned_load_charger_efficiency_pct=92.0,
    ev_planned_load_deadline=_NOW + timedelta(hours=11, minutes=15),
)


def _milp_input(**overrides: Any) -> PlannerInput:
    return dataclasses.replace(_BASE_MILP_INPUT, **overrides)  # type: ignore[arg-type]


class TestMilpEvSocGate:
    """The MILP must exclude an EV whose SoC is unknown."""

    def test_unavailable_soc_excludes_ev(self):
        configs = _build_ev_configs_for_milp(
            _milp_input(ev_planned_load_current_soc_pct=None),
            _planned_slots(),
            _NOW,
        )
        assert configs is None

    def test_unavailable_soc_excludes_managed_live_session(self):
        """A managed EV mid-session with unknown SoC must not keep planning.

        Exclusion retracts the command; the anti-flap layer then stops the
        session — the safe failure direction when the state of charge is
        unknown.
        """
        configs = _build_ev_configs_for_milp(
            _milp_input(
                ev_planned_load_current_soc_pct=None,
                ev_session_charge_kw=7.0,
            ),
            _planned_slots(),
            _NOW,
        )
        assert configs is None

    def test_unmanaged_live_session_still_accounted_without_soc(self):
        """An unmanaged live session needs no SoC — it is fixed demand.

        Excluding it would drop real physical load from the optimisation.
        """
        configs = _build_ev_configs_for_milp(
            _milp_input(
                ev_planned_load_enabled=False,
                ev_planned_load_connected=False,
                ev_planned_load_current_soc_pct=None,
                ev_session_charge_kw=7.0,
            ),
            _planned_slots(),
            _NOW,
        )
        assert configs is not None
        assert len(configs) == 1
        assert configs[0].fixed_session_only is True

    def test_real_zero_soc_included_normally(self):
        configs = _build_ev_configs_for_milp(
            _milp_input(ev_planned_load_current_soc_pct=0.0),
            _planned_slots(),
            _NOW,
        )
        assert configs is not None
        assert configs[0].initial_soc_kwh == pytest.approx(0.0)

    def test_second_ev_excluded_independently(self):
        configs = _build_ev_configs_for_milp(
            _milp_input(
                ev_second_planned_load_enabled=True,
                ev_second_planned_load_connected=True,
                ev_second_planned_load_smart_charging_enabled=True,
                ev_second_planned_load_current_soc_pct=None,
                ev_second_planned_load_target_soc_pct=80.0,
                ev_second_planned_load_battery_capacity_kwh=60.0,
                ev_second_planned_load_charger_power_kw=11.0,
                ev_second_planned_load_deadline=_NOW + timedelta(hours=11),
            ),
            _planned_slots(),
            _NOW,
        )
        assert configs is not None
        assert len(configs) == 1
        assert configs[0].is_second is False


class TestChargePastTargetPinningGuard:
    """charge_past_target EVs never carry session pins (issue #988, item 4).

    Pinning only exists for ``fixed_session_only`` EVs, and
    ``charge_past_target`` is only set for managed EVs, so the combination
    is already unreachable — this guard keeps it that way if session
    pinning ever evolves, because a pinned slot is exempt from the
    surplus-only constraint and could otherwise import from grid.
    """

    def _ev_config(self, **overrides: Any) -> EVConfig:
        kwargs: dict[str, Any] = {
            "enabled": True,
            "initial_soc_kwh": 40.0,
            "target_kwh": 50.0,
            "capacity_kwh": 50.0,
            "max_charge_per_slot": 5.0,
            "charger_efficiency": 1.0,
        }
        kwargs.update(overrides)
        return EVConfig(**kwargs)

    def test_charge_past_target_session_is_never_pinned(self):
        slots = _planned_slots(n=6)
        future_idx = list(range(6))
        ev = self._ev_config(
            charge_past_target=True,
            fixed_session_only=True,  # inconsistent on purpose
            session_charge_kw=7.0,
        )

        windows = resolve_session_windows(
            slots=slots,
            future_idx=future_idx,
            now=_NOW,
            active_evs=[ev],
        )

        assert windows.session_dc_by_ev == {}
        assert windows.has_session_demand is False

    def test_unmanaged_session_without_past_target_still_pinned(self):
        """The guard must not weaken the issue #615/#789 session handling."""
        slots = _planned_slots(n=6)
        future_idx = list(range(6))
        ev = self._ev_config(
            charge_past_target=False,
            fixed_session_only=True,
            session_charge_kw=7.0,
        )

        windows = resolve_session_windows(
            slots=slots,
            future_idx=future_idx,
            now=_NOW,
            active_evs=[ev],
        )

        assert windows.session_dc_by_ev[0], (
            "unmanaged live sessions must still pin their certainty window"
        )


def _engine_input(**overrides: Any) -> PlannerInput:
    """Full-engine input reproducing the 2026-09-12 incident shape.

    Evening peak prices (16-21), a car already above its 80 % target, and a
    07:00 next-morning deadline — the conditions under which a fabricated
    0 % SoC made the MILP plan ~69 kWh through the most expensive slots.
    """
    now_iso = _NOW.isoformat()
    prices = [
        PricePoint(hour=h, import_price=0.10, export_price=0.05) for h in range(24)
    ]
    for h in range(16, 21):
        prices[h] = PricePoint(hour=h, import_price=0.40, export_price=0.15)
    pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
    averages = [
        HourlyConsumptionAverage(
            hour=h, avg_1d=1.0, avg_3d=1.0, avg_7d=1.0, avg_14d=1.0
        )
        for h in range(24)
    ]
    kwargs: dict[str, Any] = {
        "now_iso": now_iso,
        "interval_minutes": 60,
        "interval_length_hours": 48,
        "battery_soc_pct": 50.0,
        "battery_rated_capacity_kwh": 10.0,
        "battery_end_of_discharge_soc_pct": 10.0,
        "battery_max_soc_pct": 90.0,
        "battery_max_charge_power_w": 5000.0,
        "battery_max_discharge_power_w": 5000.0,
        "battery_charge_efficiency_pct": 95.0,
        "battery_discharge_efficiency_pct": 95.0,
        "weight_1d": 25,
        "weight_3d": 30,
        "weight_7d": 30,
        "weight_14d": 15,
        "consumption_averages": averages,
        "price_points": prices,
        "solcast_slots": pv,
        "ev_planned_load_enabled": True,
        "ev_planned_load_connected": True,
        "ev_planned_load_smart_charging_enabled": True,
        "ev_planned_load_current_soc_pct": None,
        "ev_planned_load_target_soc_pct": 80.0,
        "ev_planned_load_battery_capacity_kwh": 86.5,
        "ev_planned_load_charger_power_kw": 11.0,
        "ev_planned_load_charger_efficiency_pct": 92.0,
        "ev_planned_load_deadline": _NOW + timedelta(hours=11, minutes=15),
        "ev_planned_load_base_load_includes_ev": False,
    }
    kwargs.update(overrides)
    return PlannerInput(**kwargs)


class TestEngineIncidentRegression:
    """The 2026-09-12 incident: unavailable SoC must plan zero EV charging."""

    def test_fabricated_zero_soc_would_plan_peak_charging(self):
        """Explicit precondition: with the pre-fix fabricated 0 % SoC this
        exact scenario plans EV charging in peak-priced slots — proving the
        regression test below can actually catch the bug."""
        out = run_planner(_engine_input(ev_planned_load_current_soc_pct=0.0))
        planned = sum(s.ev_planned_load_kwh for s in out.slots)
        assert planned > 60.0, (
            f"precondition broken: fabricated 0 % SoC should plan ~69 kWh, "
            f"got {planned:.1f} kWh"
        )

    def test_unavailable_soc_plans_zero_ev_charging(self):
        out = run_planner(_engine_input())

        assert all(s.ev_planned_load_kwh == pytest.approx(0.0) for s in out.slots), (
            "no slot may carry planned EV load when the SoC is unknown"
        )
        assert all(
            s.ev_charger_calculated_power == pytest.approx(0.0) for s in out.slots
        ), "no slot may command the charger when the SoC is unknown"
        if out.ev_charging_plan is not None:
            assert out.ev_charging_plan.state == STATE_UNAVAILABLE
            assert out.ev_charging_plan.charging_slots == []

    def test_recovery_when_soc_returns(self):
        """Once the SoC entity reports again, the next solve plans normally."""
        out = run_planner(_engine_input(ev_planned_load_current_soc_pct=20.0))
        planned = sum(s.ev_planned_load_kwh for s in out.slots)
        assert planned > 0.0

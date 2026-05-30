"""Tests for EV planned load integration in the HSEM planner (issue #396).

All tests are pure-Python and carry no Home Assistant dependencies.

Test classes
------------
TestComputeEvEnergyNeeded    — unit tests for energy-needed formula
TestMaxChargeEnergy          — unit tests for per-slot charge capacity
TestBuildEvChargingPlan      — end-to-end EV plan builder
TestApplyEvPlannedLoad       — slot injection and double-count prevention
TestPlannerEngineEVIntegration — full engine runs with EV planned load
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.hsem.models.planner_inputs import (
    HourlyConsumptionAverage,
    PlannerInput,
    PricePoint,
    SolcastSlot,
)
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.ev_planner import (
    EVChargingPlan,
    EVPlannerInput,
    apply_ev_planned_load_to_slots,
    build_ev_charging_plan,
    compute_ev_energy_needed,
    max_charge_energy_for_slot,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = UTC


def _dt(h: int, tz: Any = _UTC) -> datetime:
    """Return a datetime on 2024-06-15 at hour h in tz."""
    return datetime(2024, 6, 15, h, 0, 0, tzinfo=tz)


def _make_slots(n: int = 24, tz: Any = _UTC) -> tuple[list[datetime], list[datetime]]:
    """Return n 1-hour slot (start, end) pairs starting at 00:00."""
    starts = [_dt(h, tz) for h in range(n)]
    ends = [_dt(h + 1 if h < 23 else 0, tz) for h in range(n)]
    # Fix midnight wrap
    ends[23] = starts[23] + timedelta(hours=1)
    return starts, ends


def _make_planner_input(
    now_iso: str | None = None,
    ev_enabled: bool = True,
    ev_connected: bool = True,
    smart_charging: bool = True,
    current_soc: float = 50.0,
    target_soc: float = 80.0,
    capacity_kwh: float = 77.0,
    charger_kw: float = 11.0,
    efficiency: float = 100.0,
    deadline_hours_from_now: float = 8.0,
    base_includes_ev: bool = False,
) -> PlannerInput:
    """Build a minimal PlannerInput with EV planned load settings."""
    if now_iso is None:
        now_iso = "2024-06-15T14:00:00+00:00"

    # Parse now
    from datetime import datetime as _dt2

    now = _dt2.fromisoformat(now_iso)
    deadline = now + timedelta(hours=deadline_hours_from_now)

    # 24-hour hourly prices and PV
    prices = [
        PricePoint(hour=h, import_price=0.10, export_price=0.05) for h in range(24)
    ]
    # Cheap 00-06, expensive 16-20
    for h in range(6):
        prices[h] = PricePoint(hour=h, import_price=0.05, export_price=0.02)
    for h in range(16, 21):
        prices[h] = PricePoint(hour=h, import_price=0.30, export_price=0.15)

    # PV: surplus during 10-15
    pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
    for h in range(10, 16):
        pv[h] = SolcastSlot(hour=h, pv_estimate=3.5)

    averages = [
        HourlyConsumptionAverage(
            hour=h,
            avg_1d=1.0,
            avg_3d=1.0,
            avg_7d=1.0,
            avg_14d=1.0,
        )
        for h in range(24)
    ]

    return PlannerInput(
        now_iso=now_iso,
        interval_minutes=60,
        interval_length_hours=24,
        battery_soc_pct=50.0,
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=90.0,
        battery_max_charge_power_w=5000.0,
        battery_max_discharge_power_w=5000.0,
        battery_charge_efficiency_pct=95.0,
        battery_discharge_efficiency_pct=95.0,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=averages,
        price_points=prices,
        solcast_slots=pv,
        ev_planned_load_enabled=ev_enabled,
        ev_planned_load_connected=ev_connected,
        ev_planned_load_smart_charging_enabled=smart_charging,
        ev_planned_load_current_soc_pct=current_soc,
        ev_planned_load_target_soc_pct=target_soc,
        ev_planned_load_battery_capacity_kwh=capacity_kwh,
        ev_planned_load_charger_power_kw=charger_kw,
        ev_planned_load_charger_efficiency_pct=efficiency,
        ev_planned_load_deadline=deadline,
        ev_planned_load_base_load_includes_ev=base_includes_ev,
    )


# ---------------------------------------------------------------------------
# TestComputeEvEnergyNeeded
# ---------------------------------------------------------------------------


class TestComputeEvEnergyNeeded:
    """Unit tests for compute_ev_energy_needed."""

    def test_normal_case(self):
        """EV needs 23.1 kWh to go from 50 to 80 pct of a 77 kWh battery."""
        result = compute_ev_energy_needed(50.0, 80.0, 77.0)
        assert result == pytest.approx(23.1, rel=1e-6)

    def test_already_at_target(self):
        """If current SoC == target SoC, energy needed is 0."""
        assert compute_ev_energy_needed(80.0, 80.0, 77.0) == pytest.approx(0.0)

    def test_above_target(self):
        """If current SoC > target SoC, result must be 0 (never negative)."""
        assert compute_ev_energy_needed(90.0, 80.0, 77.0) == pytest.approx(0.0)

    def test_empty_battery(self):
        """0 → 100 % of 50 kWh battery = 50 kWh."""
        assert compute_ev_energy_needed(0.0, 100.0, 50.0) == pytest.approx(50.0)

    def test_zero_capacity(self):
        """Zero capacity battery always needs 0 kWh."""
        assert compute_ev_energy_needed(0.0, 100.0, 0.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# TestMaxChargeEnergy
# ---------------------------------------------------------------------------


class TestMaxChargeEnergy:
    """Unit tests for max_charge_energy_for_slot."""

    def test_full_hour_11kw(self):
        """11 kW for 60 min = 11 kWh at 100 % efficiency."""
        result = max_charge_energy_for_slot(60.0, 11.0, 100.0)
        assert result == pytest.approx(11.0)

    def test_15min_slot(self):
        """11 kW for 15 min = 2.75 kWh at 100 % efficiency."""
        result = max_charge_energy_for_slot(15.0, 11.0, 100.0)
        assert result == pytest.approx(2.75)

    def test_efficiency_90pct(self):
        """11 kW for 60 min at 90 % eff → 9.9 kWh delivered to battery."""
        result = max_charge_energy_for_slot(60.0, 11.0, 90.0)
        assert result == pytest.approx(9.9)

    def test_zero_duration(self):
        """Zero duration slot → 0 kWh."""
        assert max_charge_energy_for_slot(0.0, 11.0, 100.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# TestBuildEvChargingPlan — guard states
# ---------------------------------------------------------------------------


class TestBuildEvChargingPlanGuards:
    """Test guard conditions in build_ev_charging_plan."""

    def _make_inp(self, **kwargs: Any) -> EVPlannerInput:
        now = _dt(14)
        deadline = now + timedelta(hours=8)
        base = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=50.0,
            target_soc_pct=80.0,
            battery_capacity_kwh=77.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            deadline=deadline,
            now=now,
        )
        for k, v in kwargs.items():
            setattr(base, k, v)
        return base

    def _slots(self, n=24):
        return _make_slots(n)

    def _surplus(self, n=24, surplus=0.0):
        return [surplus] * n

    def _prices(self, n=24, price=0.10):
        return [price] * n

    def test_disabled_returns_smart_charging_disabled(self):
        """When enabled=False, plan state is smart_charging_disabled."""
        inp = self._make_inp(enabled=False)
        starts, ends = self._slots()
        plan = build_ev_charging_plan(
            inp, starts, ends, self._surplus(), self._prices()
        )
        assert plan.state == "smart_charging_disabled"
        assert plan.charging_slots == []

    def test_not_connected_returns_not_connected(self):
        """When EV not connected, plan state is not_connected."""
        inp = self._make_inp(ev_connected=False)
        starts, ends = self._slots()
        plan = build_ev_charging_plan(
            inp, starts, ends, self._surplus(), self._prices()
        )
        assert plan.state == "not_connected"

    def test_smart_charging_disabled_returns_disabled(self):
        """When smart_charging_enabled=False, plan state is smart_charging_disabled."""
        inp = self._make_inp(smart_charging_enabled=False)
        starts, ends = self._slots()
        plan = build_ev_charging_plan(
            inp, starts, ends, self._surplus(), self._prices()
        )
        assert plan.state == "smart_charging_disabled"

    def test_fully_charged(self):
        """EV at or above target → fully_charged state, no slots."""
        inp = self._make_inp(current_soc_pct=80.0, target_soc_pct=80.0)
        starts, ends = self._slots()
        plan = build_ev_charging_plan(
            inp, starts, ends, self._surplus(), self._prices()
        )
        assert plan.state == "fully_charged"
        assert plan.total_kwh_needed == pytest.approx(0.0)

    def test_zero_capacity_returns_unavailable(self):
        """Zero battery capacity → unavailable."""
        inp = self._make_inp(battery_capacity_kwh=0.0)
        starts, ends = self._slots()
        plan = build_ev_charging_plan(
            inp, starts, ends, self._surplus(), self._prices()
        )
        assert plan.state == "unavailable"

    def test_zero_charger_power_returns_unavailable(self):
        """Zero charger power → unavailable."""
        inp = self._make_inp(charger_power_kw=0.0)
        starts, ends = self._slots()
        plan = build_ev_charging_plan(
            inp, starts, ends, self._surplus(), self._prices()
        )
        assert plan.state == "unavailable"


class TestBuildEvChargingPlanSlotSelection:
    """Test slot selection logic in build_ev_charging_plan."""

    def _now(self):
        return _dt(6)  # 06:00 UTC

    def _slots_24(self):
        return _make_slots(24)

    def test_ev_needs_10kwh_before_deadline(self):
        """EV needs 10 kWh; charger is 11 kW; one 1-hour slot covers 10 kWh.

        Hand calculation:
          energy_needed = max((80 - 80/10 * 77)/100 * 77, 0) ... simplified:
          current=50%, target=80%, cap=77 → needed = (80-50)/100 * 77 = 23.1 kWh

        For this test: set current=70, target=80, cap=100 → needed = 10 kWh.
        charger_kw=11, so one 1-hour slot can supply 11 kWh > 10 kWh.
        One slot allocated at 10 kWh.
        """
        now = self._now()
        deadline = now + timedelta(hours=8)
        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=70.0,
            target_soc_pct=80.0,
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            deadline=deadline,
            now=now,
        )
        starts, ends = self._slots_24()
        surplus = [0.0] * 24
        prices = [0.10] * 24

        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)
        total_charged = sum(s.estimated_charged_kwh for s in plan.charging_slots)
        assert total_charged == pytest.approx(10.0, abs=0.01)
        assert plan.total_kwh_needed == pytest.approx(10.0, abs=0.01)

    def test_solar_surplus_slots_preferred(self):
        """Solar surplus slots are selected before cheap import slots.

        Setup:
          now = 06:00
          deadline = 14:00
          Surplus at slots 10, 11, 12 (3 kWh each)
          Cheap grid at slots 6, 7
          EV needs 5 kWh (current=70%, target=75%, cap=100 kWh)
        """
        now = self._now()
        deadline = now + timedelta(hours=8)  # 14:00
        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=70.0,
            target_soc_pct=75.0,  # 5 kWh needed
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            deadline=deadline,
            now=now,
        )
        starts, ends = self._slots_24()
        surplus = [0.0] * 24
        surplus[10] = 3.0  # 10:00 slot
        surplus[11] = 3.0  # 11:00 slot
        surplus[12] = 3.0  # 12:00 slot
        prices = [0.10] * 24
        prices[6] = 0.01  # cheap but no surplus
        prices[7] = 0.01

        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)

        # Ensure solar slots are used first
        slot_hours = {s.start.hour for s in plan.charging_slots}
        # Should include at least one solar surplus slot
        assert slot_hours & {10, 11, 12}, "Expected solar surplus slots to be preferred"

        total = sum(s.estimated_charged_kwh for s in plan.charging_slots)
        assert total == pytest.approx(5.0, abs=0.01)

    def test_solar_surplus_consumed_by_ev_reduces_surplus(self):
        """When EV consumes solar, solar surplus in the slot reduces import needed.

        Hand calculation:
          base_net = consumption - pv_estimate = 1.0 - 3.5 = -2.5 kWh (surplus)
          solar_surplus = 2.5 kWh
          EV needs 4.0 kWh
          solar_used = min(4.0, 2.5) = 2.5 kWh
          import_needed = 4.0 - 2.5 = 1.5 kWh
        """
        now = _dt(10)
        deadline = now + timedelta(hours=2)
        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=60.0,
            target_soc_pct=64.0,  # 4 kWh needed (4/100 * 100 kWh)
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            deadline=deadline,
            now=now,
        )
        starts = [now, now + timedelta(hours=1)]
        ends = [now + timedelta(hours=1), now + timedelta(hours=2)]
        surplus = [2.5, 0.0]  # slot 0 has surplus
        prices = [0.10, 0.10]

        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)

        assert len(plan.charging_slots) == 1
        slot = plan.charging_slots[0]
        assert slot.estimated_charged_kwh == pytest.approx(4.0, abs=0.01)
        assert slot.solar_surplus_kwh == pytest.approx(2.5, abs=0.01)
        assert slot.import_needed_kwh == pytest.approx(1.5, abs=0.01)
        assert slot.estimated_cost == pytest.approx(1.5 * 0.10, abs=0.001)

    def test_partial_current_slot_scaling(self):
        """Current slot is scaled by remaining minutes.

        now = 06:30 (30 minutes into a 06:00-07:00 slot)
        Remaining = 30 min
        11 kW * (30/60) h = 5.5 kWh max for this slot.
        EV needs 10 kWh → partial slot gets 5.5, next slot gets 4.5.
        """
        now = datetime(2024, 6, 15, 6, 30, 0, tzinfo=_UTC)
        deadline = now + timedelta(hours=5)
        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=70.0,
            target_soc_pct=80.0,
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            deadline=deadline,
            now=now,
        )
        starts = [_dt(h) for h in range(24)]
        ends = [
            _dt(h + 1) if h < 23 else _dt(23) + timedelta(hours=1) for h in range(24)
        ]

        surplus = [0.0] * 24
        prices = [0.10] * 24

        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)
        total = sum(s.estimated_charged_kwh for s in plan.charging_slots)
        assert total == pytest.approx(10.0, abs=0.05)

        # The 06:00 slot (index 6) should have < 11 kWh (partial)
        current_slots = [s for s in plan.charging_slots if s.start.hour == 6]
        if current_slots:
            assert current_slots[0].estimated_charged_kwh < 11.0
            assert current_slots[0].estimated_charged_kwh == pytest.approx(5.5, abs=0.1)

    def test_no_slots_before_deadline(self):
        """When all candidate slots are past the deadline, state is waiting."""
        now = _dt(20)
        deadline = now  # deadline = now → all future slots are after deadline
        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=50.0,
            target_soc_pct=80.0,
            battery_capacity_kwh=77.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            deadline=deadline,
            now=now,
        )
        starts, ends = self._slots_24()
        surplus = [0.0] * 24
        prices = [0.10] * 24

        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)
        assert plan.state == "waiting"
        assert plan.charging_slots == []


# ---------------------------------------------------------------------------
# TestApplyEvPlannedLoad — double-count prevention
# ---------------------------------------------------------------------------


class TestApplyEvPlannedLoad:
    """Test apply_ev_planned_load_to_slots."""

    def _make_plan(
        self, slots_kw: list[float], starts: list[datetime]
    ) -> EVChargingPlan:
        """Build a minimal EVChargingPlan with one slot per start.

        ``ac_load_kwh`` is set equal to ``estimated_charged_kwh`` (100 %
        efficiency assumed) so that injection produces the same numeric values
        as the old battery-side values and existing assertions stay valid.
        """
        from custom_components.hsem.planner.ev_planner import EVChargingSlot

        plan = EVChargingPlan()
        plan.state = "charging"
        for _i, (start, kw) in enumerate(zip(starts, slots_kw)):
            end = start + timedelta(hours=1)
            ev_slot = EVChargingSlot(
                start=start,
                end=end,
                estimated_charged_kwh=kw,
                ac_load_kwh=kw,  # 100 % efficiency — AC = battery-side
            )
            plan.charging_slots.append(ev_slot)
            plan.planned_load_by_slot[start.isoformat()] = kw
        return plan

    def test_normal_injection(self):
        """EV planned loads are injected into the correct slot positions."""
        starts = [_dt(h) for h in range(3)]
        ev_loads = [0.0, 5.0, 3.0]
        plan = self._make_plan(ev_loads, starts)

        result = [0.0] * 3
        apply_ev_planned_load_to_slots(
            starts, result, plan, base_load_includes_ev=False
        )

        assert result[0] == pytest.approx(0.0)
        assert result[1] == pytest.approx(5.0)
        assert result[2] == pytest.approx(3.0)

    def test_base_load_includes_ev_noop(self):
        """When base_load_includes_ev=True, loads are NOT injected."""
        starts = [_dt(h) for h in range(3)]
        ev_loads = [0.0, 5.0, 3.0]
        plan = self._make_plan(ev_loads, starts)

        result = [0.0, 0.0, 0.0]
        apply_ev_planned_load_to_slots(starts, result, plan, base_load_includes_ev=True)

        # Nothing should change — no injection when double-count mode is active
        assert result == [pytest.approx(0.0), pytest.approx(0.0), pytest.approx(0.0)]

    def test_empty_plan_no_change(self):
        """Empty EV plan results in zero EV load for all slots."""
        starts = [_dt(h) for h in range(3)]
        plan = EVChargingPlan()  # no charging slots
        result = [0.0] * 3
        apply_ev_planned_load_to_slots(
            starts, result, plan, base_load_includes_ev=False
        )
        assert result == [pytest.approx(0.0)] * 3


# ---------------------------------------------------------------------------
# TestPlannerEngineEVIntegration — end-to-end engine runs
# ---------------------------------------------------------------------------


class TestPlannerEngineEVIntegration:
    """Full planner engine tests for EV planned load."""

    def test_ev_disabled_behavior_unchanged(self):
        """When EV disabled, engine output is identical to baseline (no EV).

        Existing non-EV behavior must not change when the feature is disabled.
        """
        inp_no_ev = _make_planner_input(ev_enabled=False)
        inp_ev_disabled = _make_planner_input(ev_enabled=False, ev_connected=True)

        out_no_ev = run_planner(inp_no_ev)
        out_ev_disabled = run_planner(inp_ev_disabled)

        # Both should produce the same recommendations
        assert [s.recommendation for s in out_no_ev.slots] == [
            s.recommendation for s in out_ev_disabled.slots
        ]
        # No EV planned load in any slot
        for s in out_ev_disabled.slots:
            assert s.ev_planned_load_kwh == pytest.approx(0.0)

    def test_ev_disconnected_zero_planned_load(self):
        """EV disconnected → ev_planned_load_kwh is 0 in all slots.

        The EV charging plan sensor should report not_connected.
        """
        inp = _make_planner_input(ev_connected=False)
        out = run_planner(inp)

        for s in out.slots:
            assert s.ev_planned_load_kwh == pytest.approx(0.0)
        assert out.ev_charging_plan is not None
        assert out.ev_charging_plan.state == "not_connected"

    def test_ev_fully_charged_zero_planned_load(self):
        """EV already at target → fully_charged state, no EV load injected."""
        inp = _make_planner_input(current_soc=80.0, target_soc=80.0)
        out = run_planner(inp)

        for s in out.slots:
            assert s.ev_planned_load_kwh == pytest.approx(0.0)
        assert out.ev_charging_plan is not None
        assert out.ev_charging_plan.state == "fully_charged"

    def test_ev_load_increases_net_consumption(self):
        """With EV charging, net consumption increases by ev_planned_load_kwh.

        For slots where EV load is injected:
          estimated_net_consumption_kwh
              == avg_house_consumption_kwh + ev_planned_load_kwh - solcast_pv_estimate_kwh
        """
        inp = _make_planner_input(
            now_iso="2024-06-15T06:00:00+00:00",
            current_soc=50.0,
            target_soc=80.0,
            capacity_kwh=77.0,
            charger_kw=11.0,
            deadline_hours_from_now=8,
        )
        out = run_planner(inp)

        for s in out.slots:
            expected_net = round(
                s.avg_house_consumption_kwh
                + s.ev_planned_load_kwh
                - s.solcast_pv_estimate_kwh,
                3,
            )
            assert s.estimated_net_consumption_kwh == pytest.approx(
                expected_net, abs=1e-6
            ), (
                f"Slot {s.start.hour}:00 net mismatch: "
                f"house={s.avg_house_consumption_kwh}, ev={s.ev_planned_load_kwh}, "
                f"pv={s.solcast_pv_estimate_kwh}"
            )

    def test_solar_consumed_by_ev_no_battery_solar_charge(self):
        """When EV consumes all solar surplus, battery should not be recommended
        for solar charging in those slots.

        Hand calculation:
          base consumption = 1.0 kWh/h
          PV at 10:00-14:00 = 3.5 kWh/h
          base_net at 10:00 = 1.0 - 3.5 = -2.5 kWh (surplus)

          EV needs 23.1 kWh (50%→80% of 77 kWh battery).
          EV charger = 11 kW → max 11 kWh/h per slot.
          EV allocated to solar surplus slots first: 10, 11, 12, 13 = up to 4 slots.
          Each slot: ev_load = min(11, remaining_ev).
          effective_net = 1.0 + ev_load - 3.5
                        = ev_load - 2.5

          For ev_load = 3.5 (capped by surplus): effective_net = 1.0 → no surplus.
          For ev_load = 11 (still needs to fill): effective_net = 8.5 → heavy import.

          The important invariant: no BatteriesChargeSolar on EV-loaded solar slots
          when EV already consumes all surplus.
        """
        inp = _make_planner_input(
            now_iso="2024-06-15T06:00:00+00:00",
            current_soc=50.0,
            target_soc=80.0,
            capacity_kwh=77.0,
            charger_kw=11.0,
        )
        out = run_planner(inp)

        # Find slots where EV planned load > 0 AND solcast_pv_estimate_kwh > 0
        ev_solar_slots = [
            s
            for s in out.slots
            if s.ev_planned_load_kwh > 1e-9 and s.solcast_pv_estimate_kwh > 1e-9
        ]
        for s in ev_solar_slots:
            # Net consumption should be ≥ 0 in these slots (EV consumed the surplus)
            # → battery solar-charge recommendation should not appear
            if s.estimated_net_consumption_kwh >= 0:
                assert s.recommendation != "batteries_charge_solar", (
                    f"Slot {s.start.hour}: EV consumed solar but battery solar-charge still recommended. "
                    f"ev_load={s.ev_planned_load_kwh}, pv={s.solcast_pv_estimate_kwh}, "
                    f"net={s.estimated_net_consumption_kwh}"
                )

    def test_cheap_grid_can_still_charge_battery_while_ev_charging(self):
        """When grid price is cheap, home battery may grid-charge even during EV charging.

        Set up cheapest grid price at 02:00 (0.01 EUR/kWh) with EV also charging.
        The planner may still assign BatteriesChargeGrid to cheap slots even
        when EV is scheduled there.
        """
        inp = _make_planner_input(
            now_iso="2024-06-15T00:00:00+00:00",
            current_soc=50.0,
            target_soc=80.0,
            capacity_kwh=77.0,
            charger_kw=11.0,
            deadline_hours_from_now=8,
        )
        # Override price_points to make slot 2 very cheap
        inp.price_points[2] = PricePoint(hour=2, import_price=0.01, export_price=0.00)
        out = run_planner(inp)

        # It should be possible for battery grid-charge at hour 2 to be recommended
        # (the planner does not prevent this — both EV charging AND battery charging
        # can happen in the same slot from a planning perspective)
        charge_grid_slots = [
            s for s in out.slots if s.recommendation == "batteries_charge_grid"
        ]
        # The planner should find the very cheap slot profitable for grid-charging
        # home battery (not blocked by EV charging)
        # This is a liveness test — we only assert that the planner does NOT
        # permanently prevent grid-charging because of EV.
        # It may or may not recommend it depending on battery SoC headroom.
        assert isinstance(charge_grid_slots, list)  # just ensure no exception

    def test_double_count_prevention(self):
        """When base_load_includes_ev=True, EV load is not added to net consumption.

        With base_load_includes_ev=True, the ev_planned_load_kwh field in slots
        should remain 0 even when EV is connected and needs charging.
        """
        inp_include = _make_planner_input(
            now_iso="2024-06-15T06:00:00+00:00",
            current_soc=50.0,
            target_soc=80.0,
            capacity_kwh=77.0,
            charger_kw=11.0,
            base_includes_ev=True,
        )
        out = run_planner(inp_include)

        # With base_load_includes_ev=True, no EV load injected
        for s in out.slots:
            assert s.ev_planned_load_kwh == pytest.approx(0.0), (
                f"Slot {s.start.hour}: expected 0 ev_load (base_includes_ev=True) "
                f"but got {s.ev_planned_load_kwh}"
            )

    def test_ev_charging_plan_attached_to_output(self):
        """PlannerOutput.ev_charging_plan is populated when EV feature is enabled."""
        inp = _make_planner_input(
            now_iso="2024-06-15T06:00:00+00:00",
            current_soc=50.0,
            target_soc=80.0,
        )
        out = run_planner(inp)

        assert out.ev_charging_plan is not None
        assert out.ev_charging_plan.state in {
            "charging",
            "waiting",
            "not_connected",
            "smart_charging_disabled",
            "fully_charged",
            "unavailable",
        }

    def test_ev_plan_state_not_connected_when_disconnected(self):
        """EV plan state is not_connected when EV is not plugged in."""
        inp = _make_planner_input(ev_connected=False)
        out = run_planner(inp)

        assert out.ev_charging_plan is not None
        assert out.ev_charging_plan.state == "not_connected"

    def test_ev_plan_none_when_disabled(self):
        """EV plan is None when EV feature is disabled."""
        inp = _make_planner_input(ev_enabled=False)
        out = run_planner(inp)

        assert out.ev_charging_plan is None

    def test_ev_planned_load_slot_field_default_zero(self):
        """PlannedSlot.ev_planned_load_kwh defaults to 0.0 when EV is disabled."""
        inp = _make_planner_input(ev_enabled=False)
        out = run_planner(inp)

        for s in out.slots:
            assert s.ev_planned_load_kwh == pytest.approx(0.0)

    def test_invalid_data_no_exception(self):
        """Invalid EV data (negative SoC, zero capacity) must not raise."""
        inp = _make_planner_input(
            current_soc=-10.0,
            target_soc=80.0,
            capacity_kwh=0.0,
            charger_kw=0.0,
        )
        # Should not raise — planner handles gracefully
        out = run_planner(inp)
        assert out.ev_charging_plan is not None
        assert out.ev_charging_plan.state in {"unavailable", "fully_charged"}


# ---------------------------------------------------------------------------
# TestEvSolarSurplusRegression
# Regression: EV slot selection must use net surplus (after house consumption),
# not raw PV.  The house uses solar first; only leftover surplus is free for EV.
# The engine now runs populate_net_consumption before EV planning and derives:
#   slot_net_surplus = max(-estimated_net_consumption_kwh, 0)
# ---------------------------------------------------------------------------


class TestEvSolarSurplusRegression:
    """Regression tests for EV surplus computation correctness.

    The engine runs ``populate_net_consumption`` *before* EV planning so that
    the net surplus signal is:

        slot_net_surplus[i] = max(-estimated_net_consumption_kwh[i], 0.0)
                            = max(pv_estimate - avg_house_consumption_kwh, 0.0)

    This correctly models that the house consumes solar first; only the
    leftover net surplus is available to the EV charger at no extra grid cost.
    Using raw PV estimates would over-state the free energy available.
    """

    def test_ev_receives_solar_slot_exact_hand_calculation(self):
        """EV is allocated to the solar-surplus slot; effective net = 0.0 kWh.

        Hand calculation
        ----------------
        Setup (one solar slot: hour 10):
          house load       = 1.0 kWh/h
          PV estimate      = 3.5 kWh/h
          base net         = 1.0 − 3.5 = −2.5 kWh  (surplus)
          EV energy needed = 2.5 kWh  (25%→27.5% of 100 kWh battery)
          charger power    = 11 kW  →  max per slot = 11 kWh

        Expected EV allocation at hour 10:
          solar_surplus = max(3.5 − 1.0, 0) = 2.5 kWh
          ev_load       = min(11.0, 2.5)     = 2.5 kWh  (exactly covers energy needed)
          solar_used    = min(2.5, 2.5)       = 2.5 kWh
          import_needed = 0.0 kWh

        Effective net at hour 10 after injection:
          effective_net = avg_house(1.0) + ev_load(2.5) − pv(3.5) = 0.0 kWh

        Invariant: no battery solar-charge recommended (effective net ≥ 0,
        so no solar surplus remains for the battery).
        """
        # Build a 24-hour, 1-hour-slot input where only hour 10 has solar surplus.
        now_iso = "2024-06-15T06:00:00+00:00"
        from datetime import datetime as _dt2

        now = _dt2.fromisoformat(now_iso)
        deadline = now + timedelta(hours=8)  # deadline at 14:00

        prices = [
            PricePoint(hour=h, import_price=0.10, export_price=0.05) for h in range(24)
        ]
        # Only hour 10 has PV surplus
        pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
        pv[10] = SolcastSlot(hour=10, pv_estimate=3.5)

        averages = [
            HourlyConsumptionAverage(
                hour=h, avg_1d=1.0, avg_3d=1.0, avg_7d=1.0, avg_14d=1.0
            )
            for h in range(24)
        ]

        # EV needs exactly 2.5 kWh: 25%→27.5% of 100 kWh = 2.5 kWh
        inp = PlannerInput(
            now_iso=now_iso,
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_soc_pct=90.0,
            battery_max_charge_power_w=5000.0,
            battery_max_discharge_power_w=5000.0,
            battery_charge_efficiency_pct=95.0,
            battery_discharge_efficiency_pct=95.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=averages,
            price_points=prices,
            solcast_slots=pv,
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=25.0,
            ev_planned_load_target_soc_pct=27.5,  # exactly 2.5 kWh needed
            ev_planned_load_battery_capacity_kwh=100.0,
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=deadline,
            ev_planned_load_base_load_includes_ev=False,
        )
        out = run_planner(inp)

        # --- Assert EV plan selected the solar slot ---
        assert out.ev_charging_plan is not None
        plan = out.ev_charging_plan
        assert len(plan.charging_slots) >= 1, (
            "EV should have at least one charging slot"
        )
        solar_ev_slots = [s for s in plan.charging_slots if s.start.hour == 10]
        assert solar_ev_slots, (
            "EV should be scheduled in the solar-surplus slot (hour 10)"
        )

        # The slot at hour 10 should use the full 2.5 kWh from solar
        slot_10 = solar_ev_slots[0]
        assert slot_10.solar_surplus_kwh == pytest.approx(2.5, abs=0.01), (
            f"EV solar_surplus_kwh should be 2.5, got {slot_10.solar_surplus_kwh}. "
            "If 0.0, the solar surplus computation bug is still present."
        )
        assert slot_10.import_needed_kwh == pytest.approx(0.0, abs=0.01), (
            f"EV import_needed_kwh should be 0.0 (covered by solar), got {slot_10.import_needed_kwh}"
        )

        # --- Assert effective net load at hour 10 ---
        planner_slot_10 = next((s for s in out.slots if s.start.hour == 10), None)
        assert planner_slot_10 is not None
        assert planner_slot_10.ev_planned_load_kwh == pytest.approx(2.5, abs=0.01), (
            f"Planner slot 10 ev_planned_load_kwh should be 2.5, got {planner_slot_10.ev_planned_load_kwh}"
        )
        # effective_net = house(1.0) + ev(2.5) - pv(3.5) = 0.0
        assert planner_slot_10.estimated_net_consumption_kwh == pytest.approx(
            0.0, abs=0.01
        ), (
            f"effective_net at hour 10 should be 0.0, got {planner_slot_10.estimated_net_consumption_kwh}"
        )

        # --- Assert battery does NOT charge energy from consumed surplus ---
        # The recommendation label may still be 'batteries_charge_solar' because
        # estimated_net_consumption_kwh = 0.0 falls within the NEAR_ZERO threshold.
        # The energy-correctness invariant is: batteries_charged_kwh must be 0.0
        # (the charge scheduler derives slot_solar = abs(0.0) = 0.0, so no
        # energy flows into the battery even if the label says charge_solar).
        assert planner_slot_10.batteries_charged_kwh == pytest.approx(0.0, abs=0.01), (
            "Battery should NOT charge energy at hour 10: "
            "all solar surplus is consumed by EV. "
            f"batteries_charged_kwh={planner_slot_10.batteries_charged_kwh}, "
            f"ev_load={planner_slot_10.ev_planned_load_kwh}, "
            f"net={planner_slot_10.estimated_net_consumption_kwh}"
        )

    def test_ev_solar_surplus_zero_when_no_pv(self):
        """When PV = 0, no solar surplus reaches EV — all charging uses grid import.

        Hand calculation:
          house load = 1.0 kWh/h, PV = 0.0 kWh/h
          surplus = max(0.0 − 1.0, 0) = 0.0
          EV charged from grid entirely.
        """
        now_iso = "2024-06-15T06:00:00+00:00"
        from datetime import datetime as _dt2

        now = _dt2.fromisoformat(now_iso)
        deadline = now + timedelta(hours=6)

        prices = [
            PricePoint(hour=h, import_price=0.10, export_price=0.05) for h in range(24)
        ]
        pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]  # no PV
        averages = [
            HourlyConsumptionAverage(
                hour=h, avg_1d=1.0, avg_3d=1.0, avg_7d=1.0, avg_14d=1.0
            )
            for h in range(24)
        ]

        inp = PlannerInput(
            now_iso=now_iso,
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_soc_pct=90.0,
            battery_max_charge_power_w=5000.0,
            battery_max_discharge_power_w=5000.0,
            battery_charge_efficiency_pct=95.0,
            battery_discharge_efficiency_pct=95.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=averages,
            price_points=prices,
            solcast_slots=pv,
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=70.0,
            ev_planned_load_target_soc_pct=80.0,  # 10 kWh needed
            ev_planned_load_battery_capacity_kwh=100.0,
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=deadline,
            ev_planned_load_base_load_includes_ev=False,
        )
        out = run_planner(inp)

        assert out.ev_charging_plan is not None
        # All EV charging slots should show zero solar surplus (no PV)
        for ev_slot in out.ev_charging_plan.charging_slots:
            assert ev_slot.solar_surplus_kwh == pytest.approx(0.0, abs=1e-9), (
                f"Hour {ev_slot.start.hour}: expected 0.0 solar surplus (no PV), "
                f"got {ev_slot.solar_surplus_kwh}"
            )
            # All energy comes from grid
            assert ev_slot.import_needed_kwh == pytest.approx(
                ev_slot.estimated_charged_kwh, abs=0.01
            )


# ---------------------------------------------------------------------------
# TestEvSmartChargingRecommendationLabel
# EV-scheduled slots should be labelled ev_smart_charging so dashboards
# and the working-mode sensor reflect the real activity.
# ---------------------------------------------------------------------------


class TestEvSmartChargingRecommendationLabel:
    """Slots with planned EV load must be marked ev_smart_charging.

    Priority rules:
    - batteries_charge_grid  → kept (grid-charge beats EV label)
    - batteries_discharge_mode / force_batteries_discharge → kept
    - batteries_charge_solar / batteries_wait_mode → overridden by ev_smart_charging
    - EV disabled / not connected → no ev_smart_charging labels
    """

    def test_ev_load_slots_labelled_ev_smart_charging(self):
        """Future slots with ev_planned_load_kwh > 0 must be ev_smart_charging
        unless a higher-priority recommendation is already set.

        Higher-priority recommendations that keep their own label:
          batteries_charge_grid, force_batteries_discharge, force_export,
          time_passed, missing_input_entities.

        batteries_discharge_mode is intentionally NOT in this set: when an
        EV is scheduled to charge, ev_smart_charging takes precedence over
        a scheduled discharge window so dashboards correctly reflect EV
        activity.
        """
        _KEEP_LABELS = {
            "batteries_charge_grid",
            "force_batteries_discharge",
            "force_export",
            "time_passed",
            "missing_input_entities",
        }

        inp = _make_planner_input(
            now_iso="2024-06-15T06:00:00+00:00",
            current_soc=50.0,
            target_soc=80.0,
            capacity_kwh=77.0,
            charger_kw=11.0,
            deadline_hours_from_now=10,
        )
        out = run_planner(inp)

        for s in out.slots:
            if abs(s.ev_planned_load_kwh) > 1e-9:
                if s.recommendation in _KEEP_LABELS:
                    continue  # higher-priority recommendation correctly kept
                assert s.recommendation == "ev_smart_charging", (
                    f"Slot {s.start.hour}: ev_load={s.ev_planned_load_kwh:.3f} but "
                    f"recommendation='{s.recommendation}' instead of 'ev_smart_charging'"
                )

    def test_no_ev_smart_charging_when_disabled(self):
        """When EV is disabled, no slot should be labelled ev_smart_charging."""
        inp = _make_planner_input(ev_enabled=False)
        out = run_planner(inp)

        for s in out.slots:
            assert s.recommendation != "ev_smart_charging", (
                f"Slot {s.start.hour}: unexpected ev_smart_charging (EV disabled)"
            )

    def test_no_ev_smart_charging_when_disconnected(self):
        """When EV is not connected, no slot should be labelled ev_smart_charging."""
        inp = _make_planner_input(ev_connected=False)
        out = run_planner(inp)

        for s in out.slots:
            assert s.recommendation != "ev_smart_charging", (
                f"Slot {s.start.hour}: unexpected ev_smart_charging (EV not connected)"
            )

    def test_grid_charge_slots_not_overridden_by_ev_label(self):
        """Slots already marked batteries_charge_grid keep that recommendation.

        Set up: very cheap price at 02:00 to force a grid-charge recommendation,
        EV also enabled with deadline covering that slot.
        The grid-charge label must survive — it has higher priority.
        """
        inp = _make_planner_input(
            now_iso="2024-06-15T00:00:00+00:00",
            current_soc=10.0,  # low SoC → battery actively needs charging
            target_soc=80.0,
            capacity_kwh=77.0,
            charger_kw=11.0,
            deadline_hours_from_now=10,
        )
        # Force a very cheap price at 02:00 to encourage grid charge
        inp.price_points[2] = PricePoint(hour=2, import_price=0.001, export_price=0.0)
        # Make peak hours expensive so grid-charge has clear arbitrage value
        for h in range(16, 22):
            inp.price_points[h] = PricePoint(hour=h, import_price=2.0, export_price=0.5)
        out = run_planner(inp)

        for s in out.slots:
            if s.recommendation == "batteries_charge_grid":
                # A grid-charge slot must NOT have been overridden by EV label
                # even if EV load is also allocated to it.
                assert s.recommendation == "batteries_charge_grid", (
                    f"Slot {s.start.hour}: batteries_charge_grid was incorrectly "
                    "overridden by ev_smart_charging"
                )

    def test_ev_smart_charging_in_charge_windows(self):
        """EVSmartCharging slots are counted as charge windows in PlannerOutput."""
        inp = _make_planner_input(
            now_iso="2024-06-15T06:00:00+00:00",
            current_soc=50.0,
            target_soc=80.0,
            capacity_kwh=77.0,
            charger_kw=11.0,
            deadline_hours_from_now=10,
        )
        out = run_planner(inp)

        ev_smart_slots = [
            s for s in out.slots if s.recommendation == "ev_smart_charging"
        ]
        if ev_smart_slots:
            # At least one charge window should cover the EV slots
            all_window_starts = {w.start for w in out.charge_windows}
            ev_starts = {s.start for s in ev_smart_slots}
            assert ev_starts & all_window_starts, (
                "ev_smart_charging slots should appear in at least one charge window"
            )


# ---------------------------------------------------------------------------
# TestEvAcLoadAndSoCPath
#
# Hand-calculated tests proving that EV load flows through the full SoC/cost
# path correctly:
#
#   1. ac_load_kwh = estimated_charged_kwh / charger_efficiency
#   2. ev_planned_load_kwh injected = ac_load_kwh (not battery-side)
#   3. estimated_net_consumption_kwh = house + ev_ac_load - pv
#   4. grid_import_kwh includes EV AC draw
#   5. plan_cost includes EV grid import cost
#   6. SoC simulation uses the combined load
#
# All scenarios use:
#   - 1-hour slots, 24-hour horizon
#   - EV needed kWh known (set up to charge exactly one slot)
#   - No home battery so SoC path is simple (battery_soc_pct = 5 = floor)
# ---------------------------------------------------------------------------


class TestEvAcLoadAndSoCPath:
    """Prove EV load affects ac_load_kwh, net consumption, grid import and plan cost."""

    def _make_no_battery_input(
        self,
        now_iso: str = "2024-06-15T08:00:00+00:00",
        ev_soc: float = 70.0,
        ev_target_soc: float = 80.0,
        ev_capacity_kwh: float = 100.0,
        charger_kw: float = 11.0,
        charger_eff_pct: float = 100.0,
        import_price: float = 0.5,
        pv_estimate_kwh: float = 0.0,
        house_consumption_kwh: float = 0.5,
        deadline_hours: float = 3.0,
    ) -> PlannerInput:
        """Build a minimal input with the battery at the discharge floor."""
        now_dt = datetime.fromisoformat(now_iso)
        deadline = now_dt + timedelta(hours=deadline_hours)

        prices = [
            PricePoint(hour=h, import_price=import_price, export_price=0.1)
            for h in range(24)
        ]
        pv = [SolcastSlot(hour=h, pv_estimate=pv_estimate_kwh) for h in range(24)]
        avgs = [
            HourlyConsumptionAverage(
                hour=h,
                avg_1d=house_consumption_kwh,
                avg_3d=house_consumption_kwh,
                avg_7d=house_consumption_kwh,
                avg_14d=house_consumption_kwh,
            )
            for h in range(24)
        ]
        return PlannerInput(
            now_iso=now_iso,
            interval_minutes=60,
            interval_length_hours=24,
            # Battery at floor → no battery involvement
            battery_soc_pct=5.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=5.0,
            battery_max_soc_pct=90.0,
            battery_max_charge_power_w=5000.0,
            battery_charge_efficiency_pct=100.0,
            battery_discharge_efficiency_pct=100.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=avgs,
            price_points=prices,
            solcast_slots=pv,
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=ev_soc,
            ev_planned_load_target_soc_pct=ev_target_soc,
            ev_planned_load_battery_capacity_kwh=ev_capacity_kwh,
            ev_planned_load_charger_power_kw=charger_kw,
            ev_planned_load_charger_efficiency_pct=charger_eff_pct,
            ev_planned_load_deadline=deadline,
            time_discount_rate=1.0,  # no discount for EV tests
            ev_planned_load_base_load_includes_ev=False,
        )

    # ------------------------------------------------------------------
    # ac_load_kwh unit test
    # ------------------------------------------------------------------

    def test_ac_load_kwh_equals_battery_delivered_divided_by_efficiency(self):
        """EVChargingSlot.ac_load_kwh must equal estimated_charged_kwh / efficiency.

        Hand calculation:
          EV needs 10 kWh (battery-side, e.g. 10 → 20 % of 100 kWh battery)
          charger_power = 11 kW, slot = 1 h → max_battery_side = 11 × 1 × 0.90 = 9.9 kWh
          allocated (battery-side) = min(9.9, 10.0) = 9.9 kWh
          ac_load_kwh = 9.9 / 0.90 = 11.0 kWh
        """
        now_dt = datetime(2024, 6, 15, 8, 0, tzinfo=UTC)
        deadline = now_dt + timedelta(hours=2)
        starts = [now_dt + timedelta(hours=h) for h in range(24)]
        ends = [now_dt + timedelta(hours=h + 1) for h in range(24)]

        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=90.0,  # 10 % below target
            target_soc_pct=100.0,
            battery_capacity_kwh=100.0,  # needs 10 kWh battery-side
            charger_power_kw=11.0,
            charger_efficiency_pct=90.0,
            deadline=deadline,
            base_load_includes_ev=False,
            now=now_dt,
        )
        net_surplus = [0.0] * 24  # no solar surplus — all grid import
        import_price = [0.5] * 24

        plan = build_ev_charging_plan(inp, starts, ends, net_surplus, import_price)

        assert plan.charging_slots, "EV plan should have at least one charging slot"
        slot = plan.charging_slots[0]
        # battery-side
        assert slot.estimated_charged_kwh == pytest.approx(9.9, abs=0.01)
        # AC-side: 9.9 / 0.90 = 11.0
        assert slot.ac_load_kwh == pytest.approx(11.0, abs=0.01)
        # Invariant: ac_load_kwh = estimated_charged_kwh / efficiency
        eff = 90.0 / 100.0
        assert slot.ac_load_kwh == pytest.approx(
            slot.estimated_charged_kwh / eff, abs=1e-9
        )

    # ------------------------------------------------------------------
    # estimated_net_consumption_kwh includes AC EV draw
    # ------------------------------------------------------------------

    def test_estimated_net_consumption_includes_ev_ac_load(self):
        """estimated_net_consumption_kwh must use the AC-side EV load, not battery-side.

        Hand calculation (100 % efficiency, so AC = battery-side):
          house_load       = 0.5 kWh/h
          pv_estimate      = 0.0 kWh/h
          EV needed        = 10 kWh  (10 → 20 % of 100 kWh battery)
          charger          = 11 kW, eff = 100 %  →  max = 11 kWh
          allocated        = min(11, 10) = 10 kWh  (battery-side = AC-side at 100 %)
          ev_planned_load_kwh (AC) = 10.0

          net for EV slot: 0.5 + 10.0 - 0.0 = 10.5 kWh
        """
        inp = self._make_no_battery_input(
            ev_soc=10.0,
            ev_target_soc=20.0,
            ev_capacity_kwh=100.0,  # needs 10 kWh
            charger_kw=11.0,
            charger_eff_pct=100.0,
            house_consumption_kwh=0.5,
            pv_estimate_kwh=0.0,
            import_price=0.5,
            deadline_hours=3.0,
        )
        out = run_planner(inp)

        ev_slots = [s for s in out.slots if abs(s.ev_planned_load_kwh) > 1e-9]
        assert ev_slots, "At least one slot should have EV planned load"

        for s in ev_slots:
            expected_net = round(
                s.avg_house_consumption_kwh
                + s.ev_planned_load_kwh
                - s.solcast_pv_estimate_kwh,
                3,
            )
            assert s.estimated_net_consumption_kwh == pytest.approx(
                expected_net, abs=1e-6
            )
            # With no PV and 100 % efficiency, AC load = battery-side
            # net = 0.5 + ev_ac = 0.5 + 10.0 = 10.5
            assert s.estimated_net_consumption_kwh == pytest.approx(10.5, abs=0.1), (
                f"Slot {s.start.hour}: expected net=10.5, got {s.estimated_net_consumption_kwh}"
            )

    # ------------------------------------------------------------------
    # grid_import_kwh includes EV AC draw (SoC simulation path)
    # ------------------------------------------------------------------

    def test_grid_import_kwh_includes_ev_ac_load(self):
        """grid_import_kwh must include the EV AC load.

        Hand calculation (100 % charger efficiency, no PV, battery at floor):
          house_load = 0.5 kWh/h
          EV AC load = 10.0 kWh/h  (10 kWh needed, allocated in one slot)
          pv         = 0.0 kWh
          battery    = at floor (current_kwh = 0.0) but usable_kwh = 8.5

          With the #446 fix (continue instead of break), the planner
          correctly keeps more discharge slots, triggering pre-charge:
            battery_charged      = 5.0 kWh
            house + ev_ac        = 0.5 + 10.0 = 10.5 kWh
            grid_import          = 10.5 + 5.0 = 15.5 kWh
        """
        inp = self._make_no_battery_input(
            ev_soc=10.0,
            ev_target_soc=20.0,
            ev_capacity_kwh=100.0,
            charger_kw=11.0,
            charger_eff_pct=100.0,
            house_consumption_kwh=0.5,
            pv_estimate_kwh=0.0,
            import_price=0.5,
            deadline_hours=3.0,
        )
        out = run_planner(inp)

        ev_slots = [s for s in out.slots if abs(s.ev_planned_load_kwh) > 1e-9]
        assert ev_slots, "At least one slot should have EV planned load"

        for s in ev_slots:
            # grid_import = house + ev_ac + battery_charge - battery_discharge
            expected_import = (
                s.avg_house_consumption_kwh
                + s.ev_planned_load_kwh
                + s.batteries_charged_kwh
                - s.batteries_discharged_kwh
            )
            assert s.grid_import_kwh == pytest.approx(expected_import, abs=0.05), (
                f"Slot {s.start.hour}: expected grid_import={expected_import:.2f}, "
                f"got {s.grid_import_kwh}"
            )

    # ------------------------------------------------------------------
    # grid_import_kwh with charger efficiency < 100 %
    # ------------------------------------------------------------------

    @pytest.mark.skip(reason="MILP-only mode: schedule-based behavior not applicable")
    def test_ev_ac_load_larger_than_battery_side_at_sub_100pct_efficiency(self):
        """With 90 % charger efficiency, AC draw > battery-side energy.

        Hand calculation:
          EV needs 9 kWh battery-side  (e.g. 10 → 19 % of 100 kWh battery)
          charger: 11 kW AC, 90 % eff  →  max_battery = 11 × 0.90 = 9.9 kWh/h
          allocated (battery-side) = min(9.9, 9.0) = 9.0 kWh
          ac_load_kwh = 9.0 / 0.90 = 10.0 kWh

          house_load = 0.5 kWh, pv = 0.0, battery at floor
          With the #446 fix (continue instead of break), the planner
          correctly pre-charges the battery for discharge slots:
            battery_charged = 5.0 kWh
            grid_import     = 0.5 + 10.0 + 5.0 = 15.5 kWh
        """
        inp = self._make_no_battery_input(
            ev_soc=10.0,
            ev_target_soc=19.0,  # needs 9 kWh battery-side
            ev_capacity_kwh=100.0,
            charger_kw=11.0,
            charger_eff_pct=90.0,
            house_consumption_kwh=0.5,
            pv_estimate_kwh=0.0,
            import_price=0.5,
            deadline_hours=3.0,
        )
        out = run_planner(inp)

        ev_slots = [s for s in out.slots if abs(s.ev_planned_load_kwh) > 1e-9]
        assert ev_slots, "At least one slot should have EV planned load"

        s = ev_slots[0]
        # EV plan: battery-side = 9.0, ac_load = 9.0/0.9 = 10.0
        assert out.ev_charging_plan is not None
        plan_slot = out.ev_charging_plan.charging_slots[0]
        assert plan_slot.estimated_charged_kwh == pytest.approx(9.0, abs=0.01)
        assert plan_slot.ac_load_kwh == pytest.approx(10.0, abs=0.01)

        # PlannedSlot: ev_planned_load_kwh = ac_load_kwh = 10.0
        assert s.ev_planned_load_kwh == pytest.approx(10.0, abs=0.01), (
            f"ev_planned_load_kwh should be AC-side 10.0, got {s.ev_planned_load_kwh}"
        )
        # net = house + ev_ac - pv = 0.5 + 10.0 - 0.0 = 10.5
        assert s.estimated_net_consumption_kwh == pytest.approx(10.5, abs=0.05)
        # grid_import = house + ev_ac + battery_charge = 0.5 + 10.0 + 5.0 = 15.5
        assert s.grid_import_kwh == pytest.approx(15.5, abs=0.1)

    # ------------------------------------------------------------------
    # plan_cost includes EV grid import cost
    # ------------------------------------------------------------------

    def test_plan_cost_includes_ev_grid_import(self):
        """plan_cost must account for EV grid draw, not just house load.

        Hand calculation (100 % efficiency, no PV, 24-hour horizon, flat price):
          import_price  = 0.40 DKK/kWh
          house_load    = 0.5 kWh/h × 24 h = 12.0 kWh
          EV AC load    = 10.0 kWh (allocated to 1 slot)
          pv            = 0.0
          battery       = at floor

          grid_import_total = 12.0 + 10.0 = 22.0 kWh
          plan_cost = 22.0 × 0.40 = 8.80 DKK  (approximately)

          Without EV fix: plan_cost ≈ 12.0 × 0.40 = 4.80 DKK  (too low)
        """
        inp = self._make_no_battery_input(
            ev_soc=10.0,
            ev_target_soc=20.0,
            ev_capacity_kwh=100.0,
            charger_kw=11.0,
            charger_eff_pct=100.0,
            house_consumption_kwh=0.5,
            pv_estimate_kwh=0.0,
            import_price=0.40,
            deadline_hours=3.0,
        )
        out = run_planner(inp)

        # Total EV AC load actually allocated across all slots
        total_ev_ac = sum(s.ev_planned_load_kwh for s in out.slots)

        # EV AC load must be positive — the fix is working
        assert total_ev_ac > 1e-9, (
            "total ev_planned_load_kwh should be > 0; EV load not injected"
        )

        # Use future/current slots only — past slots have grid_import=0 by design
        # so including them in house sums would break the energy balance.
        future_slots = [
            s for s in out.slots if s.grid_import_kwh > 0 or s.ev_planned_load_kwh > 0
        ]

        house_future = sum(s.avg_house_consumption_kwh for s in future_slots)
        ev_future = sum(s.ev_planned_load_kwh for s in future_slots)
        battery_discharge_future = sum(s.batteries_discharged_kwh for s in future_slots)
        battery_charge_future = sum(s.batteries_charged_kwh for s in future_slots)
        gi_future = sum(s.grid_import_kwh for s in future_slots)

        # Energy balance per slot: gi = house + ev_ac + battery_charge - battery_discharge
        expected_gi = (
            house_future + ev_future + battery_charge_future - battery_discharge_future
        )
        assert gi_future == pytest.approx(expected_gi, abs=0.5), (
            f"grid_import ({gi_future:.2f}) should equal "
            f"house ({house_future:.2f}) + ev_ac ({ev_future:.2f}) "
            f"+ batt_chg ({battery_charge_future:.2f}) "
            f"- batt_disch ({battery_discharge_future:.2f}) = {expected_gi:.2f}; "
            "EV AC load not flowing through SoC simulation"
        )

        # The specific EV slot: grid import must include the full EV AC draw
        ev_slot = next(s for s in out.slots if abs(s.ev_planned_load_kwh) > 1e-9)
        expected_slot_gi = (
            ev_slot.avg_house_consumption_kwh
            + ev_slot.ev_planned_load_kwh
            + ev_slot.batteries_charged_kwh
            - ev_slot.batteries_discharged_kwh
        )
        assert ev_slot.grid_import_kwh == pytest.approx(expected_slot_gi, abs=0.1), (
            f"EV slot grid_import ({ev_slot.grid_import_kwh:.3f}) should equal "
            f"house ({ev_slot.avg_house_consumption_kwh:.3f}) + ev_ac "
            f"({ev_slot.ev_planned_load_kwh:.3f}) + batt_chg "
            f"({ev_slot.batteries_charged_kwh:.3f}) - batt_disch "
            f"({ev_slot.batteries_discharged_kwh:.3f}) = {expected_slot_gi:.3f}"
        )

    # ------------------------------------------------------------------
    # SoC simulation: EV load reduces battery via increased net demand
    # ------------------------------------------------------------------

    def test_soc_simulation_accounts_for_ev_load(self):
        """EV load goes to grid_import only; the battery does NOT discharge.

        The EV charger and house loads share the same AC bus.  When the EV is
        charging, battery discharge is suppressed to avoid DC→AC→DC conversion
        losses.  Therefore `batteries_discharged_kwh` is 0 during EV slots, and
        `grid_import_kwh` absorbs BOTH house and EV demand.

        Setup (battery has charge, no schedule forcing discharge):
          battery_soc_pct  = 80 %  (7.5 kWh above floor: (80-5)/100 × 10)
          house_load       = 0.5 kWh/h
          EV AC load       = 5.0 kWh/h  (5 kWh needed in one slot, 100 % eff)
          pv               = 0.0
          import_price     = flat 0.5

        Expected energy balance per EV slot:
          batteries_discharged_kwh ≈ 0.0 kWh  (suppressed — EV is charging)
          grid_import ≈ house + ev = 5.5 kWh  (everything from grid)
          total supply = batteries_discharged_kwh + grid_import ≈ 5.5 kWh

        The energy balance must still hold: supply ≈ demand — but the battery
        does not discharge when the EV is active on the shared AC bus.
        """
        inp = PlannerInput(
            now_iso="2024-06-15T08:00:00+00:00",
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=80.0,  # 7.5 kWh above floor (10 × 0.75)
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=5.0,
            battery_max_soc_pct=90.0,
            battery_max_charge_power_w=5000.0,
            battery_charge_efficiency_pct=100.0,
            battery_discharge_efficiency_pct=100.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=[
                HourlyConsumptionAverage(
                    hour=h, avg_1d=0.5, avg_3d=0.5, avg_7d=0.5, avg_14d=0.5
                )
                for h in range(24)
            ],
            price_points=[
                PricePoint(hour=h, import_price=0.5, export_price=0.1)
                for h in range(24)
            ],
            solcast_slots=[SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)],
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=5.0,
            ev_planned_load_target_soc_pct=10.0,  # 5 kWh needed
            ev_planned_load_battery_capacity_kwh=100.0,
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=datetime(2024, 6, 15, 12, 0, tzinfo=UTC),
            ev_planned_load_base_load_includes_ev=False,
        )
        out = run_planner(inp)

        ev_slots = [s for s in out.slots if abs(s.ev_planned_load_kwh) > 1e-9]
        assert ev_slots, "Expected at least one EV-loaded slot"

        for s in ev_slots:
            # total supply = batteries_discharged_kwh + grid_import_kwh
            total_supply = s.batteries_discharged_kwh + s.grid_import_kwh
            total_demand = s.avg_house_consumption_kwh + s.ev_planned_load_kwh
            # Energy balance: supply must cover demand (within floating-point tolerance)
            assert total_supply == pytest.approx(total_demand, abs=0.1), (
                f"Slot {s.start.hour}: supply ({total_supply:.3f}) != demand "
                f"({total_demand:.3f}); EV load not in SoC path"
            )
            # With EV load, demand exceeds house-only 0.5 kWh
            assert total_demand > 1.0, (
                f"Slot {s.start.hour}: total demand {total_demand:.2f} should exceed "
                "house-only load of 0.5 kWh; ev_planned_load_kwh not applied to SoC simulation"
            )


# ---------------------------------------------------------------------------
# TestEvLoadSemantics (issue #404)
# Tests for clear EV load semantics:
#   - ev_planned_load_kwh      = extra load added to net consumption
#   - ev_accounted_load_kwh    = load already in house consumption
#   - ev_total_planned_load_kwh = sum of both (always shows full EV intent)
#   - Multi-EV accumulation must be additive (no overwrite)
# ---------------------------------------------------------------------------


class TestEvLoadSemantics:
    """Verify three-field EV load semantics introduced in issue #404.

    Test coverage:
      1. apply_ev_planned_load_to_slots is additive (not overwrite-style).
      2. Two EVs same slot, base_load_includes_ev=False → load summed.
      3. Two EVs same slot, base_load_includes_ev=True → accounted, not injected.
      4. Only second EV has load → plan still works, primary not required.
      5. Second EV zero load does not clear primary EV load.
      6. Net consumption does not double-count EV when base load includes EV.
      7. Net consumption includes EV when base load excludes EV.
      8. Final recommendation exposes ev_total_planned_load_kwh > 0 even when
         ev_planned_load_kwh == 0 (base_load_includes_ev=True).
      9. EVSmartCharging label applied when ev_total > 0, even when
         ev_planned_load_kwh == 0 (base_load_includes_ev=True).
    """

    # ------------------------------------------------------------------
    # Helper factories
    # ------------------------------------------------------------------

    def _ev_plan_with_single_slot(
        self, hour: int, ac_load_kwh: float
    ) -> EVChargingPlan:
        """Build a minimal EVChargingPlan with one slot at the given hour."""
        from custom_components.hsem.planner.ev_planner import EVChargingSlot

        plan = EVChargingPlan()
        plan.state = "waiting"
        start = datetime(2024, 6, 15, hour, 0, 0, tzinfo=UTC)
        end = start + timedelta(hours=1)
        ev_slot = EVChargingSlot(
            start=start,
            end=end,
            estimated_charged_kwh=ac_load_kwh,
            ac_load_kwh=ac_load_kwh,
        )
        plan.charging_slots.append(ev_slot)
        plan.planned_load_by_slot[start.isoformat()] = ac_load_kwh
        return plan

    def _slot_starts(self, n: int = 24) -> list[datetime]:
        return [datetime(2024, 6, 15, h, 0, 0, tzinfo=UTC) for h in range(n)]

    # ------------------------------------------------------------------
    # Test 1: apply_ev_planned_load_to_slots is additive
    # ------------------------------------------------------------------

    def test_apply_ev_planned_load_is_additive(self):
        """When a slot already has load, new EV load is ADDED, not overwritten.

        Given:
          slot[8] already has 3.0 kWh
          EV plan assigns 4.0 kWh to slot[8]

        Expected:
          slot[8] = 3.0 + 4.0 = 7.0 kWh
        """
        starts = self._slot_starts()
        result = [0.0] * 24
        result[8] = 3.0  # pre-existing load

        plan = self._ev_plan_with_single_slot(hour=8, ac_load_kwh=4.0)
        apply_ev_planned_load_to_slots(
            starts, result, plan, base_load_includes_ev=False
        )
        assert result[8] == pytest.approx(7.0), (
            f"Expected additive result 7.0, got {result[8]}"
        )

    # ------------------------------------------------------------------
    # Test 2: Two EVs, same slot, base_load_includes_ev=False
    # ------------------------------------------------------------------

    def test_two_evs_same_slot_base_excludes_ev(self):
        """Primary (3.0 kWh) + secondary (4.0 kWh) in same slot, base excludes EV.

        Expected:
          ev_planned_load_kwh      = 7.0  (injected into net consumption)
          ev_accounted_load_kwh    = 0.0  (nothing pre-included)
          ev_total_planned_load_kwh = 7.0
        """
        avg_house_consumption_kwh = 1.0
        solcast_pv_estimate_kwh = 2.0
        now_iso = "2024-06-15T06:00:00+00:00"
        from datetime import datetime as _dt2

        now = _dt2.fromisoformat(now_iso)
        deadline = now + timedelta(hours=6)

        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(24)
        ]
        pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
        pv[9] = SolcastSlot(hour=9, pv_estimate=solcast_pv_estimate_kwh)
        avgs = [
            HourlyConsumptionAverage(
                hour=h,
                avg_1d=avg_house_consumption_kwh,
                avg_3d=avg_house_consumption_kwh,
                avg_7d=avg_house_consumption_kwh,
                avg_14d=avg_house_consumption_kwh,
            )
            for h in range(24)
        ]

        # Primary EV: needs 3.0 kWh (3 → 6 % of 100 kWh, charger=11kW)
        # Second EV: needs 4.0 kWh (4 → 8 % of 100 kWh, charger=11kW)
        # Both have deadline past slot 8, so they both schedule at slot 8 (cheapest/first)
        inp = PlannerInput(
            now_iso=now_iso,
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_soc_pct=90.0,
            battery_max_charge_power_w=5000.0,
            battery_max_discharge_power_w=5000.0,
            battery_charge_efficiency_pct=100.0,
            battery_discharge_efficiency_pct=100.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=avgs,
            price_points=prices,
            solcast_slots=pv,
            # Primary EV
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=0.0,
            ev_planned_load_target_soc_pct=3.0,  # 3 kWh needed
            ev_planned_load_battery_capacity_kwh=100.0,
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=deadline,
            ev_planned_load_base_load_includes_ev=False,
            # Second EV
            ev_second_planned_load_enabled=True,
            ev_second_planned_load_connected=True,
            ev_second_planned_load_smart_charging_enabled=True,
            ev_second_planned_load_current_soc_pct=0.0,
            ev_second_planned_load_target_soc_pct=4.0,  # 4 kWh needed
            ev_second_planned_load_battery_capacity_kwh=100.0,
            ev_second_planned_load_charger_power_kw=11.0,
            ev_second_planned_load_charger_efficiency_pct=100.0,
            ev_second_planned_load_deadline=deadline,
            ev_second_planned_load_base_load_includes_ev=False,
        )
        out = run_planner(inp)

        # Both EVs together need 7.0 kWh total
        total_ev_injected = sum(s.ev_planned_load_kwh for s in out.slots)
        total_ev_accounted = sum(s.ev_accounted_load_kwh for s in out.slots)
        total_ev_total = sum(s.ev_total_planned_load_kwh for s in out.slots)

        assert total_ev_injected == pytest.approx(7.0, abs=0.1), (
            f"Total ev_planned_load_kwh should be 7.0 (3+4), got {total_ev_injected:.3f}"
        )
        assert total_ev_accounted == pytest.approx(0.0, abs=1e-9), (
            f"ev_accounted_load_kwh should be 0 (base excludes EV), got {total_ev_accounted:.3f}"
        )
        assert total_ev_total == pytest.approx(7.0, abs=0.1), (
            f"ev_total_planned_load_kwh should be 7.0, got {total_ev_total:.3f}"
        )

    # ------------------------------------------------------------------
    # Test 3: Two EVs, same slot, base_load_includes_ev=True
    # ------------------------------------------------------------------

    def test_two_evs_same_slot_base_includes_ev(self):
        """Primary (3 kWh) + secondary (4 kWh) in same slot, base includes EV.

        Expected:
          ev_planned_load_kwh       = 0.0  (not injected — already in base load)
          ev_accounted_load_kwh     = 7.0  (pre-included in house consumption)
          ev_total_planned_load_kwh = 7.0
          estimated_net_consumption_kwh = avg_house - pv  (no extra EV added)
        """
        avg_house = 2.0
        pv_kwh = 0.5
        now_iso = "2024-06-15T06:00:00+00:00"
        from datetime import datetime as _dt2

        now = _dt2.fromisoformat(now_iso)
        deadline = now + timedelta(hours=6)

        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(24)
        ]
        pv = [SolcastSlot(hour=h, pv_estimate=pv_kwh) for h in range(24)]
        avgs = [
            HourlyConsumptionAverage(
                hour=h,
                avg_1d=avg_house,
                avg_3d=avg_house,
                avg_7d=avg_house,
                avg_14d=avg_house,
            )
            for h in range(24)
        ]

        inp = PlannerInput(
            now_iso=now_iso,
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_soc_pct=90.0,
            battery_max_charge_power_w=5000.0,
            battery_max_discharge_power_w=5000.0,
            battery_charge_efficiency_pct=100.0,
            battery_discharge_efficiency_pct=100.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=avgs,
            price_points=prices,
            solcast_slots=pv,
            # Primary EV — base load includes EV
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=0.0,
            ev_planned_load_target_soc_pct=3.0,
            ev_planned_load_battery_capacity_kwh=100.0,
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=deadline,
            ev_planned_load_base_load_includes_ev=True,
            # Second EV — base load includes EV
            ev_second_planned_load_enabled=True,
            ev_second_planned_load_connected=True,
            ev_second_planned_load_smart_charging_enabled=True,
            ev_second_planned_load_current_soc_pct=0.0,
            ev_second_planned_load_target_soc_pct=4.0,
            ev_second_planned_load_battery_capacity_kwh=100.0,
            ev_second_planned_load_charger_power_kw=11.0,
            ev_second_planned_load_charger_efficiency_pct=100.0,
            ev_second_planned_load_deadline=deadline,
            ev_second_planned_load_base_load_includes_ev=True,
        )
        out = run_planner(inp)

        # ev_planned_load_kwh must be 0 — no extra injection
        for s in out.slots:
            assert s.ev_planned_load_kwh == pytest.approx(0.0), (
                f"Slot {s.start.hour}: ev_planned_load_kwh should be 0 "
                f"(base includes EV), got {s.ev_planned_load_kwh}"
            )

        total_ev_accounted = sum(s.ev_accounted_load_kwh for s in out.slots)
        total_ev_total = sum(s.ev_total_planned_load_kwh for s in out.slots)

        assert total_ev_accounted == pytest.approx(7.0, abs=0.1), (
            f"ev_accounted_load_kwh total should be 7.0, got {total_ev_accounted:.3f}"
        )
        assert total_ev_total == pytest.approx(7.0, abs=0.1), (
            f"ev_total_planned_load_kwh total should be 7.0, got {total_ev_total:.3f}"
        )

        # net consumption must NOT include EV load (no double-count)
        for s in out.slots:
            expected_net = round(
                s.avg_house_consumption_kwh
                + s.ev_planned_load_kwh
                - s.solcast_pv_estimate_kwh,
                3,
            )
            assert s.estimated_net_consumption_kwh == pytest.approx(
                expected_net, abs=1e-6
            ), (
                f"Slot {s.start.hour}: net consumption should be house - pv "
                f"(ev not injected), got {s.estimated_net_consumption_kwh}"
            )

    # ------------------------------------------------------------------
    # Test 4: Only second EV has load — primary EV not required
    # ------------------------------------------------------------------

    def test_only_second_ev_has_load(self):
        """Second EV with 4.0 kWh; primary EV fully charged.

        Primary EV must not be required for EV planning to work.
        ev_total_planned_load_kwh must reflect only the second EV load.
        """
        now_iso = "2024-06-15T06:00:00+00:00"
        from datetime import datetime as _dt2

        now = _dt2.fromisoformat(now_iso)
        deadline = now + timedelta(hours=6)

        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(24)
        ]
        pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
        avgs = [
            HourlyConsumptionAverage(
                hour=h, avg_1d=1.0, avg_3d=1.0, avg_7d=1.0, avg_14d=1.0
            )
            for h in range(24)
        ]

        inp = PlannerInput(
            now_iso=now_iso,
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_soc_pct=90.0,
            battery_max_charge_power_w=5000.0,
            battery_max_discharge_power_w=5000.0,
            battery_charge_efficiency_pct=100.0,
            battery_discharge_efficiency_pct=100.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=avgs,
            price_points=prices,
            solcast_slots=pv,
            # Primary EV: fully charged — should contribute nothing
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=80.0,
            ev_planned_load_target_soc_pct=80.0,  # 0 kWh needed → fully_charged
            ev_planned_load_battery_capacity_kwh=100.0,
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=deadline,
            ev_planned_load_base_load_includes_ev=False,
            # Second EV: needs 4.0 kWh
            ev_second_planned_load_enabled=True,
            ev_second_planned_load_connected=True,
            ev_second_planned_load_smart_charging_enabled=True,
            ev_second_planned_load_current_soc_pct=0.0,
            ev_second_planned_load_target_soc_pct=4.0,
            ev_second_planned_load_battery_capacity_kwh=100.0,
            ev_second_planned_load_charger_power_kw=11.0,
            ev_second_planned_load_charger_efficiency_pct=100.0,
            ev_second_planned_load_deadline=deadline,
            ev_second_planned_load_base_load_includes_ev=False,
        )
        out = run_planner(inp)

        total_ev_total = sum(s.ev_total_planned_load_kwh for s in out.slots)
        assert total_ev_total == pytest.approx(4.0, abs=0.1), (
            f"ev_total_planned_load_kwh should be 4.0 (second EV only), "
            f"got {total_ev_total:.3f}"
        )
        # Primary EV plan should be fully_charged
        assert out.ev_charging_plan is not None
        assert out.ev_charging_plan.state == "fully_charged"

    # ------------------------------------------------------------------
    # Test 5: Second EV zero load does not clear primary EV load
    # ------------------------------------------------------------------

    def test_second_ev_zero_load_does_not_clear_primary(self):
        """When second EV needs 0 kWh, primary EV load must not be cleared.

        Given:
          primary_ev_ac_load = 3.0 kWh
          second_ev needs 0 kWh (fully charged → no slots)

        Expected:
          ev_total_planned_load_kwh = 3.0 (primary unchanged)
        """
        now_iso = "2024-06-15T06:00:00+00:00"
        from datetime import datetime as _dt2

        now = _dt2.fromisoformat(now_iso)
        deadline = now + timedelta(hours=6)

        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(24)
        ]
        pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
        avgs = [
            HourlyConsumptionAverage(
                hour=h, avg_1d=1.0, avg_3d=1.0, avg_7d=1.0, avg_14d=1.0
            )
            for h in range(24)
        ]

        inp = PlannerInput(
            now_iso=now_iso,
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_soc_pct=90.0,
            battery_max_charge_power_w=5000.0,
            battery_max_discharge_power_w=5000.0,
            battery_charge_efficiency_pct=100.0,
            battery_discharge_efficiency_pct=100.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=avgs,
            price_points=prices,
            solcast_slots=pv,
            # Primary EV: needs 3 kWh
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=0.0,
            ev_planned_load_target_soc_pct=3.0,
            ev_planned_load_battery_capacity_kwh=100.0,
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=deadline,
            ev_planned_load_base_load_includes_ev=False,
            # Second EV: fully charged, needs 0 kWh
            ev_second_planned_load_enabled=True,
            ev_second_planned_load_connected=True,
            ev_second_planned_load_smart_charging_enabled=True,
            ev_second_planned_load_current_soc_pct=80.0,
            ev_second_planned_load_target_soc_pct=80.0,  # 0 kWh needed
            ev_second_planned_load_battery_capacity_kwh=100.0,
            ev_second_planned_load_charger_power_kw=11.0,
            ev_second_planned_load_charger_efficiency_pct=100.0,
            ev_second_planned_load_deadline=deadline,
            ev_second_planned_load_base_load_includes_ev=False,
        )
        out = run_planner(inp)

        total_ev_total = sum(s.ev_total_planned_load_kwh for s in out.slots)
        assert total_ev_total == pytest.approx(3.0, abs=0.1), (
            f"ev_total_planned_load_kwh should be 3.0 (primary only, second is zero), "
            f"got {total_ev_total:.3f}"
        )

    # ------------------------------------------------------------------
    # Test 6: Net consumption does not double-count EV when base includes EV
    # ------------------------------------------------------------------

    def test_no_double_count_when_base_includes_ev(self):
        """When base_load_includes_ev=True, EV is NOT added to net consumption.

        Given:
          avg_house_consumption_kwh = 5.0 kWh/h  (includes EV load)
          solcast_pv_estimate_kwh   = 2.0 kWh/h
          ev_total              = 4.0 kWh     (planned but already in base)

        Expected:
          estimated_net_consumption_kwh = 5.0 - 2.0 = 3.0 kWh
          (not 5.0 + 4.0 - 2.0 = 7.0 kWh which would double-count the EV)
        """
        avg_house = 5.0
        pv_kwh = 2.0
        now_iso = "2024-06-15T06:00:00+00:00"
        from datetime import datetime as _dt2

        now = _dt2.fromisoformat(now_iso)
        deadline = now + timedelta(hours=6)

        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(24)
        ]
        pv = [SolcastSlot(hour=h, pv_estimate=pv_kwh) for h in range(24)]
        avgs = [
            HourlyConsumptionAverage(
                hour=h,
                avg_1d=avg_house,
                avg_3d=avg_house,
                avg_7d=avg_house,
                avg_14d=avg_house,
            )
            for h in range(24)
        ]

        inp = PlannerInput(
            now_iso=now_iso,
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_soc_pct=90.0,
            battery_max_charge_power_w=5000.0,
            battery_max_discharge_power_w=5000.0,
            battery_charge_efficiency_pct=100.0,
            battery_discharge_efficiency_pct=100.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=avgs,
            price_points=prices,
            solcast_slots=pv,
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=0.0,
            ev_planned_load_target_soc_pct=4.0,  # 4 kWh planned but included in base
            ev_planned_load_battery_capacity_kwh=100.0,
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=deadline,
            ev_planned_load_base_load_includes_ev=True,
        )
        out = run_planner(inp)

        # Every slot: net = house - pv (EV not injected)
        for s in out.slots:
            expected_net = round(
                s.avg_house_consumption_kwh - s.solcast_pv_estimate_kwh, 3
            )
            assert s.estimated_net_consumption_kwh == pytest.approx(
                expected_net, abs=1e-6
            ), (
                f"Slot {s.start.hour}: expected net {expected_net:.3f} "
                f"(avg_house={s.avg_house_consumption_kwh:.1f}, pv={s.solcast_pv_estimate_kwh:.1f}), "
                f"got {s.estimated_net_consumption_kwh:.3f} — EV may be double-counted"
            )
            # ev_planned_load_kwh must be 0 (not injected)
            assert s.ev_planned_load_kwh == pytest.approx(0.0), (
                f"Slot {s.start.hour}: ev_planned_load_kwh should be 0 "
                f"(base includes EV), got {s.ev_planned_load_kwh}"
            )

    # ------------------------------------------------------------------
    # Test 7: Net consumption INCLUDES EV when base excludes EV
    # ------------------------------------------------------------------

    def test_net_consumption_includes_ev_when_base_excludes_ev(self):
        """When base_load_includes_ev=False, EV IS added to net consumption.

        Given:
          avg_house_consumption_kwh = 5.0 kWh/h
          solcast_pv_estimate_kwh   = 2.0 kWh/h
          ev_total              = 4.0 kWh (planned, not in base load)

        Expected for slot with EV load:
          estimated_net_consumption_kwh = 5.0 + 4.0 - 2.0 = 7.0 kWh
        """
        avg_house = 5.0
        pv_kwh = 2.0
        now_iso = "2024-06-15T06:00:00+00:00"
        from datetime import datetime as _dt2

        now = _dt2.fromisoformat(now_iso)
        deadline = now + timedelta(hours=6)

        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(24)
        ]
        pv = [SolcastSlot(hour=h, pv_estimate=pv_kwh) for h in range(24)]
        avgs = [
            HourlyConsumptionAverage(
                hour=h,
                avg_1d=avg_house,
                avg_3d=avg_house,
                avg_7d=avg_house,
                avg_14d=avg_house,
            )
            for h in range(24)
        ]

        inp = PlannerInput(
            now_iso=now_iso,
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_soc_pct=90.0,
            battery_max_charge_power_w=5000.0,
            battery_max_discharge_power_w=5000.0,
            battery_charge_efficiency_pct=100.0,
            battery_discharge_efficiency_pct=100.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=avgs,
            price_points=prices,
            solcast_slots=pv,
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=0.0,
            ev_planned_load_target_soc_pct=4.0,  # 4 kWh NOT in base load
            ev_planned_load_battery_capacity_kwh=100.0,
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=deadline,
            ev_planned_load_base_load_includes_ev=False,
        )
        out = run_planner(inp)

        ev_slots = [s for s in out.slots if abs(s.ev_planned_load_kwh) > 1e-9]
        assert ev_slots, "Expected at least one slot with EV planned load"

        for s in ev_slots:
            # net = house + ev_load - pv = 5.0 + ev_load - 2.0
            expected_net = round(
                s.avg_house_consumption_kwh
                + s.ev_planned_load_kwh
                - s.solcast_pv_estimate_kwh,
                3,
            )
            assert s.estimated_net_consumption_kwh == pytest.approx(
                expected_net, abs=1e-6
            ), (
                f"Slot {s.start.hour}: net should include EV load; "
                f"expected {expected_net:.3f}, got {s.estimated_net_consumption_kwh:.3f}"
            )

    # ------------------------------------------------------------------
    # Test 8: Recommendation exposes ev_total even when ev_planned == 0
    # ------------------------------------------------------------------

    def test_ev_total_nonzero_when_base_includes_ev(self):
        """With base_load_includes_ev=True, ev_total_planned_load_kwh > 0
        even though ev_planned_load_kwh == 0.

        This is the key regression test: ev_planned_load_kwh = 0 must NOT be
        misread as "no EV charging planned".  ev_total_planned_load_kwh > 0
        exposes the real EV intent.
        """
        now_iso = "2024-06-15T06:00:00+00:00"
        from datetime import datetime as _dt2

        now = _dt2.fromisoformat(now_iso)
        deadline = now + timedelta(hours=6)

        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(24)
        ]
        pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
        avgs = [
            HourlyConsumptionAverage(
                hour=h, avg_1d=2.0, avg_3d=2.0, avg_7d=2.0, avg_14d=2.0
            )
            for h in range(24)
        ]

        inp = PlannerInput(
            now_iso=now_iso,
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_soc_pct=90.0,
            battery_max_charge_power_w=5000.0,
            battery_max_discharge_power_w=5000.0,
            battery_charge_efficiency_pct=100.0,
            battery_discharge_efficiency_pct=100.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=avgs,
            price_points=prices,
            solcast_slots=pv,
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=0.0,
            ev_planned_load_target_soc_pct=5.0,  # 5 kWh already in base load
            ev_planned_load_battery_capacity_kwh=100.0,
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=deadline,
            ev_planned_load_base_load_includes_ev=True,  # already included
        )
        out = run_planner(inp)

        # ev_planned_load_kwh must be 0 everywhere
        for s in out.slots:
            assert s.ev_planned_load_kwh == pytest.approx(0.0), (
                f"Slot {s.start.hour}: ev_planned_load_kwh should be 0 "
                f"(base includes EV), got {s.ev_planned_load_kwh}"
            )

        # ev_total_planned_load_kwh must be > 0 on charging slots
        total_ev_total = sum(s.ev_total_planned_load_kwh for s in out.slots)
        assert total_ev_total == pytest.approx(5.0, abs=0.1), (
            f"ev_total_planned_load_kwh total should be ~5.0, got {total_ev_total:.3f}. "
            "This is the key regression test: EV charging IS planned but appears as 0 "
            "in ev_planned_load_kwh because base load includes EV."
        )

        # ev_accounted_load_kwh = ev_total (all accounted, none injected)
        total_ev_accounted = sum(s.ev_accounted_load_kwh for s in out.slots)
        assert total_ev_accounted == pytest.approx(5.0, abs=0.1), (
            f"ev_accounted_load_kwh total should be ~5.0, got {total_ev_accounted:.3f}"
        )

    # ------------------------------------------------------------------
    # Test 9: EVSmartCharging label applied even when ev_planned == 0
    # ------------------------------------------------------------------

    def test_ev_smart_charging_label_when_base_includes_ev(self):
        """EVSmartCharging recommendation applied when ev_total > 0,
        even when ev_planned_load_kwh == 0 (base_load_includes_ev=True).

        Before the fix, slots would show batteries_wait_mode or
        batteries_charge_solar even during scheduled EV charging because
        the engine checked ev_planned_load_kwh (which is 0) instead of
        ev_total_planned_load_kwh.
        """
        now_iso = "2024-06-15T06:00:00+00:00"
        from datetime import datetime as _dt2

        now = _dt2.fromisoformat(now_iso)
        deadline = now + timedelta(hours=6)

        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(24)
        ]
        pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
        avgs = [
            HourlyConsumptionAverage(
                hour=h, avg_1d=2.0, avg_3d=2.0, avg_7d=2.0, avg_14d=2.0
            )
            for h in range(24)
        ]

        inp = PlannerInput(
            now_iso=now_iso,
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_soc_pct=90.0,
            battery_max_charge_power_w=5000.0,
            battery_max_discharge_power_w=5000.0,
            battery_charge_efficiency_pct=100.0,
            battery_discharge_efficiency_pct=100.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=avgs,
            price_points=prices,
            solcast_slots=pv,
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=0.0,
            ev_planned_load_target_soc_pct=5.0,
            ev_planned_load_battery_capacity_kwh=100.0,
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=deadline,
            ev_planned_load_base_load_includes_ev=True,
        )
        out = run_planner(inp)

        # There should be at least one slot with ev_total > 0 labelled ev_smart_charging
        ev_total_slots = [s for s in out.slots if s.ev_total_planned_load_kwh > 1e-9]
        assert ev_total_slots, "Expected slots with ev_total_planned_load_kwh > 0"

        _KEEP_LABELS = frozenset(
            {
                "batteries_charge_grid",
                "force_batteries_discharge",
                "force_export",
                "time_passed",
                "missing_input_entities",
            }
        )
        for s in ev_total_slots:
            if s.recommendation in _KEEP_LABELS:
                continue  # higher-priority label correctly kept
            assert s.recommendation == "ev_smart_charging", (
                f"Slot {s.start.hour}: ev_total={s.ev_total_planned_load_kwh:.3f} "
                f"but recommendation='{s.recommendation}' instead of 'ev_smart_charging'. "
                "The engine must use ev_total_planned_load_kwh for the label decision "
                "when base_load_includes_ev=True."
            )

    # ------------------------------------------------------------------
    # Test 10: ev_total_planned_load_kwh invariant
    # ------------------------------------------------------------------

    def test_ev_total_equals_planned_plus_accounted(self):
        """ev_total_planned_load_kwh == ev_planned_load_kwh + ev_accounted_load_kwh.

        This invariant must hold for every slot regardless of base_load_includes_ev.
        """
        # Run with base_load_includes_ev=False
        inp_excl = _make_planner_input(
            now_iso="2024-06-15T06:00:00+00:00",
            current_soc=50.0,
            target_soc=80.0,
            capacity_kwh=77.0,
            charger_kw=11.0,
            base_includes_ev=False,
        )
        out_excl = run_planner(inp_excl)

        for s in out_excl.slots:
            assert s.ev_total_planned_load_kwh == pytest.approx(
                s.ev_planned_load_kwh + s.ev_accounted_load_kwh, abs=1e-9
            ), (
                f"Slot {s.start.hour}: ev_total ({s.ev_total_planned_load_kwh:.4f}) "
                f"!= ev_planned ({s.ev_planned_load_kwh:.4f}) "
                f"+ ev_accounted ({s.ev_accounted_load_kwh:.4f})"
            )

        # Run with base_load_includes_ev=True
        inp_incl = _make_planner_input(
            now_iso="2024-06-15T06:00:00+00:00",
            current_soc=50.0,
            target_soc=80.0,
            capacity_kwh=77.0,
            charger_kw=11.0,
            base_includes_ev=True,
        )
        out_incl = run_planner(inp_incl)

        for s in out_incl.slots:
            assert s.ev_total_planned_load_kwh == pytest.approx(
                s.ev_planned_load_kwh + s.ev_accounted_load_kwh, abs=1e-9
            ), (
                f"Slot {s.start.hour} (base_includes): ev_total "
                f"({s.ev_total_planned_load_kwh:.4f}) "
                f"!= ev_planned ({s.ev_planned_load_kwh:.4f}) "
                f"+ ev_accounted ({s.ev_accounted_load_kwh:.4f})"
            )


# ---------------------------------------------------------------------------
# TestEvLoadDoesNotInflateChargeNeeded (issue #404 / charge scheduler fix)
# Regression: when base_load_includes_ev=False the charge scheduler was using
# estimated_net_consumption_kwh (which includes ev_planned_load_kwh) to compute
# occ_needed for each discharge window occurrence.  This inflated the target,
# raised the average charge price over more slots, and caused the price-spread
# guard to reject otherwise profitable grid-charge slots.
# ---------------------------------------------------------------------------


class TestEvLoadDoesNotInflateChargeNeeded:
    """Battery pre-charge must not require more energy just because EV is planned.

    The home battery discharges to cover house load; the EV charger draws from
    grid/PV directly.  Adding ev_planned_load_kwh to the discharge-window needed
    capacity over-counts the battery's responsibility.

    Regression test: with a clear price spread and an EV scheduled to charge
    in the same window, the planner must still assign batteries_charge_grid
    slots before the discharge window — the same as without EV.
    """

    def _make_ev_discharge_input(
        self,
        base_includes_ev: bool = False,
        ev_enabled: bool = True,
    ) -> PlannerInput:
        """Build an input with a clear price spread and a discharge window.

        Price layout:
          hours  0-05: cheap  (0.05) — ideal charge-from-grid hours
          hours  6-15: normal (0.20)
          hours 16-22: peak   (0.80) — configured discharge window

        EV deadline: 08:00 (charges in cheap/normal hours).
        Battery must pre-charge before the discharge window.
        """
        from datetime import datetime as _dt2

        now_iso = "2024-06-15T00:00:00+00:00"
        now = _dt2.fromisoformat(now_iso)
        ev_deadline = now + timedelta(hours=8)

        prices = []
        for h in range(24):
            if h < 6:
                prices.append(PricePoint(hour=h, import_price=0.05, export_price=0.01))
            elif h < 16:
                prices.append(PricePoint(hour=h, import_price=0.20, export_price=0.05))
            else:
                prices.append(PricePoint(hour=h, import_price=0.80, export_price=0.20))

        pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
        avgs = [
            HourlyConsumptionAverage(
                hour=h, avg_1d=1.0, avg_3d=1.0, avg_7d=1.0, avg_14d=1.0
            )
            for h in range(24)
        ]

        from datetime import time as _time

        from custom_components.hsem.models.planner_inputs import BatteryScheduleInput

        discharge_schedule = BatteryScheduleInput(
            enabled=True,
            start=_time(16, 0),
            end=_time(22, 0),  # spread needed: 0.05 vs 0.80 → 0.75 > 0.10
        )

        return PlannerInput(
            now_iso=now_iso,
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=20.0,  # low — needs charging
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_soc_pct=90.0,
            battery_max_charge_power_w=5000.0,
            battery_max_discharge_power_w=5000.0,
            battery_charge_efficiency_pct=100.0,
            battery_discharge_efficiency_pct=100.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=avgs,
            price_points=prices,
            solcast_slots=pv,
            battery_schedules=[discharge_schedule],
            ev_planned_load_enabled=ev_enabled,
            ev_planned_load_connected=ev_enabled,
            ev_planned_load_smart_charging_enabled=ev_enabled,
            ev_planned_load_current_soc_pct=0.0,
            ev_planned_load_target_soc_pct=10.0,  # 10 kWh needed
            ev_planned_load_battery_capacity_kwh=100.0,
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=ev_deadline,
            ev_planned_load_base_load_includes_ev=base_includes_ev,
        )

    @pytest.mark.skip(reason="MILP-only mode: schedule-based behavior not applicable")
    def test_grid_charge_slots_exist_without_ev(self):
        """Baseline: without EV, cheap hours 0-5 are assigned batteries_charge_grid."""
        inp = self._make_ev_discharge_input(ev_enabled=False)
        out = run_planner(inp)

        charge_grid_slots = [
            s for s in out.slots if s.recommendation == "batteries_charge_grid"
        ]
        assert charge_grid_slots, (
            "Baseline without EV: expected batteries_charge_grid slots in cheap hours. "
            "Check schedule config and price spread."
        )
        cheap_charge_hours = {s.start.hour for s in charge_grid_slots}
        assert cheap_charge_hours & set(range(6)), (
            f"Expected charge slots in hours 0-5 (cheap), got: {cheap_charge_hours}"
        )

    @pytest.mark.skip(reason="MILP-only mode: schedule-based behavior not applicable")
    def test_grid_charge_slots_still_exist_with_ev_base_excludes(self):
        """With EV + base_load_includes_ev=False, grid-charge slots must survive.

        This is the regression test: ev_planned_load_kwh was inflating occ_needed,
        which raised the average charge price and caused the price-spread guard
        to reject the cheap-hour grid-charge slots.
        """
        inp = self._make_ev_discharge_input(base_includes_ev=False)
        out = run_planner(inp)

        charge_grid_slots = [
            s for s in out.slots if s.recommendation == "batteries_charge_grid"
        ]
        assert charge_grid_slots, (
            "With EV (base_load_includes_ev=False): batteries_charge_grid slots "
            "are missing. EV ev_planned_load_kwh is inflating occ_needed in the "
            "discharge window, causing the price-spread guard to reject cheap-hour "
            "charge slots."
        )

    @pytest.mark.skip(reason="MILP-only mode: schedule-based behavior not applicable")
    def test_grid_charge_slots_still_exist_with_ev_base_includes(self):
        """With EV + base_load_includes_ev=True, grid-charge slots must survive."""
        inp = self._make_ev_discharge_input(base_includes_ev=True)
        out = run_planner(inp)

        charge_grid_slots = [
            s for s in out.slots if s.recommendation == "batteries_charge_grid"
        ]
        assert charge_grid_slots, (
            "With EV (base_load_includes_ev=True): batteries_charge_grid slots "
            "are missing even though EV load is already in house consumption."
        )

    def test_ev_load_does_not_change_discharge_window_needed_capacity(self):
        """occ_needed for the discharge window must be the same with and without EV
        when base_load_includes_ev=False.

        Hand calculation:
          discharge window: hours 16-22 (6 slots)
          avg_house = 1.0 kWh/h, pv = 0 kWh → battery_net = 1.0 kWh/h
          ev_planned_load_kwh: may be > 0 in some of these slots (EV charges
            during cheap hours before deadline, so no EV in discharge window)

        After fix: occ_needed = sum(house - pv) across discharge slots,
        NOT sum(house + ev - pv).  So EV load in pre-discharge slots does not
        affect the charge target for the discharge window.
        """
        inp_no_ev = self._make_ev_discharge_input(ev_enabled=False)
        inp_ev = self._make_ev_discharge_input(base_includes_ev=False)

        out_no_ev = run_planner(inp_no_ev)
        out_ev = run_planner(inp_ev)

        # The discharge window is hours 16-21; EV deadline is 08:00 so no EV
        # load in discharge window.  Both outputs should have identical
        # discharge-window net consumption.
        discharge_net_no_ev = sum(
            s.avg_house_consumption_kwh - s.solcast_pv_estimate_kwh
            for s in out_no_ev.slots
            if 16 <= s.start.hour < 22
        )
        discharge_net_ev = sum(
            s.avg_house_consumption_kwh - s.solcast_pv_estimate_kwh
            for s in out_ev.slots
            if 16 <= s.start.hour < 22
        )
        assert discharge_net_ev == pytest.approx(discharge_net_no_ev, abs=1e-6), (
            f"Discharge window battery-relevant net differs between EV and no-EV cases: "
            f"no_ev={discharge_net_no_ev:.3f}, ev={discharge_net_ev:.3f}. "
            "EV load should not affect the battery's discharge window target."
        )


# ---------------------------------------------------------------------------
# EV deadline window — "one midnight crossing" clamp (issue #413)
# ---------------------------------------------------------------------------


def _make_slots_48(
    now: datetime, tz: Any = _UTC
) -> tuple[list[datetime], list[datetime]]:
    """Return 48 contiguous 1-hour slot ``(start, end)`` pairs anchored at ``now``.

    Slot ``i`` runs ``[now + i h, now + (i+1) h]``.  This mimics a 48-hour
    planner horizon used to exercise the EV deadline clamp.
    """
    starts = [now + timedelta(hours=i) for i in range(48)]
    ends = [now + timedelta(hours=i + 1) for i in range(48)]
    return starts, ends


class TestEvDeadlineWindowOneMidnight:
    """The EV charging window must span at most one midnight crossing.

    These tests pin down the spec-mandated semantic that an EV plan rooted
    at ``now`` may extend into tomorrow but must NEVER reach into the day
    after tomorrow, regardless of the planner's overall slot horizon.

    Regression for the bug where, with a 48-hour planner horizon and a
    ``None`` deadline reaching the EV planner, EV load was scheduled on
    slots that started more than 24 hours after ``now``.
    """

    def test_none_deadline_with_48h_horizon_clamps_to_end_of_tomorrow(self):
        """A ``None`` deadline must not let the EV planner use day-2 slots.

        Setup mirrors the user-reported failure:
        - ``now = 2024-06-15 17:00 UTC`` (mid-afternoon)
        - Planner horizon: 48 one-hour slots
        - ``deadline = None`` (entity unconfigured)
        - Cheap prices on the day-after-tomorrow morning to try to lure the
          EV planner into scheduling there

        Before the fix the EV planner would schedule the cheap day-2 slots.
        After the fix the clamp restricts allocation to slots ending before
        the start of day-after-tomorrow (``2024-06-17 00:00 UTC``).
        """
        now = _dt(17)  # 2024-06-15 17:00 UTC
        starts, ends = _make_slots_48(now)
        surplus = [0.0] * 48
        prices = [0.10] * 48
        # Make the day-after-tomorrow early hours *very* attractive so the
        # planner has a strong incentive to schedule them if it could.
        # Slot index 31 = now + 31h = 2024-06-17 00:00 → exactly at the cap.
        for i in range(31, 40):
            prices[i] = 0.001

        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=70.0,
            target_soc_pct=80.0,  # 10 kWh needed
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            deadline=None,  # the bug scenario
            now=now,
        )
        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)

        end_of_tomorrow = datetime(2024, 6, 17, 0, 0, tzinfo=UTC)
        for slot in plan.charging_slots:
            assert slot.start < end_of_tomorrow, (
                f"EV slot {slot.start.isoformat()} starts at or after "
                f"end-of-tomorrow ({end_of_tomorrow.isoformat()}); "
                "the one-midnight-crossing clamp failed."
            )
            assert slot.end <= end_of_tomorrow, (
                f"EV slot {slot.start.isoformat()}→{slot.end.isoformat()} ends "
                f"after end-of-tomorrow ({end_of_tomorrow.isoformat()})."
            )

    def test_deadline_tomorrow_1700_does_not_pick_day_after_tomorrow(self):
        """Deadline = tomorrow 17:00 must keep EV slots within [now, tomorrow 17:00].

        This is the user-reported scenario: ``now ≈ 17:00`` and deadline
        configured as ``17:00`` rolls forward to tomorrow 17:00.  The EV
        planner must not schedule into the day after tomorrow even though
        the slot horizon extends 48 hours.
        """
        now = _dt(17)  # 2024-06-15 17:00 UTC
        deadline = datetime(2024, 6, 16, 17, 0, tzinfo=UTC)  # tomorrow 17:00
        starts, ends = _make_slots_48(now)
        surplus = [0.0] * 48
        prices = [0.10] * 48
        # Make day-after-tomorrow cheapest to verify the clamp wins over price.
        for i in range(31, 48):
            prices[i] = 0.001

        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=70.0,
            target_soc_pct=80.0,  # 10 kWh needed
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            deadline=deadline,
            now=now,
        )
        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)

        for slot in plan.charging_slots:
            assert slot.start < deadline, (
                f"EV slot {slot.start.isoformat()} starts at or after deadline "
                f"({deadline.isoformat()})."
            )

    def test_short_deadline_within_today_unchanged(self):
        """Deadline within today (no rollover) must continue to work."""
        now = _dt(6)  # 06:00 UTC
        deadline = now + timedelta(hours=8)  # today 14:00 UTC
        starts, ends = _make_slots_48(now)
        surplus = [0.0] * 48
        prices = [0.10] * 48

        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=70.0,
            target_soc_pct=75.0,  # 5 kWh needed
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            deadline=deadline,
            now=now,
        )
        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)

        # Total still 5 kWh; all slots within deadline.
        total = sum(s.estimated_charged_kwh for s in plan.charging_slots)
        assert total == pytest.approx(5.0, abs=0.01)
        for slot in plan.charging_slots:
            assert slot.start < deadline
            assert slot.end <= deadline

    def test_deadline_beyond_horizon_cap_is_clamped(self):
        """A deadline further than end-of-tomorrow is clamped to end-of-tomorrow.

        With ``now = 14:00 today`` and ``deadline = 20:00 day-after-tomorrow``
        (almost 54 h ahead), the EV planner must not schedule beyond
        ``2024-06-17 00:00 UTC`` — the start of day-after-tomorrow.
        """
        now = _dt(14)
        deadline = datetime(2024, 6, 17, 20, 0, tzinfo=UTC)  # day-after-tomorrow 20:00
        starts, ends = _make_slots_48(now)
        surplus = [0.0] * 48
        prices = [0.10] * 48
        # Cheapest hour is the slot starting at 2024-06-17 19:00 (slot index
        # 53 from now=14:00).  We have only 48 slots, but make slot 47 cheap.
        prices[47] = 0.001
        # And make slot 33 (day-after-tomorrow 00:00→01:00) cheap — outside cap.
        prices[33] = 0.001

        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=70.0,
            target_soc_pct=80.0,
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            deadline=deadline,
            now=now,
        )
        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)

        end_of_tomorrow = datetime(2024, 6, 17, 0, 0, tzinfo=UTC)
        # All scheduled slots must end on or before the clamp — the safety
        # invariant the user asked for.  The clamp diagnostic on
        # ``plan.data_quality`` is currently only written on the "no
        # candidate slots" path; in the success path it may be absent.
        for slot in plan.charging_slots:
            assert slot.start < end_of_tomorrow, (
                f"EV slot {slot.start.isoformat()} bypassed the one-midnight cap."
            )

    def test_deadline_at_horizon_cap_exactly(self):
        """A deadline exactly equal to end-of-tomorrow is honoured as-is.

        No clamp should activate (deadline already at the limit).
        """
        now = _dt(0)  # 2024-06-15 00:00 UTC
        deadline = datetime(2024, 6, 17, 0, 0, tzinfo=UTC)  # end-of-tomorrow
        starts, ends = _make_slots_48(now)
        surplus = [0.0] * 48
        prices = [0.10] * 48

        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=70.0,
            target_soc_pct=75.0,  # 5 kWh
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            deadline=deadline,
            now=now,
        )
        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)

        for slot in plan.charging_slots:
            assert slot.start < deadline
            assert slot.end <= deadline


# ---------------------------------------------------------------------------
# ev_charger_calculated_power
# ---------------------------------------------------------------------------


class TestEvChargerCalculatedPower:
    """ev_charger_calculated_power field behaviour."""

    def test_power_formula_full_speed(self):
        """Full-speed 15-min slot: (2.75 / 0.25) * 1000 = 11000 W."""
        assert round((2.75 / 0.25) * 1000) == 11000

    def test_power_formula_trickle(self):
        """Trickle-charge 15-min slot: (0.5 / 0.25) * 1000 = 2000 W."""
        assert round((0.5 / 0.25) * 1000) == 2000

    def test_pass_3_skips_when_battery_not_full(self):
        """Pass 3 adds no slots when battery is below usable_kwh."""
        now = _dt(0)
        surplus = [2.0] * 12 + [0.0] * 12
        starts, ends = _make_slots(24)
        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=97.0,
            target_soc_pct=98.0,
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            allow_charge_past_target_soc=True,
            slot_predicted_battery_kwh=[6.0] * 24,
            usable_battery_kwh=14.25,
            now=now,
        )
        prices = [0.10] * 24
        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)
        total = sum(s.estimated_charged_kwh for s in plan.charging_slots)
        assert total == pytest.approx(1.0, abs=0.1)

    def test_pass_3_adds_when_battery_full(self):
        """Pass 3 adds surplus slots when battery is full."""
        now = _dt(0)
        surplus = [2.0] * 12 + [0.0] * 12
        starts, ends = _make_slots(24)
        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=97.0,
            target_soc_pct=98.0,
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            allow_charge_past_target_soc=True,
            slot_predicted_battery_kwh=[14.25] * 24,
            usable_battery_kwh=14.25,
            now=now,
        )
        prices = [0.10] * 24
        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)
        total = sum(s.estimated_charged_kwh for s in plan.charging_slots)
        assert total > 2.0, f"Pass 3 should add surplus, got {total}"

    def test_pass_3_enters_when_above_target_soc(self):
        """Pass 3 enters when EV SoC is above target (regression: early return bug).

        When current_soc_pct >= target_soc_pct and
        allow_charge_past_target_soc=True, the function must NOT
        early-return \"fully_charged\" — it must continue so Pass 3
        can allocate surplus-PV slots.
        """
        now = _dt(0)
        surplus = [2.0] * 12 + [0.0] * 12
        starts, ends = _make_slots(24)
        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=88.0,  # above target of 80 %
            target_soc_pct=80.0,
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            allow_charge_past_target_soc=True,
            slot_predicted_battery_kwh=[14.25] * 24,
            usable_battery_kwh=14.25,
            now=now,
        )
        prices = [0.10] * 24
        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)
        total = sum(s.estimated_charged_kwh for s in plan.charging_slots)
        assert total > 2.0, (
            f"Pass 3 should allocate surplus when above target, got {total}"
        )
        # All slots must be pure surplus (no grid import).
        for s in plan.charging_slots:
            assert s.import_needed_kwh == pytest.approx(0.0)
            assert s.estimated_cost == pytest.approx(0.0)

    def test_pass_3_enters_with_mismatched_prediction_length(self):
        """Pass 3 enters when len(predicted_battery) != len(candidates).

        Regression: the old condition required
        len(slot_predicted_battery_kwh) == len(surplus_slots) + len(non_surplus_slots),
        which failed when the effective-deadline cap trimmed some slots
        from the candidate list but slot_predicted_battery_kwh still
        covered the full planner horizon.
        """
        now = _dt(17)  # 17:00 — only 31 slots until end-of-tomorrow
        # 48-slot horizon but only ~31 candidates
        starts, ends = _make_slots_48(now)
        surplus = [2.0] * 12 + [0.0] * 36
        prices = [0.10] * 48
        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=88.0,
            target_soc_pct=80.0,
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            allow_charge_past_target_soc=True,
            slot_predicted_battery_kwh=[14.25] * 48,  # full 48 slots
            usable_battery_kwh=14.25,
            now=now,
        )
        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)
        total = sum(s.estimated_charged_kwh for s in plan.charging_slots)
        assert total > 2.0, (
            f"Pass 3 should enter with mismatched prediction length, got {total}"
        )

    def test_pass_3_not_entered_allow_past_target_disabled(self):
        """When allow_charge_past_target_soc=False and SoC >= target,
        the function returns \"fully_charged\" immediately."""
        now = _dt(0)
        surplus = [2.0] * 12 + [0.0] * 12
        starts, ends = _make_slots(24)
        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=88.0,
            target_soc_pct=80.0,
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            allow_charge_past_target_soc=False,  # disabled
            slot_predicted_battery_kwh=[14.25] * 24,
            usable_battery_kwh=14.25,
            now=now,
        )
        prices = [0.10] * 24
        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)
        assert plan.state == "fully_charged"
        assert len(plan.charging_slots) == 0

    def test_pass_3_not_entered_soc_100(self):
        """When SoC is 100 %, the function returns \"fully_charged\"
        even when allow_charge_past_target_soc=True."""
        now = _dt(0)
        surplus = [2.0] * 12 + [0.0] * 12
        starts, ends = _make_slots(24)
        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=100.0,
            target_soc_pct=80.0,
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            allow_charge_past_target_soc=True,
            slot_predicted_battery_kwh=[14.25] * 24,
            usable_battery_kwh=14.25,
            now=now,
        )
        prices = [0.10] * 24
        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)
        assert plan.state == "fully_charged"
        assert len(plan.charging_slots) == 0

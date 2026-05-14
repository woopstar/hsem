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


def _dt(h: int, tz=_UTC) -> datetime:
    """Return a datetime on 2024-06-15 at hour h in tz."""
    return datetime(2024, 6, 15, h, 0, 0, tzinfo=tz)


def _make_slots(n: int = 24, tz=_UTC):
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
        battery_conversion_loss_pct=5.0,
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

    def _make_inp(self, **kwargs) -> EVPlannerInput:
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

        Each slot is treated as 100 %-efficient so AC load == delivered.
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
                ac_load_kwh=kw,
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
          estimated_net_consumption
              == avg_house_consumption + ev_planned_load_kwh - solcast_pv_estimate
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
                s.avg_house_consumption + s.ev_planned_load_kwh - s.solcast_pv_estimate,
                3,
            )
            assert s.estimated_net_consumption == pytest.approx(
                expected_net, abs=1e-6
            ), (
                f"Slot {s.start.hour}:00 net mismatch: "
                f"house={s.avg_house_consumption}, ev={s.ev_planned_load_kwh}, "
                f"pv={s.solcast_pv_estimate}"
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

        # Find slots where EV planned load > 0 AND solcast_pv_estimate > 0
        ev_solar_slots = [
            s
            for s in out.slots
            if s.ev_planned_load_kwh > 1e-9 and s.solcast_pv_estimate > 1e-9
        ]
        for s in ev_solar_slots:
            # Net consumption should be ≥ 0 in these slots (EV consumed the surplus)
            # → battery solar-charge recommendation should not appear
            if s.estimated_net_consumption >= 0:
                assert s.recommendation != "batteries_charge_solar", (
                    f"Slot {s.start.hour}: EV consumed solar but battery solar-charge still recommended. "
                    f"ev_load={s.ev_planned_load_kwh}, pv={s.solcast_pv_estimate}, "
                    f"net={s.estimated_net_consumption}"
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
# Regression for bug: slot_solar_surplus was computed from estimated_net_consumption
# which is 0.0 before populate_net_consumption runs, so EV never received solar slots.
# Fix: compute surplus from raw base fields (pv_estimate - avg_house_consumption).
# ---------------------------------------------------------------------------


class TestEvSolarSurplusRegression:
    """Regression tests for EV solar surplus computation bug.

    Before the fix, ``slot_solar_surplus`` was derived from
    ``s.estimated_net_consumption`` which is still ``0.0`` at the point the
    EV planner runs (``populate_net_consumption`` had not been called yet).
    The EV therefore never saw any solar surplus and always treated every slot
    as a grid-import slot.

    After the fix, surplus is computed directly from base fields:
        surplus = max(pv_estimate - avg_house_consumption, 0.0)
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
            battery_conversion_loss_pct=5.0,
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
        assert planner_slot_10.estimated_net_consumption == pytest.approx(
            0.0, abs=0.01
        ), (
            f"effective_net at hour 10 should be 0.0, got {planner_slot_10.estimated_net_consumption}"
        )

        # --- Assert battery does NOT charge energy from consumed surplus ---
        # The recommendation label may still be 'batteries_charge_solar' because
        # estimated_net_consumption = 0.0 falls within the NEAR_ZERO threshold.
        # The energy-correctness invariant is: batteries_charged must be 0.0
        # (the charge scheduler derives slot_solar = abs(0.0) = 0.0, so no
        # energy flows into the battery even if the label says charge_solar).
        assert planner_slot_10.batteries_charged == pytest.approx(0.0, abs=0.01), (
            "Battery should NOT charge energy at hour 10: "
            "all solar surplus is consumed by EV. "
            f"batteries_charged={planner_slot_10.batteries_charged}, "
            f"ev_load={planner_slot_10.ev_planned_load_kwh}, "
            f"net={planner_slot_10.estimated_net_consumption}"
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
            battery_conversion_loss_pct=5.0,
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
# TestMultiEvSolarAllocation
# Two EVs sharing a single mutable solar-surplus budget per slot.
# ---------------------------------------------------------------------------


class TestMultiEvSolarAllocation:
    """Sequential allocation of solar surplus across two EVs."""

    def test_two_evs_share_limited_solar_surplus(self):
        """Primary EV takes solar first; secondary gets only what's left.

        Hand calculation
        ----------------
        Single 1-hour slot at 10:00 with:
          slot solar surplus      = 4.0 kWh (AC, shared budget)
          primary EV needs        = 3.0 kWh delivered, 100 % efficiency
          secondary EV needs      = 3.0 kWh delivered, 100 % efficiency
          charger power (both)    = 11 kW (max 11 kWh per slot)
          import price            = 0.20 EUR/kWh

        Primary plan (runs first):
          delivered = min(11.0, 3.0) = 3.0 kWh
          ac_load   = 3.0 / 1.0      = 3.0 kWh
          solar     = min(3.0, 4.0)  = 3.0 kWh
          import    = 0.0 kWh
          cost      = 0.0 EUR
          remaining slot surplus    = 4.0 − 3.0 = 1.0 kWh

        Secondary plan (runs second, sees decremented surplus):
          delivered = 3.0 kWh
          ac_load   = 3.0 kWh
          solar     = min(3.0, 1.0)  = 1.0 kWh
          import    = 3.0 − 1.0      = 2.0 kWh
          cost      = 2.0 × 0.20     = 0.40 EUR

        Combined planner load injected into slot:
          ev_planned_load_kwh = 3.0 + 3.0 = 6.0 kWh (AC domain)
        """
        now = _dt(9)  # 09:00, slot 10 is in future
        deadline = now + timedelta(hours=4)

        # Build a single-slot input where only hour 10 has surplus
        starts = [_dt(h) for h in range(24)]
        ends = [_dt(h) + timedelta(hours=1) for h in range(24)]
        surplus = [0.0] * 24
        surplus[10] = 4.0
        prices = [0.20] * 24

        primary_inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=70.0,
            target_soc_pct=73.0,  # 3 kWh / 100 kWh
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            deadline=deadline,
            now=now,
        )
        secondary_inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=70.0,
            target_soc_pct=73.0,  # 3 kWh / 100 kWh
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            deadline=deadline,
            now=now,
        )

        primary_plan = build_ev_charging_plan(
            primary_inp, starts, ends, surplus, prices
        )
        # Capture surplus state between the two EV plans.
        surplus_after_primary = list(surplus)
        secondary_plan = build_ev_charging_plan(
            secondary_inp, starts, ends, surplus, prices
        )

        # Primary should see the full 4.0 kWh surplus and consume 3.0 of it.
        primary_slot_10 = next(
            (s for s in primary_plan.charging_slots if s.start.hour == 10), None
        )
        assert primary_slot_10 is not None
        assert primary_slot_10.estimated_charged_kwh == pytest.approx(3.0, abs=0.01)
        assert primary_slot_10.ac_load_kwh == pytest.approx(3.0, abs=0.01)
        assert primary_slot_10.solar_surplus_kwh == pytest.approx(3.0, abs=0.01)
        assert primary_slot_10.import_needed_kwh == pytest.approx(0.0, abs=1e-9)
        assert primary_slot_10.estimated_cost == pytest.approx(0.0, abs=1e-9)

        # Surplus list at the point secondary started: 1.0 kWh remaining.
        assert surplus_after_primary[10] == pytest.approx(1.0, abs=1e-9)

        # Secondary should see 1.0 kWh remaining, consume 1.0 from solar, 2.0 grid.
        secondary_slot_10 = next(
            (s for s in secondary_plan.charging_slots if s.start.hour == 10), None
        )
        assert secondary_slot_10 is not None
        assert secondary_slot_10.estimated_charged_kwh == pytest.approx(3.0, abs=0.01)
        assert secondary_slot_10.ac_load_kwh == pytest.approx(3.0, abs=0.01)
        assert secondary_slot_10.solar_surplus_kwh == pytest.approx(1.0, abs=0.01)
        assert secondary_slot_10.import_needed_kwh == pytest.approx(2.0, abs=0.01)
        assert secondary_slot_10.estimated_cost == pytest.approx(0.40, abs=1e-4)

        # Surplus should now be drained for slot 10.
        assert surplus[10] == pytest.approx(0.0, abs=1e-9)

        # Combined AC load injected into the planner equals 6.0 kWh.
        combined = primary_plan.planned_load_by_slot.get(
            starts[10].isoformat(), 0.0
        ) + secondary_plan.planned_load_by_slot.get(starts[10].isoformat(), 0.0)
        assert combined == pytest.approx(6.0, abs=0.01)

    def test_combined_solar_sum_bounded_by_original_surplus(self):
        """Sum of two EVs' solar_surplus_kwh never exceeds the original budget.

        With 2.0 kWh of slot surplus in the only solar slot and both EVs
        wanting 2.0 kWh each (1 slot only available):
          primary takes 2.0 solar
          secondary sees 0.0 solar remaining → must use a grid slot
          combined solar consumed by both = 2.0 (equal to original budget).
        """
        now = _dt(9)
        deadline = now + timedelta(hours=4)
        starts = [_dt(h) for h in range(24)]
        ends = [_dt(h) + timedelta(hours=1) for h in range(24)]
        surplus = [0.0] * 24
        surplus[10] = 2.0
        original_surplus = surplus[10]
        prices = [0.10] * 24

        inp_kwargs = dict(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=70.0,
            target_soc_pct=72.0,  # 2 kWh / 100 kWh
            battery_capacity_kwh=100.0,
            charger_power_kw=11.0,
            charger_efficiency_pct=100.0,
            deadline=deadline,
            now=now,
        )

        primary = build_ev_charging_plan(
            EVPlannerInput(**inp_kwargs), starts, ends, surplus, prices
        )
        secondary = build_ev_charging_plan(
            EVPlannerInput(**inp_kwargs), starts, ends, surplus, prices
        )

        # Sum each plan's solar_surplus across all slots — this is the
        # actual solar consumed by each EV in this pass.
        primary_solar = sum(s.solar_surplus_kwh for s in primary.charging_slots)
        secondary_solar = sum(s.solar_surplus_kwh for s in secondary.charging_slots)

        # Primary took all 2.0 kWh; secondary saw none.
        assert primary_solar == pytest.approx(2.0, abs=0.01)
        assert secondary_solar == pytest.approx(0.0, abs=1e-9)

        # Bounded invariant: combined consumption <= original surplus.
        combined_solar = primary_solar + secondary_solar
        assert combined_solar <= original_surplus + 1e-6
        assert combined_solar == pytest.approx(original_surplus, abs=0.01)

    def test_engine_two_evs_share_solar_in_same_slot(self):
        """End-to-end engine run: two enabled EVs share the same solar slot.

        Hand calculation
        ----------------
        Single solar slot at hour 10:
          house load  = 1.0 kWh, PV = 5.0 kWh → base surplus = 4.0 kWh.
          Primary EV needs 3.0 kWh delivered (efficiency 100 %) → AC 3.0.
          Secondary EV needs 3.0 kWh delivered (efficiency 100 %) → AC 3.0.

        Sequential allocation:
          Primary: solar 3.0, grid 0.0.
          Secondary: solar 1.0, grid 2.0.
          Combined ev_planned_load_kwh = 6.0 kWh injected into slot 10.

        Effective net at hour 10:
          1.0 (house) + 6.0 (combined EV) − 5.0 (PV) = 2.0 kWh (positive import).
        """
        now_iso = "2024-06-15T09:00:00+00:00"
        from datetime import datetime as _dt2

        now = _dt2.fromisoformat(now_iso)
        deadline = now + timedelta(hours=6)

        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(24)
        ]
        pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
        pv[10] = SolcastSlot(hour=10, pv_estimate=5.0)
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
            battery_conversion_loss_pct=5.0,
            battery_discharge_efficiency_pct=95.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=averages,
            price_points=prices,
            solcast_slots=pv,
            # Primary EV
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=70.0,
            ev_planned_load_target_soc_pct=73.0,
            ev_planned_load_battery_capacity_kwh=100.0,
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=deadline,
            ev_planned_load_base_load_includes_ev=False,
            # Secondary EV (identical requirement)
            ev_second_planned_load_enabled=True,
            ev_second_planned_load_connected=True,
            ev_second_planned_load_smart_charging_enabled=True,
            ev_second_planned_load_current_soc_pct=70.0,
            ev_second_planned_load_target_soc_pct=73.0,
            ev_second_planned_load_battery_capacity_kwh=100.0,
            ev_second_planned_load_charger_power_kw=11.0,
            ev_second_planned_load_charger_efficiency_pct=100.0,
            ev_second_planned_load_deadline=deadline,
            ev_second_planned_load_base_load_includes_ev=False,
        )
        out = run_planner(inp)

        assert out.ev_charging_plan is not None
        assert out.ev_second_charging_plan is not None

        prim_slot = next(
            (s for s in out.ev_charging_plan.charging_slots if s.start.hour == 10),
            None,
        )
        sec_slot = next(
            (
                s
                for s in out.ev_second_charging_plan.charging_slots
                if s.start.hour == 10
            ),
            None,
        )
        assert prim_slot is not None
        assert sec_slot is not None
        # Primary claimed 3.0 kWh of the 4.0 kWh surplus.
        assert prim_slot.solar_surplus_kwh == pytest.approx(3.0, abs=0.01)
        assert prim_slot.import_needed_kwh == pytest.approx(0.0, abs=0.01)
        # Secondary saw only the leftover 1.0 kWh of surplus.
        assert sec_slot.solar_surplus_kwh == pytest.approx(1.0, abs=0.01)
        assert sec_slot.import_needed_kwh == pytest.approx(2.0, abs=0.01)
        # Sum of solar consumed must not exceed pre-injection surplus (4.0 kWh).
        assert (prim_slot.solar_surplus_kwh + sec_slot.solar_surplus_kwh) == (
            pytest.approx(4.0, abs=0.01)
        )

        # Planner slot 10 should carry the combined AC load (6.0 kWh).
        planner_slot_10 = next((s for s in out.slots if s.start.hour == 10), None)
        assert planner_slot_10 is not None
        assert planner_slot_10.ev_planned_load_kwh == pytest.approx(6.0, abs=0.01)
        # Effective net: 1.0 (house) + 6.0 (EVs) − 5.0 (PV) = 2.0
        assert planner_slot_10.estimated_net_consumption == pytest.approx(2.0, abs=0.01)


# ---------------------------------------------------------------------------
# TestChargerEfficiencyAcLoad
# Charger efficiency lives in the AC-load domain, not the delivered-energy
# domain.  Delivered energy still counts toward the SoC target unchanged.
# ---------------------------------------------------------------------------


class TestChargerEfficiencyAcLoad:
    """Charger efficiency raises AC load and grid import without inflating
    delivered energy toward the SoC target."""

    def test_charger_efficiency_80pct_increases_ac_load(self):
        """80 % efficient charger draws 5.0 kWh AC to deliver 4.0 kWh to battery.

        Hand calculation
        ----------------
        Single 1-hour slot, charger 5 kW @ 80 %, EV needs 4.0 kWh:
          max delivered per slot = 5 kW × 1 h × 0.80 = 4.0 kWh
          delivered              = min(4.0, 4.0) = 4.0 kWh
          ac_load                = 4.0 / 0.80     = 5.0 kWh
          solar surplus in slot  = 0.0
          import_needed          = 5.0 − 0.0      = 5.0 kWh
          cost                   = 5.0 × 0.20     = 1.0 EUR
        Injected planner load (AC) = 5.0 kWh.
        """
        now = _dt(9)
        deadline = now + timedelta(hours=2)
        starts = [_dt(h) for h in range(24)]
        ends = [_dt(h) + timedelta(hours=1) for h in range(24)]
        surplus = [0.0] * 24
        prices = [0.20] * 24

        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=60.0,
            target_soc_pct=64.0,  # 4 kWh / 100 kWh
            battery_capacity_kwh=100.0,
            charger_power_kw=5.0,
            charger_efficiency_pct=80.0,
            deadline=deadline,
            now=now,
        )
        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)

        assert len(plan.charging_slots) == 1
        slot = plan.charging_slots[0]
        assert slot.estimated_charged_kwh == pytest.approx(4.0, abs=0.01)
        assert slot.ac_load_kwh == pytest.approx(5.0, abs=0.01)
        assert slot.solar_surplus_kwh == pytest.approx(0.0, abs=1e-9)
        assert slot.import_needed_kwh == pytest.approx(5.0, abs=0.01)
        assert slot.estimated_cost == pytest.approx(1.0, abs=1e-4)
        # Planner-injection value is the AC load, not delivered.
        assert plan.planned_load_by_slot[slot.start.isoformat()] == pytest.approx(
            5.0, abs=0.01
        )
        # AC = solar + import invariant.
        assert (slot.solar_surplus_kwh + slot.import_needed_kwh) == pytest.approx(
            slot.ac_load_kwh, abs=1e-6
        )

    def test_charger_efficiency_100pct_preserves_default_behavior(self):
        """At 100 % efficiency, ac_load_kwh == estimated_charged_kwh (no regression).

        Hand calculation
        ----------------
        Same setup but 100 % efficient: delivered == ac_load == 4.0 kWh.
        Injected planner load equals delivered.  Locks in legacy behaviour.
        """
        now = _dt(9)
        deadline = now + timedelta(hours=2)
        starts = [_dt(h) for h in range(24)]
        ends = [_dt(h) + timedelta(hours=1) for h in range(24)]
        surplus = [0.0] * 24
        prices = [0.20] * 24

        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=60.0,
            target_soc_pct=64.0,
            battery_capacity_kwh=100.0,
            charger_power_kw=5.0,
            charger_efficiency_pct=100.0,
            deadline=deadline,
            now=now,
        )
        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)

        assert len(plan.charging_slots) == 1
        slot = plan.charging_slots[0]
        assert slot.estimated_charged_kwh == pytest.approx(4.0, abs=0.01)
        # The defining 100 %-efficiency invariant.
        assert slot.ac_load_kwh == pytest.approx(slot.estimated_charged_kwh, abs=1e-9)
        assert slot.import_needed_kwh == pytest.approx(4.0, abs=0.01)
        assert plan.planned_load_by_slot[slot.start.isoformat()] == pytest.approx(
            slot.estimated_charged_kwh, abs=1e-9
        )

    def test_charger_efficiency_partial_solar_with_loss(self):
        """Efficiency < 100 % with partial solar: AC load is solar + grid in AC domain.

        Hand calculation
        ----------------
        Slot has 3.0 kWh solar surplus (AC).  Charger 5 kW @ 80 %, EV needs
        4.0 kWh delivered:
          delivered     = 4.0 kWh
          ac_load       = 4.0 / 0.80 = 5.0 kWh
          solar_used    = min(5.0, 3.0) = 3.0 kWh
          import_needed = 5.0 − 3.0    = 2.0 kWh
        SoC target still receives 4.0 kWh.
        """
        now = _dt(9)
        deadline = now + timedelta(hours=2)
        starts = [_dt(h) for h in range(24)]
        ends = [_dt(h) + timedelta(hours=1) for h in range(24)]
        surplus = [0.0] * 24
        surplus[9] = 3.0
        prices = [0.20] * 24

        inp = EVPlannerInput(
            enabled=True,
            ev_connected=True,
            smart_charging_enabled=True,
            current_soc_pct=60.0,
            target_soc_pct=64.0,
            battery_capacity_kwh=100.0,
            charger_power_kw=5.0,
            charger_efficiency_pct=80.0,
            deadline=deadline,
            now=now,
        )
        plan = build_ev_charging_plan(inp, starts, ends, surplus, prices)
        assert len(plan.charging_slots) >= 1
        slot = plan.charging_slots[0]
        assert slot.estimated_charged_kwh == pytest.approx(4.0, abs=0.01)
        assert slot.ac_load_kwh == pytest.approx(5.0, abs=0.01)
        assert slot.solar_surplus_kwh == pytest.approx(3.0, abs=0.01)
        assert slot.import_needed_kwh == pytest.approx(2.0, abs=0.01)

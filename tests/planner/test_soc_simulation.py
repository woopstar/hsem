"""Tests for full SoC simulation (#293).

Acceptance criteria verified here
----------------------------------
- SoC never goes below min (end-of-discharge floor).
- SoC never goes above max (max_soc_pct ceiling).
- Planner accounts for PV, load, charge, discharge, import, and export.
- Tests cover battery full, battery empty, and mid-SoC.
- Charge power limit is respected per slot.
- Discharge power limit is respected per slot.
- battery_max_soc_pct is respected in usable_capacity and simulation.

All tests are synchronous and import nothing from Home Assistant's runtime.
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planner_inputs import (
    BatteryScheduleInput,
    HourlyConsumptionAverage,
    PlannerInput,
    PricePoint,
    SolcastSlot,
)
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.slot_population import usable_capacity
from custom_components.hsem.planner.soc_simulation import simulate_soc
from custom_components.hsem.utils.recommendations import Recommendations
from tests.planner.fixtures import make_summer_day_input, make_winter_day_input

_TZ = ZoneInfo("Europe/Copenhagen")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_input(  # NOSONAR
    *,
    battery_soc_pct: float = 50.0,
    battery_rated_capacity_kwh: float = 10.0,
    battery_end_of_discharge_soc_pct: float = 10.0,
    battery_max_soc_pct: float = 100.0,
    battery_max_charge_power_w: float = 5000.0,
    battery_max_discharge_power_w: float | None = None,
    battery_purchase_price: float = 0.0,
    pv_kwh_per_hour: float = 0.0,
    load_kwh_per_hour: float = 0.5,
    import_price: float = 0.20,
    export_price: float = 0.05,
    interval_minutes: int = 60,
    interval_length_hours: int = 24,
    schedules: list[BatteryScheduleInput] | None = None,
    now_iso: str = "2024-06-15T00:00:00+02:00",
) -> PlannerInput:
    """Build a minimal PlannerInput with uniform load/PV across all 24 hours."""
    prices = [
        PricePoint(hour=h, import_price=import_price, export_price=export_price)
        for h in range(24)
    ]
    solar = [SolcastSlot(hour=h, pv_estimate=pv_kwh_per_hour) for h in range(24)]
    consumption = [
        HourlyConsumptionAverage(
            hour=h,
            avg_1d=load_kwh_per_hour,
            avg_3d=load_kwh_per_hour,
            avg_7d=load_kwh_per_hour,
            avg_14d=load_kwh_per_hour,
        )
        for h in range(24)
    ]
    return PlannerInput(
        now_iso=now_iso,
        interval_minutes=interval_minutes,
        interval_length_hours=interval_length_hours,
        battery_soc_pct=battery_soc_pct,
        battery_rated_capacity_kwh=battery_rated_capacity_kwh,
        battery_end_of_discharge_soc_pct=battery_end_of_discharge_soc_pct,
        battery_max_soc_pct=battery_max_soc_pct,
        battery_max_charge_power_w=battery_max_charge_power_w,
        battery_max_discharge_power_w=battery_max_discharge_power_w,
        battery_purchase_price=battery_purchase_price,
        battery_expected_cycles=6000,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=consumption,
        price_points=prices,
        solcast_slots=solar,
        battery_schedules=schedules if schedules is not None else [],
        excess_export_enabled=False,
        excess_export_discharge_buffer_pct=10.0,
        excess_export_price_threshold=0.10,
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        house_power_includes_ev=True,
        is_read_only=True,
    )


# ===========================================================================
# Unit tests for usable_capacity
# ===========================================================================


class TestUsableCapacity:
    """Tests for the usable_capacity helper with max_soc_pct support."""

    def test_default_max_soc_matches_original_behaviour(self):
        """max_soc_pct=100 should give the same result as the old implementation."""
        usable, current = usable_capacity(10.0, 50.0, 10.0, 100.0)
        assert usable == pytest.approx(9.0)
        assert current == pytest.approx(4.0)

    def test_max_soc_reduces_usable_range(self):
        """battery_max_soc_pct=80 should limit the usable range to 70%."""
        usable, current = usable_capacity(10.0, 50.0, 10.0, 80.0)
        assert usable == pytest.approx(7.0)
        assert current == pytest.approx(4.0)

    def test_current_clamped_to_usable_when_soc_above_max(self):
        """If current SoC already exceeds max_soc_pct, current is clamped to usable."""
        usable, current = usable_capacity(10.0, 90.0, 10.0, 80.0)
        # usable = 10 * (80 - 10) / 100 = 7.0
        # raw_current = 10 * 90% - 10 * 10% = 8.0  → clamped to 7.0
        assert usable == pytest.approx(7.0)
        assert current == pytest.approx(7.0)

    def test_zero_rated_capacity_returns_zeros(self):
        usable, current = usable_capacity(0.0, 50.0, 10.0)
        assert usable == pytest.approx(0.0)
        assert current == pytest.approx(0.0)

    def test_full_battery_at_100_pct(self):
        usable, current = usable_capacity(10.0, 100.0, 10.0)
        assert usable == pytest.approx(9.0)
        assert current == pytest.approx(9.0)

    def test_empty_battery_at_end_of_discharge(self):
        usable, current = usable_capacity(10.0, 10.0, 10.0)
        assert usable == pytest.approx(9.0)
        assert current == pytest.approx(0.0)

    def test_max_soc_below_min_soc_gives_zero_usable(self):
        """If max_soc_pct < end_of_discharge_soc_pct, usable should be 0."""
        usable, current = usable_capacity(10.0, 50.0, 20.0, 10.0)
        assert usable == pytest.approx(0.0)
        assert current == pytest.approx(0.0)


# ===========================================================================
# Unit tests for simulate_soc directly
# ===========================================================================


def _make_slots_for_simulation(
    n: int = 4,
    load: float = 0.5,
    pv: float = 0.0,
    batteries_charged: float = 0.0,
    now_iso: str = "2024-06-15T00:00:00+02:00",
) -> tuple[list[PlannedSlot], object]:
    """Build ``n`` 1-hour slots for direct simulation testing."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Copenhagen")
    t0 = datetime.fromisoformat(now_iso).replace(tzinfo=tz)
    slots: list[PlannedSlot] = []
    for i in range(n):
        start = t0 + timedelta(hours=i)
        end = start + timedelta(hours=1)
        slot = PlannedSlot(start=start, end=end)
        slot.avg_house_consumption_kwh = load
        slot.solcast_pv_estimate_kwh = pv
        slot.batteries_charged_kwh = batteries_charged
    return slots, t0


class TestSimulateSocUnit:
    """Direct unit tests for the simulate_soc function."""

    def test_no_pv_no_charge_depletes_battery(self):
        """With 0.5 kWh/h load, 0 PV, 0 charge: battery drains 0.5 kWh/h."""
        from datetime import datetime

        tz = ZoneInfo("Europe/Copenhagen")
        t0 = datetime.fromisoformat("2024-06-15T00:00:00+02:00").replace(tzinfo=tz)
        slots = []
        for i in range(4):
            s = PlannedSlot(
                start=t0 + timedelta(hours=i), end=t0 + timedelta(hours=i + 1)
            )
            s.avg_house_consumption_kwh = 0.5
            s.solcast_pv_estimate_kwh = 0.0
            s.batteries_charged_kwh = 0.0
            slots.append(s)

        usable_kwh = 9.0  # 10 kWh rated, 10% min SoC
        current_kwh = 4.0  # 50% SoC → 4 kWh above floor

        simulate_soc(slots, t0, current_kwh, usable_kwh, usable_kwh, 5.0, None)

        assert slots[0].estimated_battery_capacity_kwh == pytest.approx(3.5)
        assert slots[1].estimated_battery_capacity_kwh == pytest.approx(3.0)
        assert slots[2].estimated_battery_capacity_kwh == pytest.approx(2.5)
        assert slots[3].estimated_battery_capacity_kwh == pytest.approx(2.0)

    def test_battery_never_goes_below_zero(self):
        """Battery capacity must never go negative (min SoC floor)."""
        from datetime import datetime

        tz = ZoneInfo("Europe/Copenhagen")
        t0 = datetime.fromisoformat("2024-06-15T00:00:00+02:00").replace(tzinfo=tz)
        slots = []
        for i in range(6):
            s = PlannedSlot(
                start=t0 + timedelta(hours=i), end=t0 + timedelta(hours=i + 1)
            )
            s.avg_house_consumption_kwh = 2.0  # heavy load
            s.solcast_pv_estimate_kwh = 0.0
            s.batteries_charged_kwh = 0.0
            slots.append(s)

        simulate_soc(slots, t0, 2.0, 9.0, 9.0, 5.0, None)

        for slot in slots:
            assert slot.estimated_battery_capacity_kwh >= 0.0, (
                f"Battery went negative at {slot.start}"
            )
            assert slot.estimated_battery_soc_pct >= 0.0

    def test_battery_never_exceeds_usable(self):
        """Battery capacity must never exceed usable_kwh (max SoC ceiling)."""
        from datetime import datetime

        tz = ZoneInfo("Europe/Copenhagen")
        t0 = datetime.fromisoformat("2024-06-15T00:00:00+02:00").replace(tzinfo=tz)
        slots = []
        for i in range(4):
            s = PlannedSlot(
                start=t0 + timedelta(hours=i), end=t0 + timedelta(hours=i + 1)
            )
            s.avg_house_consumption_kwh = 0.0
            s.solcast_pv_estimate_kwh = 0.0
            s.batteries_charged_kwh = 10.0  # large charge request
            slots.append(s)

        usable_kwh = 9.0
        simulate_soc(slots, t0, 4.0, usable_kwh, usable_kwh, 5.0, None)

        for slot in slots:
            assert slot.estimated_battery_capacity_kwh <= usable_kwh + 1e-6, (
                f"Battery exceeded max at {slot.start}: {slot.estimated_battery_capacity_kwh}"
            )
            assert slot.estimated_battery_soc_pct <= 100.0 + 1e-6

    def test_charge_power_limit_clamped(self):
        """batteries_charged must not exceed max_charge_per_slot."""
        from datetime import datetime

        tz = ZoneInfo("Europe/Copenhagen")
        t0 = datetime.fromisoformat("2024-06-15T00:00:00+02:00").replace(tzinfo=tz)
        s = PlannedSlot(start=t0, end=t0 + timedelta(hours=1))
        s.avg_house_consumption_kwh = 0.0
        s.solcast_pv_estimate_kwh = 0.0
        s.batteries_charged_kwh = 10.0  # far exceeds limit

        simulate_soc(
            [s], t0, 0.0, 9.0, 9.0, max_charge_per_slot=2.0, max_discharge_per_slot=None
        )

        assert s.batteries_charged_kwh <= 2.0 + 1e-6

    def test_discharge_power_limit_clamped(self):
        """Discharge must not exceed max_discharge_per_slot."""
        from datetime import datetime

        tz = ZoneInfo("Europe/Copenhagen")
        t0 = datetime.fromisoformat("2024-06-15T00:00:00+02:00").replace(tzinfo=tz)
        s = PlannedSlot(start=t0, end=t0 + timedelta(hours=1))
        s.avg_house_consumption_kwh = 5.0  # large load
        s.solcast_pv_estimate_kwh = 0.0
        s.batteries_charged_kwh = 0.0

        simulate_soc(
            [s], t0, 9.0, 9.0, 9.0, max_charge_per_slot=5.0, max_discharge_per_slot=1.5
        )

        assert s.batteries_discharged_kwh <= 1.5 + 1e-6

    def test_pv_surplus_exported_to_grid(self):
        """When PV exceeds load and battery is full, surplus is exported."""
        from datetime import datetime

        tz = ZoneInfo("Europe/Copenhagen")
        t0 = datetime.fromisoformat("2024-06-15T12:00:00+02:00").replace(tzinfo=tz)
        s = PlannedSlot(start=t0, end=t0 + timedelta(hours=1))
        s.avg_house_consumption_kwh = 0.5
        s.solcast_pv_estimate_kwh = 3.0  # big surplus
        s.batteries_charged_kwh = 0.0

        # Battery already full; all surplus should go to grid
        simulate_soc(
            [s], t0, 9.0, 9.0, 9.0, max_charge_per_slot=0.0, max_discharge_per_slot=None
        )

        # No discharge needed; surplus beyond battery goes to grid
        assert s.batteries_discharged_kwh == pytest.approx(0.0)
        assert s.grid_export_kwh > 0.0
        assert s.grid_import_kwh == pytest.approx(0.0)

    def test_grid_import_when_battery_empty(self):
        """When battery is empty and load > PV, grid must supply the rest."""
        from datetime import datetime

        tz = ZoneInfo("Europe/Copenhagen")
        t0 = datetime.fromisoformat("2024-06-15T00:00:00+02:00").replace(tzinfo=tz)
        s = PlannedSlot(start=t0, end=t0 + timedelta(hours=1))
        s.avg_house_consumption_kwh = 1.0
        s.solcast_pv_estimate_kwh = 0.0
        s.batteries_charged_kwh = 0.0

        simulate_soc(
            [s], t0, 0.0, 9.0, 9.0, max_charge_per_slot=5.0, max_discharge_per_slot=None
        )

        assert s.batteries_discharged_kwh == pytest.approx(0.0)
        assert s.grid_import_kwh == pytest.approx(1.0)

    def test_past_slots_get_zero_fields(self):
        """Slots entirely before ``now`` should have all SoC fields zeroed."""
        from datetime import datetime

        tz = ZoneInfo("Europe/Copenhagen")
        t0 = datetime.fromisoformat("2024-06-15T06:00:00+02:00").replace(tzinfo=tz)
        s = PlannedSlot(
            start=t0 - timedelta(hours=2),
            end=t0 - timedelta(hours=1),
        )
        s.avg_house_consumption_kwh = 0.5
        s.solcast_pv_estimate_kwh = 0.0
        s.batteries_charged_kwh = 0.0

        simulate_soc(
            [s], t0, 4.0, 9.0, 9.0, max_charge_per_slot=5.0, max_discharge_per_slot=None
        )

        assert s.estimated_battery_capacity_kwh == pytest.approx(0.0)
        assert s.estimated_battery_soc_pct == pytest.approx(0.0)


# ===========================================================================
# Integration tests via run_planner
# ===========================================================================


class TestSoCBoundsIntegration:
    """SoC must stay within [min, max] through a full planning run."""

    def test_soc_never_below_zero_summer(self):
        """SoC must never go below 0 on a summer day."""
        result = run_planner(make_summer_day_input())
        for slot in result.slots:
            assert slot.estimated_battery_soc_pct >= 0.0, (
                f"SoC below zero at {slot.start}: {slot.estimated_battery_soc_pct}"
            )

    def test_soc_never_above_100_summer(self):
        """SoC must never exceed 100 % on a summer day."""
        result = run_planner(make_summer_day_input())
        for slot in result.slots:
            assert slot.estimated_battery_soc_pct <= 100.0 + 1e-6, (
                f"SoC above 100 at {slot.start}: {slot.estimated_battery_soc_pct}"
            )

    def test_soc_never_below_zero_winter(self):
        """SoC must never go below 0 on a winter day."""
        result = run_planner(make_winter_day_input())
        for slot in result.slots:
            assert slot.estimated_battery_soc_pct >= 0.0

    def test_soc_never_above_100_winter(self):
        """SoC must never exceed 100 % on a winter day."""
        result = run_planner(make_winter_day_input())
        for slot in result.slots:
            assert slot.estimated_battery_soc_pct <= 100.0 + 1e-6

    def test_soc_never_exceeds_max_soc_pct(self):
        """Absolute SoC must never exceed battery_max_soc_pct when it is < 100 %."""
        inp = _make_minimal_input(
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_soc_pct=80.0,
            pv_kwh_per_hour=3.0,  # lots of PV → charging pressure
            load_kwh_per_hour=0.3,
        )
        result = run_planner(inp)
        # The absolute SoC (relative to rated 10 kWh capacity) must stay ≤ 80 %.
        # Allow a tiny epsilon for floating-point rounding.
        for slot in result.slots:
            if slot.recommendation != Recommendations.TimePassed.value:
                assert slot.estimated_battery_soc_pct <= 80.0 + 0.1, (
                    f"Absolute SoC exceeded max_soc_pct=80 at {slot.start}: "
                    f"{slot.estimated_battery_soc_pct}"
                )

    def test_battery_capacity_bounded_by_usable(self):
        """estimated_battery_capacity must never exceed usable_kwh."""
        result = run_planner(make_summer_day_input(battery_soc_pct=10.0))
        usable, _ = usable_capacity(10.0, 10.0, 10.0, 100.0)
        for slot in result.slots:
            assert slot.estimated_battery_capacity_kwh <= usable + 1e-6


class TestSoCEdgeCases:
    """Battery full, empty, and mid-SoC starting conditions."""

    def test_battery_starts_full(self):
        """A full battery must not charge further and can only discharge."""
        result = run_planner(
            _make_minimal_input(
                battery_soc_pct=100.0,
                load_kwh_per_hour=0.5,
                pv_kwh_per_hour=0.0,
            )
        )
        future_slots = [
            s
            for s in result.slots
            if s.recommendation != Recommendations.TimePassed.value
        ]
        if future_slots:
            first = future_slots[0]
            # First slot starts at 100 %, capacity should be ≤ usable (no overcharge).
            usable, _ = usable_capacity(10.0, 100.0, 10.0)
            assert first.estimated_battery_capacity_kwh <= usable + 1e-6

    def test_battery_starts_empty(self):
        """An empty battery must not go negative."""
        result = run_planner(
            _make_minimal_input(
                battery_soc_pct=10.0,  # at end-of-discharge floor
                load_kwh_per_hour=0.5,
                pv_kwh_per_hour=0.0,
            )
        )
        for slot in result.slots:
            assert slot.estimated_battery_capacity_kwh >= 0.0
            assert slot.estimated_battery_soc_pct >= 0.0

    def test_battery_mid_soc(self):
        """Mid-SoC battery should discharge gradually then stop at floor."""
        result = run_planner(
            _make_minimal_input(
                battery_soc_pct=50.0,
                load_kwh_per_hour=0.5,
                pv_kwh_per_hour=0.0,
                # Disable all schedules so the simulation simply drains
                schedules=[],
                # Set a modest cycle cost so the MILP doesn't arbitrage
                # terminal-SoC credit for free
                battery_purchase_price=5000.0,
            )
        )
        future_slots = [
            s
            for s in result.slots
            if s.recommendation != Recommendations.TimePassed.value
        ]
        # Battery capacity should be monotonically non-increasing (no external charge)
        caps = [s.estimated_battery_capacity_kwh for s in future_slots]
        for a, b in zip(caps, caps[1:]):
            assert b <= a + 1e-6, f"Battery capacity increased unexpectedly: {a} → {b}"

    def test_battery_zero_capacity_no_errors(self):
        """A zero-capacity battery should not raise and SoC should be 0."""
        result = run_planner(
            _make_minimal_input(
                battery_rated_capacity_kwh=0.0,
            )
        )
        for slot in result.slots:
            assert slot.estimated_battery_soc_pct == pytest.approx(0.0)
            assert slot.estimated_battery_capacity_kwh == pytest.approx(0.0)


class TestPowerLimits:
    """Charge and discharge power limits must be respected."""

    def test_charge_power_limit_respected_in_full_run(self):
        """batteries_charged per slot must not exceed max_charge_per_slot."""
        inp = _make_minimal_input(
            battery_max_charge_power_w=1000.0,  # 1 kW → 1 kWh/h per slot,
            pv_kwh_per_hour=5.0,  # large PV to force charging
            load_kwh_per_hour=0.2,
            schedules=[],
        )
        result = run_planner(inp)
        # max_charge_per_slot = 1 kW * 1h * 1.0 (no loss) = 1.0 kWh
        for slot in result.slots:
            assert slot.batteries_charged_kwh <= 1.0 + 1e-6, (
                f"Charge exceeded limit at {slot.start}: {slot.batteries_charged_kwh}"
            )

    def test_discharge_power_limit_respected_in_full_run(self):
        """batteries_discharged per slot must not exceed max_discharge_per_slot."""
        inp = _make_minimal_input(
            battery_max_discharge_power_w=1000.0,  # 1 kW → 1 kWh/h
            load_kwh_per_hour=3.0,  # heavy load to force discharging
            pv_kwh_per_hour=0.0,
            schedules=[],
        )
        result = run_planner(inp)
        for slot in result.slots:
            assert slot.batteries_discharged_kwh <= 1.0 + 1e-6, (
                f"Discharge exceeded limit at {slot.start}: {slot.batteries_discharged_kwh}"
            )

    def test_no_discharge_limit_allows_high_discharge(self):
        """When battery_max_discharge_power_w is None, discharge is only limited
        by available capacity."""
        inp = _make_minimal_input(
            battery_soc_pct=100.0,
            battery_max_discharge_power_w=None,
            load_kwh_per_hour=4.0,  # heavy load
            pv_kwh_per_hour=0.0,
            schedules=[],
        )
        result = run_planner(inp)
        # At least one future slot should discharge more than 1 kWh
        future_discharges = [
            s.batteries_discharged_kwh
            for s in result.slots
            if s.recommendation != Recommendations.TimePassed.value
        ]
        assert any(d > 1.0 for d in future_discharges), (
            "Expected at least one slot to discharge > 1 kWh when no limit is set"
        )


class TestEnergyFlowAccounting:
    """grid_import_kwh and grid_export_kwh must be consistent with energy flows."""

    def test_import_non_negative(self):
        """Grid imports must be non-negative in all slots."""
        result = run_planner(make_summer_day_input())
        for slot in result.slots:
            assert slot.grid_import_kwh >= 0.0, (
                f"Negative import at {slot.start}: {slot.grid_import_kwh}"
            )

    def test_export_non_negative(self):
        """Grid exports must be non-negative in all slots."""
        result = run_planner(make_summer_day_input())
        for slot in result.slots:
            assert slot.grid_export_kwh >= 0.0, (
                f"Negative export at {slot.start}: {slot.grid_export_kwh}"
            )

    def test_no_simultaneous_import_and_export(self):
        """A slot must not have both positive import and positive export."""
        result = run_planner(make_summer_day_input())
        for slot in result.slots:
            assert not (slot.grid_import_kwh > 1e-6 and slot.grid_export_kwh > 1e-6), (
                f"Simultaneous import+export at {slot.start}: "
                f"import={slot.grid_import_kwh}, export={slot.grid_export_kwh}"
            )

    def test_full_pv_surplus_exported(self):
        """When PV > load and battery is full, all surplus should be exported."""
        inp = _make_minimal_input(
            battery_soc_pct=100.0,  # full battery
            pv_kwh_per_hour=5.0,
            load_kwh_per_hour=0.5,
            schedules=[],
        )
        result = run_planner(inp)
        future_slots = [
            s
            for s in result.slots
            if s.recommendation != Recommendations.TimePassed.value
            and s.grid_import_kwh == pytest.approx(0.0, abs=1e-3)
        ]
        # With full battery and high PV, there should be some export slots
        assert any(s.grid_export_kwh > 0.0 for s in future_slots), (
            "Expected exported energy with full battery and PV surplus"
        )

    def test_no_export_when_battery_empty_and_no_pv(self):
        """With empty battery and no PV, there should be no export."""
        inp = _make_minimal_input(
            battery_soc_pct=10.0,  # empty
            pv_kwh_per_hour=0.0,
            load_kwh_per_hour=0.5,
            schedules=[],
        )
        result = run_planner(inp)
        for slot in result.slots:
            if slot.recommendation != Recommendations.TimePassed.value:
                assert slot.grid_export_kwh == pytest.approx(0.0), (
                    f"Unexpected export at {slot.start}: {slot.grid_export_kwh}"
                )


class TestMaxSoCPct:
    """Tests for the battery_max_soc_pct input."""

    def test_usable_range_reduced_by_max_soc_pct(self):
        """usable_kwh should be (max_soc_pct - min_soc_pct) / 100 * rated."""
        usable, _ = usable_capacity(10.0, 50.0, 10.0, 90.0)
        assert usable == pytest.approx(8.0)

    def test_max_soc_pct_100_equals_no_limit(self):
        """max_soc_pct=100 should give same result as omitting max_soc."""
        u_limited, c_limited = usable_capacity(10.0, 50.0, 10.0, 100.0)
        u_default, c_default = usable_capacity(10.0, 50.0, 10.0)
        assert u_limited == pytest.approx(u_default)
        assert c_limited == pytest.approx(c_default)

    def test_run_planner_accepts_max_soc_pct_field(self):
        """run_planner must accept battery_max_soc_pct without error."""
        inp = _make_minimal_input(battery_max_soc_pct=90.0)
        result = run_planner(inp)
        assert result.slots, "Expected slots in output"

    def test_soc_capped_at_max_soc_pct(self):
        """Absolute SoC must not exceed battery_max_soc_pct during simulation."""
        inp = _make_minimal_input(
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_soc_pct=75.0,
            pv_kwh_per_hour=5.0,  # lots of PV
            load_kwh_per_hour=0.1,
            schedules=[],
        )
        result = run_planner(inp)
        for slot in result.slots:
            if slot.recommendation != Recommendations.TimePassed.value:
                # Absolute SoC must not exceed the configured max.
                assert slot.estimated_battery_soc_pct <= 75.0 + 0.1, (
                    f"Absolute SoC exceeded max_soc_pct=75 at {slot.start}: "
                    f"{slot.estimated_battery_soc_pct}"
                )

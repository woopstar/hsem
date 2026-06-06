"""Tests that solar charge energy is not double-counted across multiple slots.

Regression suite for the bug where ``apply_optimization_strategy`` stored the
running cumulative ``charged`` total in each slot's ``batteries_charged_kwh`` field
instead of the per-slot energy delta.  Summing those values in
``total_charged_energy_kwh()`` or ``engine._derive_windows()`` therefore
over-reported how much energy was charged.

Acceptance criteria
-------------------
1. Each BatteriesChargeSolar slot stores only the energy charged *in that slot*,
   not the running cumulative total.
2. Summing ``batteries_charged_kwh`` across all BatteriesChargeSolar slots equals
   ``total_charged_energy_kwh()`` (no duplication).
3. The sum never exceeds the battery's usable remaining capacity.
4. ``total_charged_energy_kwh()`` matches a manual slot-by-slot sum.
5. Multiple solar charge slots do not accumulate into a single incorrectly large
   value on any individual slot.
"""

from __future__ import annotations

from datetime import time

import pytest

from custom_components.hsem.models.battery_schedule_input import BatteryScheduleInput
from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SOLAR_CHARGE_VALUE = Recommendations.BatteriesChargeSolar.value


def _make_solar_only_input(
    *,
    battery_soc_pct: float = 0.0,
    battery_rated_capacity_kwh: float = 10.0,
    battery_end_of_discharge_soc_pct: float = 10.0,
    battery_max_charge_power_w: float = 5000.0,
    solar_per_hour: float = 3.0,
    consumption_per_hour: float = 0.3,
    now_iso: str = "2024-06-15T00:00:00+02:00",
    schedules: list[BatteryScheduleInput] | None = None,
) -> PlannerInput:
    """Return a 24-hour summer input where solar > consumption in most mid-day hours.

    All hours have a uniform price (no cheap-grid incentive), so the planner
    should rely solely on solar surplus for charging.

    Args:
        battery_soc_pct: Initial SoC (0-100 %).
        battery_rated_capacity_kwh: Nameplate capacity.
        battery_end_of_discharge_soc_pct: End-of-discharge reserve.
        battery_max_charge_power_w: Max charge power in Watts.
        solar_per_hour: PV production per hour in kWh.
        consumption_per_hour: House consumption per hour in kWh.
        now_iso: Planning timestamp (timezone-aware ISO-8601).
        schedules: Battery charge/discharge schedule overrides.
    """
    prices = [
        PricePoint(hour=h, import_price=0.20, export_price=0.18) for h in range(24)
    ]
    # Solar only during hours 8-16; 0 otherwise
    solar = [
        SolcastSlot(hour=h, pv_estimate=solar_per_hour if 8 <= h < 16 else 0.0)
        for h in range(24)
    ]
    consumption = [
        HourlyConsumptionAverage(
            hour=h,
            avg_1d=consumption_per_hour,
            avg_3d=consumption_per_hour,
            avg_7d=consumption_per_hour,
            avg_14d=consumption_per_hour,
        )
        for h in range(24)
    ]
    # Default: no discharge schedules so only solar-strategy charging runs
    if schedules is None:
        schedules = [
            BatteryScheduleInput(enabled=False, start=time(7, 0), end=time(9, 0)),
        ]
    return PlannerInput(
        now_iso=now_iso,
        interval_minutes=60,
        interval_length_hours=24,
        battery_soc_pct=battery_soc_pct,
        battery_rated_capacity_kwh=battery_rated_capacity_kwh,
        battery_end_of_discharge_soc_pct=battery_end_of_discharge_soc_pct,
        battery_max_charge_power_w=battery_max_charge_power_w,
        battery_purchase_price=10_000.0,
        battery_expected_cycles=6000,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=consumption,
        price_points=prices,
        solcast_slots=solar,
        battery_schedules=schedules,
        excess_export_enabled=False,
        excess_export_discharge_buffer_pct=10.0,
        excess_export_price_threshold=0.10,
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        house_power_includes_ev=True,
        is_read_only=True,
    )


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestSolarChargePerSlotSemantics:
    """batteries_charged_kwh on each slot must store per-slot energy, not cumulative."""

    def test_per_slot_value_does_not_exceed_slot_solar_surplus(self):
        """Each solar-charge slot must not charge more than its own solar surplus."""
        inp = _make_solar_only_input(battery_soc_pct=0.0, solar_per_hour=3.0)
        result = run_planner(inp)

        solar_slots = [
            s for s in result.slots if s.recommendation == _SOLAR_CHARGE_VALUE
        ]
        assert solar_slots, "Expected at least one BatteriesChargeSolar slot"

        for slot in solar_slots:
            # Per-slot surplus: PV production - consumption (negative net consumption)
            surplus = abs(slot.estimated_net_consumption_kwh)
            assert slot.batteries_charged_kwh <= surplus + 1e-6, (
                f"Slot {slot.start.isoformat()}: batteries_charged={slot.batteries_charged_kwh} "
                f"exceeds available solar surplus={surplus}"
            )

    def test_sum_matches_total_charged_energy_kwh(self):
        """Summing batteries_charged_kwh across all slots must equal total_charged_energy_kwh."""
        inp = _make_solar_only_input(battery_soc_pct=0.0, solar_per_hour=3.0)
        result = run_planner(inp)

        manual_sum = round(sum(s.batteries_charged_kwh for s in result.slots), 3)
        reported = result.total_charged_energy_kwh()

        assert abs(manual_sum - reported) < 1e-6, (
            f"Manual sum {manual_sum} != total_charged_energy_kwh {reported}"
        )

    def test_total_charged_does_not_exceed_usable_capacity(self):
        """Total solar charge must not exceed the available battery headroom."""
        rated = 10.0
        eod_pct = 10.0
        soc = 0.0
        usable = rated * (1 - eod_pct / 100.0)  # 9 kWh
        current_kwh = soc / 100.0 * rated
        headroom = max(usable - current_kwh, 0.0)

        inp = _make_solar_only_input(
            battery_soc_pct=soc,
            battery_rated_capacity_kwh=rated,
            battery_end_of_discharge_soc_pct=eod_pct,
            solar_per_hour=5.0,  # abundant solar
        )
        result = run_planner(inp)

        total = result.total_charged_energy_kwh()
        assert total <= headroom + 1e-3, (
            f"Total charged {total} kWh exceeds battery headroom {headroom} kWh"
        )

    def test_no_single_slot_holds_entire_cumulative_sum(self):
        """No individual slot may carry a batteries_charged_kwh value larger than its surplus.

        Before the fix, the last solar slot would receive the running total instead
        of its own per-slot contribution.
        """
        inp = _make_solar_only_input(
            battery_soc_pct=0.0,
            solar_per_hour=2.0,
            consumption_per_hour=0.3,
        )
        result = run_planner(inp)

        solar_slots = [
            s for s in result.slots if s.recommendation == _SOLAR_CHARGE_VALUE
        ]
        assert len(solar_slots) >= 2, (
            "Need multiple solar-charge slots to test double-count regression"
        )

        for slot in solar_slots:
            per_slot_max = abs(slot.estimated_net_consumption_kwh)
            assert slot.batteries_charged_kwh <= per_slot_max + 1e-6, (
                f"Slot {slot.start.isoformat()}: batteries_charged={slot.batteries_charged_kwh} "
                f"is larger than per-slot surplus={per_slot_max}. "
                "Likely the cumulative total was stored in this slot (double-count bug)."
            )

    def test_multiple_solar_slots_distribute_charge_correctly(self):
        """With many solar hours and an empty battery, charge is spread across slots."""
        inp = _make_solar_only_input(
            battery_soc_pct=0.0,
            solar_per_hour=2.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
        )
        result = run_planner(inp)

        solar_slots = [
            s for s in result.slots if s.recommendation == _SOLAR_CHARGE_VALUE
        ]
        # Total across slots must equal reported total (no double-count)
        manual_sum = round(sum(s.batteries_charged_kwh for s in solar_slots), 3)
        reported = result.total_charged_energy_kwh()
        assert abs(manual_sum - reported) < 1e-6

        # Each individual slot must not exceed its per-slot surplus
        for slot in solar_slots:
            surplus = abs(slot.estimated_net_consumption_kwh)
            assert slot.batteries_charged_kwh <= surplus + 1e-6, (
                f"Slot {slot.start.isoformat()}: per_slot={slot.batteries_charged_kwh} "
                f"> surplus={surplus}"
            )

    def test_partial_battery_fill_does_not_exceed_rated_capacity(self):
        """With abundant solar, total charged energy must never exceed the rated capacity.

        This verifies the key invariant: no matter how many solar-surplus slots the
        optimizer processes, the per-slot accumulator never inflates individual slot
        values beyond what was actually charged in that slot.
        """
        rated = 10.0
        eod_pct = 10.0
        soc = 0.0

        inp = _make_solar_only_input(
            battery_soc_pct=soc,
            battery_rated_capacity_kwh=rated,
            battery_end_of_discharge_soc_pct=eod_pct,
            solar_per_hour=5.0,  # 8 h × 5 kWh = 40 kWh >> battery
        )
        result = run_planner(inp)

        total = result.total_charged_energy_kwh()
        # Total charged must not exceed rated capacity (usable + reserve) as an
        # absolute upper bound — the planner may cap at usable but never above rated.
        assert total <= rated + 1e-3, (
            f"Total charged {total} kWh exceeds rated capacity {rated} kWh"
        )

        # Each individual solar slot must store a per-slot value ≤ its own surplus
        for slot in result.slots:
            if slot.recommendation == _SOLAR_CHARGE_VALUE:
                surplus = abs(slot.estimated_net_consumption_kwh)
                assert slot.batteries_charged_kwh <= surplus + 1e-6, (
                    f"Slot {slot.start.isoformat()}: batteries_charged={slot.batteries_charged_kwh} "
                    f"> slot surplus={surplus} (cumulative bug?)"
                )

    @pytest.mark.skip(reason="MILP-only mode: schedule-based behavior not applicable")
    def test_fully_charged_battery_no_solar_charge(self):
        """A 100 % SoC battery must not actually charge (SoC clamps to 0).

        Solar charging slots may still be planned (per-day budget) but the
        SoC simulation clamps them to zero and clears the recommendation
        when the battery has no headroom.
        """
        inp = _make_solar_only_input(
            battery_soc_pct=100.0,
            solar_per_hour=5.0,
        )
        result = run_planner(inp)

        total = result.total_charged_energy_kwh()
        assert total <= 1e-3, f"Expected ~0 kWh charged for a full battery, got {total}"

"""Tests for planner power-threshold boundary behaviour (issue #272).

Verifies that the planner classifies slots correctly at, above, and below
each power threshold boundary.

All tests are pure-Python — no Home Assistant runtime is required.
"""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.discharge_scheduler import (
    apply_optimization_strategy,
)
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = UTC
_CHARGE_SOLAR = Recommendations.BatteriesChargeSolar.value
_CHARGE_GRID = Recommendations.BatteriesChargeGrid.value
_DISCHARGE = Recommendations.BatteriesDischargeMode.value


def _slot(
    hour: int = 10,
    net_consumption: float = 0.0,
    import_price: float = 0.20,
    export_price: float = 0.05,
    recommendation: str | None = None,
) -> PlannedSlot:
    """Construct a minimal PlannedSlot for testing threshold logic."""
    start = datetime(2024, 6, 15, hour, 0, tzinfo=_UTC)
    end = datetime(2024, 6, 15, hour + 1, 0, tzinfo=_UTC)
    return PlannedSlot(
        start=start,
        end=end,
        price=SlotPrice(import_price=import_price, export_price=export_price),
        estimated_net_consumption_kwh=net_consumption,
        recommendation=recommendation,
    )


def _now(hour: int = 0) -> datetime:
    return datetime(2024, 6, 15, hour, 0, tzinfo=_UTC)


def _make_minimal_input(
    solcast_kwh_per_hour: list[float],
    consumption_kwh_per_hour: list[float],
    *,
    now_iso: str = "2024-06-15T00:00:00+00:00",
    months_winter: list[int] | None = None,
    battery_soc_pct: float = 50.0,
    interval_minutes: int = 60,
) -> PlannerInput:
    """Build a PlannerInput from parallel per-hour lists."""
    prices = [
        PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(24)
    ]
    solar = [
        SolcastSlot(hour=h, pv_estimate=kwh)
        for h, kwh in enumerate(solcast_kwh_per_hour)
    ]
    consumption = [
        HourlyConsumptionAverage(
            hour=h, avg_1d=kwh, avg_3d=kwh, avg_7d=kwh, avg_14d=kwh
        )
        for h, kwh in enumerate(consumption_kwh_per_hour)
    ]
    return PlannerInput(
        now_iso=now_iso,
        interval_minutes=interval_minutes,
        interval_length_hours=24,
        battery_soc_pct=battery_soc_pct,
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_charge_power_w=5000.0,
        battery_purchase_price=10_000.0,
        battery_expected_cycles=6000,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=consumption,
        price_points=prices,
        solcast_slots=solar,
        excess_export_enabled=False,
        excess_export_discharge_buffer_pct=10.0,
        excess_export_price_threshold=0.10,
        months_winter=(
            months_winter if months_winter is not None else [1, 2, 3, 4, 10, 11, 12]
        ),
        house_power_includes_ev=False,
    )


# ===========================================================================
# 2. apply_optimization_strategy — near-zero consumption threshold
# ===========================================================================


class TestNearZeroThresholdInOptimizationStrategy:
    """Seasonal optimisation assigns solar charge only to slots with an
    actual PV surplus (negative net consumption).  Slots with a small
    positive house load must NOT be mislabeled as solar-charging
    opportunities (issue #720)."""

    def _run_summer(self, net_consumption: float) -> str | None:
        """Run optimization strategy on a single unassigned summer slot."""
        now = _now(12)  # noon in June
        slot = _slot(hour=12, net_consumption=net_consumption)
        apply_optimization_strategy(
            slots=[slot],
            now=now,
            current_capacity=5.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
        )
        return slot.recommendation

    def test_surplus_assigned_charge_solar(self):
        """A slot with negative net consumption (PV surplus) must get BatteriesChargeSolar."""
        assert self._run_summer(-0.5) == _CHARGE_SOLAR

    def test_at_exact_zero_assigned_discharge(self):
        """A slot at exactly zero net consumption has no PV surplus — must get
        BatteriesDischargeMode, not BatteriesChargeSolar."""
        assert self._run_summer(0.0) == _DISCHARGE

    def test_small_positive_consumption_assigned_discharge(self):
        """A slot with small positive consumption (0.08 kWh) and no PV must
        get BatteriesDischargeMode, not BatteriesChargeSolar (issue #720)."""
        assert self._run_summer(0.08) == _DISCHARGE

    def test_just_above_threshold_assigned_discharge(self):
        """A slot just above the old threshold (0.11 kWh) must get BatteriesDischargeMode."""
        assert self._run_summer(0.11) == _DISCHARGE

    def test_high_consumption_assigned_discharge(self):
        """A high-consumption slot (1.2 kWh) must get BatteriesDischargeMode in summer."""
        assert self._run_summer(1.2) == _DISCHARGE


# ===========================================================================
# 3. apply_optimization_strategy — solar charging loop threshold
# ===========================================================================


class TestSolarChargingLoopThreshold:
    """The 'solar charging until battery full' loop inside
    apply_optimization_strategy only charges from actual PV surplus
    (negative net consumption)."""

    def _run_solar_charge_loop(self, net_consumptions: list[float]) -> list[str | None]:
        """Return recommendations from multiple slots after the charge loop."""
        now = _now(0)  # midnight so all today's slots are eligible
        slots = [
            _slot(hour=h, net_consumption=nc) for h, nc in enumerate(net_consumptions)
        ]
        apply_optimization_strategy(
            slots=slots,
            now=now,
            current_capacity=0.0,  # battery empty → needs charging
            usable_capacity=5.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
        )
        return [s.recommendation for s in slots]

    def test_surplus_slots_charged_first(self):
        """Slots with PV surplus (negative net) should receive BatteriesChargeSolar."""
        recs = self._run_solar_charge_loop([-0.5, -0.3, -0.1])
        assert all(r == _CHARGE_SOLAR for r in recs)

    def test_consumption_slot_skipped_by_charge_loop(self):
        """A slot with positive consumption should not be charged by the solar loop."""
        # mix: slot 0 is surplus, slot 1 is consumption
        recs = self._run_solar_charge_loop([-0.5, 0.5])
        # slot 0: should be charged
        assert recs[0] == _CHARGE_SOLAR
        # slot 1: positive consumption, so NOT charged by the loop
        # (it will become BatteriesDischargeMode from the seasonal fill)
        assert recs[1] != _CHARGE_SOLAR

    def test_at_exact_zero_excluded_from_charge_loop(self):
        """A slot at exactly zero net consumption has no PV surplus — excluded."""
        recs = self._run_solar_charge_loop([0.0])
        assert recs[0] != _CHARGE_SOLAR

    def test_small_positive_consumption_excluded_from_charge_loop(self):
        """A slot with small positive consumption (0.08 kWh) and no PV must
        be excluded from the solar charge loop (issue #720)."""
        recs = self._run_solar_charge_loop([0.08])
        assert recs[0] != _CHARGE_SOLAR


# ===========================================================================
# 4. End-to-end planner — threshold boundaries via run_planner
# ===========================================================================


class TestPlannerThresholdEndToEnd:
    """Full planner runs to confirm thresholds are respected end-to-end."""

    def test_no_false_solar_charge_on_consumption_hours(self):
        """Hours where consumption clearly exceeds solar must NOT be
        classified as BatteriesChargeSolar (summer)."""
        # High consumption at night, no solar at night
        solar = [0.0] * 6 + [0.1] * 18
        consumption = [1.5] * 6 + [0.05] * 18  # night consumption >> solar
        inp = _make_minimal_input(
            solar,
            consumption,
            now_iso="2024-06-15T00:00:00+00:00",
        )
        output = run_planner(inp)

        # Night slots 0-5 have no solar; net consumption is strongly positive
        # → must NOT be BatteriesChargeSolar
        night_solar_charged = [
            s
            for s in output.slots
            if s.start.hour < 6 and s.recommendation == _CHARGE_SOLAR
        ]
        assert not night_solar_charged, (
            f"Unexpected solar charge slots at night: "
            f"{[s.start.hour for s in night_solar_charged]}"
        )

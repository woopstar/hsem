"""Tests for named power threshold constants (issue #272).

Verifies that:
- ``SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH`` and
  ``NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH`` are exported from
  ``custom_components.hsem.const``.
- The planner classifies slots correctly at, above, and below each boundary.
- The default values preserve v5.1.0 behaviour (no silent regression).

All tests are pure-Python — no Home Assistant runtime is required.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from custom_components.hsem.const import (
    NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH,
    SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH,
)
from custom_components.hsem.models.planner_inputs import (
    BatteryScheduleInput,
    HourlyConsumptionAverage,
    PlannerInput,
    PricePoint,
    SolcastSlot,
)
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.charge_scheduler import apply_charge_schedules
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
    schedules: list[BatteryScheduleInput] | None = None,
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
        battery_schedules=schedules if schedules is not None else [],
        excess_export_enabled=False,
        excess_export_discharge_buffer_pct=10.0,
        excess_export_price_threshold=0.10,
        months_winter=(
            months_winter if months_winter is not None else [1, 2, 3, 4, 10, 11, 12]
        ),
        house_power_includes_ev=False,
        is_read_only=True,
    )


# ===========================================================================
# 1. Constant definitions and default values
# ===========================================================================


class TestConstantDefinitions:
    """Constants must exist in const.py with correct default values."""

    def test_solar_surplus_threshold_is_negative(self):
        """SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH must be negative (surplus means export)."""
        assert SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH < 0

    def test_solar_surplus_threshold_default_matches_v510(self):
        """Default solar surplus threshold must match v5.1.0 value of -0.2 kWh."""
        assert pytest.approx(-0.2) == SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH

    def test_near_zero_threshold_is_positive_or_zero(self):
        """NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH must be >= 0 (small positive buffer)."""
        assert NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH >= 0

    def test_near_zero_threshold_default_matches_v510(self):
        """Default near-zero threshold must match v5.1.0 value of 0.1 kWh."""
        assert pytest.approx(0.1) == NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH

    def test_solar_surplus_threshold_less_than_near_zero(self):
        """Solar surplus threshold must be below the near-zero threshold."""
        assert SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH < NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH


# ===========================================================================
# 2. apply_charge_schedules — solar surplus threshold
# ===========================================================================


class TestSolarSurplusThresholdInChargeSchedules:
    """Slots qualify for Priority-2 solar charge only when net consumption
    is strictly below SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH."""

    def _run(self, net_consumption: float) -> str | None:
        """Return the recommendation assigned to a single candidate slot.

        Priority-3 (cheapest grid hours) is disabled by setting a very large
        depreciation threshold so the test can isolate whether Priority-2
        (solar surplus) fires.  The flat 0.20 import price would never satisfy
        a high depreciation guard, so the slot stays unassigned unless the
        solar surplus condition triggers.
        """
        now = _now(0)
        # Put the candidate slot just before a discharge window
        candidate = _slot(hour=6, net_consumption=net_consumption)
        # A later slot acts as the discharge window
        discharge_slot = _slot(
            hour=8,
            net_consumption=0.5,
            recommendation=Recommendations.BatteriesDischargeMode.value,
        )
        slots = [candidate, discharge_slot]

        sched = BatteryScheduleInput(
            enabled=True,
            start=time(8, 0),
            end=time(9, 0),
        )
        # Pre-set discharge schedule metadata (normally done by apply_discharge_schedules)
        sched._needed_capacity = 0.5  # type: ignore[attr-defined]
        sched._avg_import_price = 0.20  # type: ignore[attr-defined]

        apply_charge_schedules(
            slots=slots,
            battery_schedules=[sched],
            now=now,
            max_charge_per_interval=5.0,
            recommended_threshold=1000.0,
        )
        return candidate.recommendation

    def test_large_solar_surplus_is_charged(self):
        """A slot with -1.0 kWh net (strong surplus) must be solar-charged."""
        assert self._run(-1.0) == _CHARGE_SOLAR

    def test_at_exact_threshold_not_charged(self):
        """A slot exactly at the threshold (-0.2) must NOT be solar-charged
        (condition is strictly less-than)."""
        assert self._run(SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH) is None

    def test_just_below_threshold_is_charged(self):
        """A slot just below the threshold (-0.21) must be solar-charged."""
        assert self._run(SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH - 0.01) == _CHARGE_SOLAR

    def test_just_above_threshold_not_charged(self):
        """A slot just above the threshold (-0.19) must NOT be solar-charged."""
        assert self._run(SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH + 0.01) is None

    def test_zero_net_consumption_not_solar_charged(self):
        """A balanced slot (0.0 kWh net) must NOT qualify for Priority-2."""
        assert self._run(0.0) is None

    def test_positive_net_consumption_not_solar_charged(self):
        """A consumption slot (0.5 kWh net) must NOT qualify for Priority-2."""
        assert self._run(0.5) is None


# ===========================================================================
# 3. apply_optimization_strategy — near-zero consumption threshold
# ===========================================================================


class TestNearZeroThresholdInOptimizationStrategy:
    """Seasonal optimisation uses NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH to
    decide whether an unassigned summer slot should charge from solar or
    discharge the battery."""

    def _run_summer(self, net_consumption: float) -> str | None:
        """Run optimization strategy on a single unassigned summer slot."""
        now = _now(12)  # noon in June
        slot = _slot(hour=12, net_consumption=net_consumption)
        warnings: list[str] = []
        apply_optimization_strategy(
            slots=[slot],
            now=now,
            current_capacity=5.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
            warnings=warnings,
        )
        return slot.recommendation

    def test_below_threshold_assigned_charge_solar(self):
        """A slot with net < threshold (e.g. -0.5 kWh) must get BatteriesChargeSolar."""
        assert self._run_summer(-0.5) == _CHARGE_SOLAR

    def test_at_exact_threshold_assigned_charge_solar(self):
        """A slot at exactly the threshold (0.1 kWh) must get BatteriesChargeSolar
        because the condition is <=."""
        assert self._run_summer(NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH) == _CHARGE_SOLAR

    def test_just_above_threshold_assigned_discharge(self):
        """A slot just above the threshold (0.11 kWh) must get BatteriesDischargeMode."""
        assert (
            self._run_summer(NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH + 0.01) == _DISCHARGE
        )

    def test_high_consumption_assigned_discharge(self):
        """A high-consumption slot (1.2 kWh) must get BatteriesDischargeMode in summer."""
        assert self._run_summer(1.2) == _DISCHARGE

    def test_zero_net_consumption_assigned_charge_solar(self):
        """Zero net consumption is <= threshold, so must get BatteriesChargeSolar."""
        assert self._run_summer(0.0) == _CHARGE_SOLAR


# ===========================================================================
# 4. apply_optimization_strategy — solar charging loop threshold
# ===========================================================================


class TestSolarChargingLoopThreshold:
    """The 'solar charging until battery full' loop inside
    apply_optimization_strategy also uses NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH."""

    def _run_solar_charge_loop(self, net_consumptions: list[float]) -> list[str | None]:
        """Return recommendations from multiple slots after the charge loop."""
        now = _now(0)  # midnight so all today's slots are eligible
        slots = [
            _slot(hour=h, net_consumption=nc) for h, nc in enumerate(net_consumptions)
        ]
        warnings: list[str] = []
        apply_optimization_strategy(
            slots=slots,
            now=now,
            current_capacity=0.0,  # battery empty → needs charging
            usable_capacity=5.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
            warnings=warnings,
        )
        return [s.recommendation for s in slots]

    def test_surplus_slots_charged_first(self):
        """Slots below threshold should receive BatteriesChargeSolar in charge loop."""
        # All slots below threshold
        recs = self._run_solar_charge_loop([-0.5, -0.3, -0.1])
        assert all(r == _CHARGE_SOLAR for r in recs)

    def test_consumption_slot_skipped_by_charge_loop(self):
        """A slot above the threshold should not be charged by the solar loop."""
        # mix: slot 0 is surplus, slot 1 is consumption
        recs = self._run_solar_charge_loop([-0.5, 0.5])
        # slot 0: should be charged
        assert recs[0] == _CHARGE_SOLAR
        # slot 1: above threshold, so NOT charged by the loop
        # (it will become BatteriesDischargeMode from the seasonal fill)
        assert recs[1] != _CHARGE_SOLAR

    def test_at_exact_threshold_included_in_charge_loop(self):
        """A slot at exactly the near-zero threshold is included (condition <=)."""
        recs = self._run_solar_charge_loop([NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH])
        assert recs[0] == _CHARGE_SOLAR

    def test_just_above_threshold_excluded_from_charge_loop(self):
        """A slot just above threshold is excluded from the solar charge loop."""
        recs = self._run_solar_charge_loop([NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH + 0.01])
        assert recs[0] != _CHARGE_SOLAR


# ===========================================================================
# 5. End-to-end planner — threshold boundaries via run_planner
# ===========================================================================


class TestPlannerThresholdEndToEnd:
    """Full planner runs to confirm thresholds are respected when coordinated
    with schedule-based charge/discharge decisions."""

    @pytest.mark.skip(reason="MILP-only mode: schedule-based behavior not applicable")
    def test_summer_solar_slots_are_charge_solar(self):
        """On a summer day with strong solar production, peak solar hours
        must be classified as BatteriesChargeSolar (not grid or discharge)."""
        # Solar dominates 10:00–14:00 with > 3 kWh production
        # while house consumes only ~0.5 kWh → large negative net consumption
        solar = [0.0] * 10 + [3.5, 4.0, 4.5, 4.0, 3.5] + [1.0, 0.5, 0.1] + [0.0] * 6
        consumption = [0.5] * 24
        inp = _make_minimal_input(solar, consumption)
        output = run_planner(inp)

        solar_charged_hours = {
            s.start.hour for s in output.slots if s.recommendation == _CHARGE_SOLAR
        }
        # At least hours 10-14 should be solar-charged (surplus >> 0.2 threshold)
        assert solar_charged_hours.issuperset(
            {10, 11, 12, 13, 14}
        ), f"Expected solar hours 10-14 to be charged, got: {solar_charged_hours}"

    def test_no_false_solar_charge_on_consumption_hours(self):
        """Hours where consumption clearly exceeds solar must NOT be
        classified as BatteriesChargeSolar (summer, no schedule)."""
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

    def test_constant_values_match_threshold_names(self):
        """The threshold constants used by the planner match the documented
        v5.1.0 default values exactly — a regression guard."""
        assert pytest.approx(-0.2) == SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH, (
            "SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH must default to -0.2 kWh "
            "(v5.1.0 backward compatibility)"
        )
        assert pytest.approx(0.1) == NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH, (
            "NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH must default to 0.1 kWh "
            "(v5.1.0 backward compatibility)"
        )

    def test_planner_uses_named_constants_not_literals(self):
        """charge_scheduler.py and discharge_scheduler.py must not contain
        unexplained 0.1 or 0.2 kW literals — they must reference the named
        constants instead."""
        import ast
        from pathlib import Path

        for mod_name in ("charge_scheduler.py", "discharge_scheduler.py"):
            scheduler_path = (
                Path(__file__).parents[1]
                / "custom_components"
                / "hsem"
                / "planner"
                / mod_name
            )
            source = scheduler_path.read_text(encoding="utf-8")
            tree = ast.parse(source)

            suspicious: list[tuple[int, float]] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, float):
                    # Flag bare 0.1 and 0.2 — the only legitimate use of these
                    # values is now via named constants imported from const.py
                    if node.value in (0.1, 0.2, -0.1, -0.2):
                        suspicious.append((node.lineno, node.value))

            assert not suspicious, (
                f"{mod_name} still contains bare power threshold literals "
                f"at lines {suspicious}. Use SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH or "
                f"NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH instead."
            )

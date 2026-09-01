"""Tests for the pure-Python HSEM planner engine (#279).

Acceptance criteria verified here
----------------------------------
- Planner can run in unit tests without Home Assistant.
- Tests can assert selected charge/discharge windows.
- A sample fixture covers 24 hours.

All tests are synchronous and import nothing from Home Assistant's runtime.
They exercise the public ``run_planner`` function from
``custom_components.hsem.planner``.
"""

from __future__ import annotations

import pytest

from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.planner import run_planner
from custom_components.hsem.utils.recommendations import Recommendations
from tests.planner.fixtures import (
    make_flat_price_input,
    make_summer_day_input,
    make_winter_day_input,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_CHARGE_VALUES = {
    Recommendations.BatteriesChargeGrid.value,
    Recommendations.BatteriesChargeSolar.value,
}


# ===========================================================================
# 1. Basic planner contract
# ===========================================================================


class TestPlannerContract:
    """The planner must run without HA and return a well-formed output."""

    def test_returns_planner_output(self):
        """run_planner must return a PlannerOutput instance."""
        result = run_planner(make_summer_day_input())
        assert isinstance(result, PlannerOutput)

    def test_no_home_assistant_import_needed(self):
        """Importing the engine must not pull in homeassistant.*."""

        # Verify we can import the engine without homeassistant being fully loaded
        # (it may already be on sys.modules from conftest; what matters is that
        # the engine itself does NOT import homeassistant at top-level).
        import custom_components.hsem.planner.engine_core as engine_mod

        engine_source = engine_mod.__file__
        with open(engine_source, encoding="utf-8") as fh:
            source = fh.read()

        # Top-level homeassistant imports are not allowed
        import ast

        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("homeassistant"), (
                        f"Engine imports 'homeassistant' at top-level: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("homeassistant"):
                    assert False, (
                        f"Engine imports from 'homeassistant' at top-level: {node.module}"
                    )

    def test_24_hour_fixture_produces_24_slots(self):
        """A 24-hour, 60-min fixture must produce exactly 24 slots."""
        result = run_planner(
            make_summer_day_input(interval_minutes=60, interval_length_hours=24)
        )
        assert len(result.slots) == 24

    def test_96_slot_fixture_for_15_min_intervals(self):
        """A 24-hour, 15-min fixture must produce 96 slots."""
        result = run_planner(
            make_summer_day_input(interval_minutes=15, interval_length_hours=24)
        )
        assert len(result.slots) == 96

    def test_slots_are_contiguous(self):
        """Every slot's end must equal the next slot's start."""
        result = run_planner(make_summer_day_input())
        for a, b in zip(result.slots, result.slots[1:]):
            assert a.end == b.start, (
                f"Gap between {a.end.isoformat()} and {b.start.isoformat()}"
            )

    def test_all_slots_have_recommendation(self):
        """Every slot must have a non-None recommendation after planning."""
        result = run_planner(make_summer_day_input())
        for slot in result.slots:
            assert slot.recommendation is not None, (
                f"Slot {slot.start.isoformat()} has no recommendation"
            )

    def test_recommendations_are_known_values(self):
        """All slot recommendations must be valid Recommendations enum values."""
        valid_values = {r.value for r in Recommendations}
        result = run_planner(make_summer_day_input())
        for slot in result.slots:
            assert slot.recommendation in valid_values, (
                f"Unknown recommendation: {slot.recommendation!r}"
            )

    def test_missing_inputs_empty_on_full_fixture(self):
        """A fully populated fixture should produce no missing-input entries."""
        result = run_planner(make_summer_day_input())
        assert result.missing_inputs == []


# ===========================================================================
# 2. Slot values and structure
# ===========================================================================


class TestSlotValues:
    """Per-slot computed values must be consistent with inputs."""

    def test_net_consumption_equals_load_minus_pv(self):
        """estimated_net_consumption_kwh must equal avg_consumption - pv_estimate."""
        result = run_planner(make_summer_day_input())
        for slot in result.slots:
            expected = round(
                slot.avg_house_consumption_kwh - slot.solcast_pv_estimate_kwh, 3
            )
            assert abs(slot.estimated_net_consumption_kwh - expected) < 1e-6, (
                f"Hour {slot.start.hour}: net={slot.estimated_net_consumption_kwh}, "
                f"expected={expected}"
            )

    def test_battery_soc_bounded(self):
        """Battery SoC estimates must be in [0, 100]."""
        result = run_planner(make_summer_day_input())
        for slot in result.slots:
            assert 0 <= slot.estimated_battery_soc_pct <= 100, (
                f"SoC out of range at {slot.start.isoformat()}: {slot.estimated_battery_soc_pct}"
            )

    def test_battery_capacity_bounded(self):
        """Battery capacity estimates must never exceed usable_capacity."""
        inp = make_summer_day_input(
            battery_rated_capacity_kwh=10.0, battery_end_of_discharge_soc_pct=10.0
        )
        usable = 10.0 * (1 - 0.10)  # 9 kWh
        result = run_planner(inp)
        for slot in result.slots:
            assert slot.estimated_battery_capacity_kwh <= usable + 1e-6, (
                f"Capacity {slot.estimated_battery_capacity_kwh} exceeds usable {usable}"
            )

    def test_batteries_charged_non_negative(self):
        """batteries_charged_kwh must never be negative."""
        result = run_planner(make_summer_day_input())
        for slot in result.slots:
            assert slot.batteries_charged_kwh >= 0, (
                f"Negative batteries_charged_kwh at {slot.start.isoformat()}"
            )

    def test_prices_populated_correctly(self):
        """Import/export prices must match the fixture price_points."""
        inp = make_summer_day_input()
        price_by_hour = {pp.hour: pp for pp in inp.price_points}
        result = run_planner(inp)
        for slot in result.slots:
            h = slot.start.hour
            if h in price_by_hour:
                assert (
                    abs(slot.price.import_price - price_by_hour[h].import_price) < 1e-9
                )
                assert (
                    abs(slot.price.export_price - price_by_hour[h].export_price) < 1e-9
                )


# ===========================================================================
# 3. Charge scheduling
# ===========================================================================


class TestChargeScheduling:
    """The planner must select appropriate charge slots before discharge windows."""

    def test_charge_slots_exist(self):
        """At least one charge slot must be selected when battery needs energy."""
        # Start with empty battery so charging is definitely needed
        inp = make_summer_day_input(battery_soc_pct=0.0)
        result = run_planner(inp)
        assert result.charge_slot_count() > 0, "Expected at least one charge slot"

    def test_charge_windows_detected(self):
        """PlannerOutput.charge_windows must be populated when charging occurs."""
        inp = make_summer_day_input(battery_soc_pct=0.0)
        result = run_planner(inp)
        assert result.charge_windows, "Expected charge windows in the output"

    def test_battery_charges_when_empty(self):
        """When battery starts empty, it must be charged by end of day.

        With solar PV available and a summer day, the battery is expected to
        charge via solar production.  ``charge_slot_count()`` counts slots with
        an explicit charge recommendation; ``battery_soc_at_end`` also rises
        from automatic PV capture even without explicit charge slots.

        We check that at least one of these conditions holds:
        - Explicit charge recommendations are present (charge_slot_count > 0), OR
        - Battery SoC at end of day is above the discharge floor (10% + tolerance).
        """
        inp = make_summer_day_input(battery_soc_pct=0.0)
        result = run_planner(inp)
        # At least one charging mechanism must have been active:
        # explicit charge slots OR battery SoC rose above the empty floor.
        end_of_discharge_floor = inp.battery_end_of_discharge_soc_pct
        soc_rose = result.battery_soc_at_end > end_of_discharge_floor + 1.0
        has_charge_slots = result.charge_slot_count() > 0
        assert soc_rose or has_charge_slots, (
            f"Expected battery to charge when starting empty on summer day. "
            f"charge_slot_count={result.charge_slot_count()}, "
            f"battery_soc_at_end={result.battery_soc_at_end:.1f}% "
            f"(floor={end_of_discharge_floor}%)"
        )

    def test_solar_surplus_can_trigger_solar_charge(self):
        """Summer mid-day hours with large PV surplus should produce solar charge slots."""
        # Start empty so solar surplus has headroom to charge into
        inp = make_summer_day_input(battery_soc_pct=0.0)
        result = run_planner(inp)
        solar_charge_slots = result.slots_with_recommendation(
            Recommendations.BatteriesChargeSolar.value
        )
        assert solar_charge_slots, (
            "Expected at least one BatteriesChargeSolar slot on a summer day"
        )


# ===========================================================================
# 4. Current recommendation
# ===========================================================================


class TestCurrentRecommendation:
    """The current_recommendation must reflect the slot containing now."""

    def test_current_recommendation_present(self):
        """current_recommendation must be set when now falls within a slot."""
        # Plan from midnight; now = midnight, so the first slot is current
        inp = make_summer_day_input(now_iso="2024-06-15T00:00:00+02:00")
        result = run_planner(inp)
        assert result.current_recommendation is not None

    def test_current_recommendation_valid(self):
        """current_recommendation must be a known Recommendations value."""
        valid_values = {r.value for r in Recommendations}
        inp = make_summer_day_input(now_iso="2024-06-15T08:00:00+02:00")
        result = run_planner(inp)
        assert result.current_recommendation in valid_values


# ===========================================================================
# 5. Winter vs summer season logic
# ===========================================================================


class TestSeasonalLogic:
    """Winter and summer months must produce different recommendation patterns."""

    def test_winter_defaults_to_wait_mode(self):
        """Unassigned winter slots must be BatteriesWaitMode (not discharge).

        The aggressive candidate may override some winter slots with
        BatteriesDischargeMode on the most expensive hours — this is correct
        behavior (expensive winter evening peaks are worth discharging for).
        Verify that BatteriesWaitMode is still present for the winter fallback.
        """
        result = run_planner(make_winter_day_input())
        wait_mode_slots = [
            s
            for s in result.slots
            if s.recommendation == Recommendations.BatteriesWaitMode.value
        ]
        assert wait_mode_slots, (
            "Winter: expected BatteriesWaitMode slots from seasonal fallback"
        )

    def test_summer_has_solar_charge_slots(self):
        """A clear summer day must have BatteriesChargeSolar recommendations."""
        result = run_planner(make_summer_day_input())
        solar_slots = result.slots_with_recommendation(
            Recommendations.BatteriesChargeSolar.value
        )
        assert solar_slots, "Expected BatteriesChargeSolar slots on summer day"


# ===========================================================================
# 6. 24-hour fixture completeness
# ===========================================================================


class TestFixtureCompleteness:
    """The 24-hour fixtures must provide all required data."""

    def test_summer_fixture_has_24_price_points(self):
        inp = make_summer_day_input()
        assert len(inp.price_points) == 24

    def test_summer_fixture_has_24_solcast_slots(self):
        inp = make_summer_day_input()
        assert len(inp.solcast_slots) == 24

    def test_summer_fixture_has_24_consumption_averages(self):
        inp = make_summer_day_input()
        assert len(inp.consumption_averages) == 24

    def test_summer_fixture_weights_sum_to_100(self):
        inp = make_summer_day_input()
        assert inp.weight_1d + inp.weight_3d + inp.weight_7d + inp.weight_14d == 100

    def test_winter_fixture_weights_sum_to_100(self):
        inp = make_winter_day_input()
        assert inp.weight_1d + inp.weight_3d + inp.weight_7d + inp.weight_14d == 100

    def test_flat_fixture_all_slots_same_price(self):
        inp = make_flat_price_input(import_price=0.25, export_price=0.10)
        result = run_planner(inp)
        import_prices = [s.price.import_price for s in result.slots]
        assert all(abs(p - 0.25) < 1e-9 for p in import_prices), (
            "Not all slots have the expected flat import price"
        )


# ===========================================================================
# 7. Output helper methods
# ===========================================================================


class TestOutputHelpers:
    """PlannerOutput helper methods must return correct values."""

    def test_slots_with_recommendation_filters_correctly(self):
        result = run_planner(make_summer_day_input())
        charge_slots = result.slots_with_recommendation(
            Recommendations.BatteriesChargeGrid.value
        )
        for s in charge_slots:
            assert s.recommendation == Recommendations.BatteriesChargeGrid.value

    def test_charge_slot_count_matches_manual_count(self):
        result = run_planner(make_summer_day_input(battery_soc_pct=0.0))
        manual = sum(1 for s in result.slots if s.recommendation in _CHARGE_VALUES)
        assert result.charge_slot_count() == manual

    def test_total_charged_energy_matches_sum(self):
        result = run_planner(make_summer_day_input(battery_soc_pct=0.0))
        expected = round(sum(s.batteries_charged_kwh for s in result.slots), 3)
        assert abs(result.total_charged_energy_kwh() - expected) < 1e-6


# ===========================================================================
# 8. Edge cases
# ===========================================================================


class TestEdgeCases:
    """Planner must handle degenerate inputs gracefully."""

    def test_zero_pv_no_solar_charge_slots(self):
        """With zero PV production there must be no BatteriesChargeSolar slots."""
        inp = make_flat_price_input(battery_soc_pct=0.0)
        # flat_price_input already has zero PV
        result = run_planner(inp)
        solar_slots = result.slots_with_recommendation(
            Recommendations.BatteriesChargeSolar.value
        )
        assert not solar_slots, "No solar charge slots expected when PV=0"

    def test_default_winter_input_has_wait_mode_slots(self) -> None:
        """BatteriesWaitMode slots expected on the default winter fixture.

        In winter the seasonal strategy sets unassigned slots to BatteriesWaitMode.
        The MILP may still set BatteriesDischargeMode on the most expensive
        winter slots — this is correct behavior (expensive evening peaks are
        worth discharging for).
        """
        inp = make_winter_day_input()
        result = run_planner(inp)
        wait_mode = result.slots_with_recommendation(
            Recommendations.BatteriesWaitMode.value
        )
        assert wait_mode, "BatteriesWaitMode expected on the default winter fixture"

    def test_invalid_timezone_raises(self):
        """Passing a naive datetime string must raise ValueError."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:00:00")  # no tz
        with pytest.raises(ValueError, match="timezone-aware"):
            run_planner(inp)

    def test_weight_mismatch_produces_warning(self):
        """Weights that do not sum to 100 must produce a warning."""
        inp = make_summer_day_input()
        inp.weight_1d = 10  # sum = 85 instead of 100
        result = run_planner(inp)
        assert any("weights sum" in w for w in result.warnings), (
            "Expected a weight-mismatch warning"
        )

    def test_single_slot_horizon(self):
        """A single-slot planning horizon must not crash."""
        inp = make_flat_price_input(interval_minutes=60, interval_length_hours=1)
        result = run_planner(inp)
        assert len(result.slots) == 1

    def test_battery_soc_zero_still_plans(self):
        """Planning with 0 % SoC must succeed and produce valid output."""
        inp = make_summer_day_input(battery_soc_pct=0.0)
        result = run_planner(inp)
        assert result.slots

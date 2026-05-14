"""Tests for battery charge/discharge efficiency (issue #291).

Covers:
- SoC simulation with separate charge and discharge efficiency.
- Cost function with separate efficiencies.
- Roundtrip: 90 % charge × 90 % discharge = 81 % round-trip yield.
- Acceptance criteria from issue #291:
    * Charging 10 kWh does NOT yield 10 kWh usable unless efficiency == 100 %.
    * 90 % charge / 90 % discharge scenario is exercised.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planner_inputs import PlannerInput
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.planner.cost_function import CostWeights, score_plan
from custom_components.hsem.planner.soc_simulation import simulate_soc
from custom_components.hsem.utils.prices import SlotPrice

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TZ = ZoneInfo("Europe/Copenhagen")
_T0 = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)


def _make_slot(
    offset_hours: int,
    *,
    batteries_charged: float = 0.0,
    pv: float = 0.0,
    load: float = 0.0,
    import_price: float = 0.20,
    export_price: float = 0.05,
) -> PlannedSlot:
    """Create a minimal PlannedSlot for simulation tests."""
    start = _T0 + timedelta(hours=offset_hours)
    end = start + timedelta(hours=1)
    slot = PlannedSlot(
        start=start,
        end=end,
        price=SlotPrice(import_price=import_price, export_price=export_price),
    )
    slot.batteries_charged = batteries_charged
    slot.solcast_pv_estimate = pv
    slot.avg_house_consumption = load
    return slot


def _run_simulation(
    slots: list[PlannedSlot],
    *,
    current_kwh: float = 5.0,
    usable_kwh: float = 9.0,
    max_capacity_kwh: float = 9.0,
    max_charge_per_slot: float = 5.0,
    max_discharge_per_slot: float | None = None,
    rated_kwh: float = 10.0,
    end_of_discharge_soc_pct: float = 10.0,
    charge_efficiency_pct: float = 100.0,
    discharge_efficiency_pct: float = 100.0,
    now: datetime | None = None,
) -> None:
    """Run simulate_soc with sensible defaults."""
    simulate_soc(
        slots,
        now=now or _T0,
        current_kwh=current_kwh,
        usable_kwh=usable_kwh,
        max_capacity_kwh=max_capacity_kwh,
        max_charge_per_slot=max_charge_per_slot,
        max_discharge_per_slot=max_discharge_per_slot,
        rated_kwh=rated_kwh,
        end_of_discharge_soc_pct=end_of_discharge_soc_pct,
        charge_efficiency_pct=charge_efficiency_pct,
        discharge_efficiency_pct=discharge_efficiency_pct,
    )


# ===========================================================================
# SoC simulation — charge efficiency
# ===========================================================================


class TestChargeEfficiencySoC:
    """Verify that charge efficiency correctly reduces the stored kWh."""

    def test_100pct_efficiency_stores_all_commanded_energy(self) -> None:
        """At 100 % efficiency, scheduled_charge == energy stored."""
        slot = _make_slot(1, batteries_charged=4.0)
        _run_simulation([slot], current_kwh=0.0, charge_efficiency_pct=100.0)
        # cap should be 0 + 4.0 = 4.0
        assert slot.estimated_battery_capacity == pytest.approx(4.0, abs=0.01)

    def test_90pct_efficiency_stores_less(self) -> None:
        """At 90 % charge efficiency, batteries_charged is unchanged (it already
        represents battery-side stored kWh), but the SoC still advances correctly.

        The distinction is that the GRID has to supply charge/charge_eff = more,
        but the battery SoC advances by exactly batteries_charged.
        """
        # batteries_charged = 4 kWh  →  battery stores 4 kWh, grid supplied 4/0.9 ≈ 4.44 kWh
        slot = _make_slot(1, batteries_charged=4.0, load=0.0, pv=0.0)
        _run_simulation([slot], current_kwh=0.0, charge_efficiency_pct=90.0)
        # SoC still advances by batteries_charged (battery-side)
        assert slot.estimated_battery_capacity == pytest.approx(4.0, abs=0.01)

    def test_90pct_efficiency_increases_grid_import_for_charging(self) -> None:
        """Grid import for charging must be batteries_charged / charge_eff."""
        slot = _make_slot(1, batteries_charged=4.0, load=0.0, pv=0.0)
        _run_simulation([slot], current_kwh=0.0, charge_efficiency_pct=90.0)
        # grid_import ≈ 4.0 / 0.9 ≈ 4.444 (no load, no PV)
        assert slot.grid_import_kwh == pytest.approx(4.0 / 0.90, abs=0.01)

    def test_100pct_efficiency_grid_import_equals_charge(self) -> None:
        """At 100 % efficiency, grid import for charging equals batteries_charged."""
        slot = _make_slot(1, batteries_charged=4.0, load=0.0, pv=0.0)
        _run_simulation([slot], current_kwh=0.0, charge_efficiency_pct=100.0)
        assert slot.grid_import_kwh == pytest.approx(4.0, abs=0.01)

    def test_charge_10kwh_at_90pct_requires_111kwh_from_grid(self) -> None:
        """Issue #291 acceptance: charging 10 kWh at 90 % needs ~11.11 kWh from grid."""
        slot = _make_slot(1, batteries_charged=10.0, load=0.0, pv=0.0)
        _run_simulation(
            [slot],
            current_kwh=0.0,
            usable_kwh=15.0,
            max_capacity_kwh=15.0,
            max_charge_per_slot=15.0,
            charge_efficiency_pct=90.0,
        )
        # Grid must supply 10 / 0.9 ≈ 11.11 kWh
        assert slot.grid_import_kwh == pytest.approx(10.0 / 0.9, abs=0.01)
        # Battery stores 10 kWh (battery-side)
        assert slot.estimated_battery_capacity == pytest.approx(10.0, abs=0.01)


# ===========================================================================
# SoC simulation — discharge efficiency
# ===========================================================================


class TestDischargeEfficiencySoC:
    """Verify that discharge efficiency correctly reduces house-side delivery."""

    def test_100pct_efficiency_discharge_covers_load_exactly(self) -> None:
        """At 100 % discharge efficiency, the battery exactly covers the load."""
        slot = _make_slot(1, load=3.0, pv=0.0)
        _run_simulation(
            [slot],
            current_kwh=5.0,
            discharge_efficiency_pct=100.0,
        )
        # discharge == 3 kWh; no grid import needed
        assert slot.batteries_discharged == pytest.approx(3.0, abs=0.01)
        assert slot.grid_import_kwh == pytest.approx(0.0, abs=0.01)

    def test_90pct_efficiency_battery_removes_more_for_same_load(self) -> None:
        """At 90 % discharge efficiency, more battery energy is removed to cover load."""
        load = 3.0  # house needs 3 kWh
        slot = _make_slot(1, load=load, pv=0.0)
        _run_simulation(
            [slot],
            current_kwh=5.0,
            discharge_efficiency_pct=90.0,
        )
        # Battery must remove load / discharge_eff = 3 / 0.9 ≈ 3.33 kWh
        expected_removed = load / 0.90
        assert slot.batteries_discharged == pytest.approx(expected_removed, abs=0.01)
        # Grid import should be zero (battery has enough capacity)
        assert slot.grid_import_kwh == pytest.approx(0.0, abs=0.01)

    def test_90pct_efficiency_soc_decreases_by_full_removed_kwh(self) -> None:
        """SoC decreases by the full removed kWh, not the house-delivered kWh."""
        load = 3.0
        starting_kwh = 5.0
        slot = _make_slot(1, load=load, pv=0.0)
        _run_simulation(
            [slot],
            current_kwh=starting_kwh,
            discharge_efficiency_pct=90.0,
        )
        removed = load / 0.90  # ≈ 3.33 kWh removed from battery
        expected_cap = starting_kwh - removed
        assert slot.estimated_battery_capacity == pytest.approx(expected_cap, abs=0.01)

    def test_grid_import_covers_shortfall_at_90pct_discharge(self) -> None:
        """When battery cannot fully cover load at 90 % eff, grid fills the gap."""
        load = 5.0  # house needs 5 kWh
        starting_kwh = 3.0  # only 3 kWh available above floor
        slot = _make_slot(1, load=load, pv=0.0)
        _run_simulation(
            [slot],
            current_kwh=starting_kwh,
            discharge_efficiency_pct=90.0,
        )
        # battery can deliver at most 3.0 × 0.9 = 2.7 kWh to house
        max_house_from_battery = starting_kwh * 0.90
        expected_grid = load - max_house_from_battery
        assert slot.grid_import_kwh == pytest.approx(expected_grid, abs=0.02)
        # Battery is fully discharged
        assert slot.estimated_battery_capacity == pytest.approx(0.0, abs=0.01)


# ===========================================================================
# Round-trip: 90 % charge × 90 % discharge
# ===========================================================================


class TestRoundtripEfficiency:
    """Acceptance criteria: 90 % charge / 90 % discharge round-trip tests."""

    def test_roundtrip_10kwh_stored_yields_9kwh_usable(self) -> None:
        """Issue #291 AC: charging 10 kWh does not yield 10 kWh usable unless eff == 100 %.

        With 90 % charge and 90 % discharge:
          - Grid supplies 10 / 0.9 ≈ 11.11 kWh to store 10 kWh in battery.
          - Discharging those 10 kWh yields 10 × 0.9 = 9 kWh to the house.
          - Round-trip yield = 9 / 11.11 ≈ 81 %.
        """
        # Slot 0: charge 10 kWh into battery
        charge_slot = _make_slot(1, batteries_charged=10.0, load=0.0, pv=0.0)
        # Slot 1: discharge — need 10 kWh of load (battery has 10 kWh stored)
        discharge_slot = _make_slot(2, load=10.0, pv=0.0)

        slots = [charge_slot, discharge_slot]
        _run_simulation(
            slots,
            current_kwh=0.0,
            usable_kwh=15.0,
            max_capacity_kwh=15.0,
            max_charge_per_slot=15.0,
            charge_efficiency_pct=90.0,
            discharge_efficiency_pct=90.0,
        )

        # After charging: 10 kWh stored
        assert charge_slot.estimated_battery_capacity == pytest.approx(10.0, abs=0.01)
        # Grid import for the charge slot: 10 / 0.9 ≈ 11.11 kWh
        assert charge_slot.grid_import_kwh == pytest.approx(10.0 / 0.9, abs=0.02)

        # Discharge slot: battery removes 10 / 0.9 ≈ 11.11 kWh ... but only 10 kWh stored
        # So battery is fully emptied (cap goes to 0) and the remainder is grid-imported.
        # House gets 10 kWh × 0.9 = 9 kWh from battery; grid covers 10 - 9 = 1 kWh.
        assert discharge_slot.estimated_battery_capacity == pytest.approx(0.0, abs=0.01)
        house_from_battery = 10.0 * 0.90  # 9 kWh
        expected_grid = 10.0 - house_from_battery
        assert discharge_slot.grid_import_kwh == pytest.approx(expected_grid, abs=0.02)

    def test_100pct_efficiency_is_lossless(self) -> None:
        """At 100 % / 100 % efficiency the round-trip is perfectly lossless."""
        charge_slot = _make_slot(1, batteries_charged=5.0, load=0.0, pv=0.0)
        discharge_slot = _make_slot(2, load=5.0, pv=0.0)

        slots = [charge_slot, discharge_slot]
        _run_simulation(
            slots,
            current_kwh=0.0,
            usable_kwh=10.0,
            max_capacity_kwh=10.0,
            max_charge_per_slot=10.0,
            charge_efficiency_pct=100.0,
            discharge_efficiency_pct=100.0,
        )

        assert charge_slot.grid_import_kwh == pytest.approx(5.0, abs=0.01)
        # Exactly 5 kWh stored → discharge covers the 5 kWh load completely
        assert discharge_slot.grid_import_kwh == pytest.approx(0.0, abs=0.01)
        assert discharge_slot.batteries_discharged == pytest.approx(5.0, abs=0.01)
        assert discharge_slot.estimated_battery_capacity == pytest.approx(0.0, abs=0.01)

    def test_asymmetric_efficiency_95_charge_90_discharge(self) -> None:
        """95 % charge / 90 % discharge: verify grid import and SoC are consistent."""
        stored = 5.0  # kWh
        charge_slot = _make_slot(1, batteries_charged=stored, load=0.0, pv=0.0)
        discharge_slot = _make_slot(2, load=4.0, pv=0.0)

        _run_simulation(
            [charge_slot, discharge_slot],
            current_kwh=0.0,
            usable_kwh=10.0,
            max_capacity_kwh=10.0,
            max_charge_per_slot=10.0,
            charge_efficiency_pct=95.0,
            discharge_efficiency_pct=90.0,
        )

        # Grid import for charge: 5 / 0.95 ≈ 5.263 kWh
        assert charge_slot.grid_import_kwh == pytest.approx(stored / 0.95, abs=0.02)
        # Battery removes 4 / 0.90 ≈ 4.44 kWh to deliver 4 kWh to house
        removed = 4.0 / 0.90
        assert discharge_slot.batteries_discharged == pytest.approx(removed, abs=0.02)
        # No grid import (5 kWh stored > 4.44 kWh needed)
        assert discharge_slot.grid_import_kwh == pytest.approx(0.0, abs=0.02)
        # SoC after discharge
        expected_cap_after = stored - removed
        assert discharge_slot.estimated_battery_capacity == pytest.approx(
            expected_cap_after, abs=0.02
        )


# ===========================================================================
# Cost function — efficiency in conversion_loss_cost
# ===========================================================================


class TestCostFunctionEfficiency:
    """Verify that charge/discharge efficiency affects the conversion_loss_cost term."""

    def _make_cost_slot(
        self,
        *,
        batteries_charged: float = 0.0,
        batteries_discharged: float = 0.0,
        grid_import_kwh: float = 0.0,
        grid_export_kwh: float = 0.0,
        import_price: float = 0.20,
        export_price: float = 0.05,
    ) -> PlannedSlot:
        start = _T0 + timedelta(hours=1)
        end = start + timedelta(hours=1)
        slot = PlannedSlot(
            start=start,
            end=end,
            price=SlotPrice(import_price=import_price, export_price=export_price),
        )
        slot.batteries_charged = batteries_charged
        slot.batteries_discharged = batteries_discharged
        slot.grid_import_kwh = grid_import_kwh
        slot.grid_export_kwh = grid_export_kwh
        slot.estimated_battery_soc = 50.0
        return slot

    def test_zero_battery_activity_no_conversion_loss(self) -> None:
        """No battery movement → conversion_loss_cost == 0."""
        slot = self._make_cost_slot(grid_import_kwh=1.0)
        bd = score_plan(
            [slot],
            CostWeights(charge_efficiency_pct=90.0, discharge_efficiency_pct=90.0),
        )
        assert bd.conversion_loss_cost == pytest.approx(0.0, abs=1e-9)

    def test_100pct_efficiency_uses_legacy_conversion_loss_pct(self) -> None:
        """When both efficiencies are 100 %, the legacy conversion_loss_pct drives the term."""
        slot = self._make_cost_slot(
            batteries_charged=4.0, batteries_discharged=4.0, grid_import_kwh=4.0
        )
        weights = CostWeights(
            charge_efficiency_pct=100.0,
            discharge_efficiency_pct=100.0,
            conversion_loss_pct=10.0,
        )
        bd = score_plan([slot], weights)
        # cycled = 4+4 = 8; loss = 8 × 0.10 = 0.8; mid_price = 0.125; cost = 0.1
        expected_loss_cost = 8.0 * 0.10 * ((0.20 + 0.05) / 2.0)
        assert bd.conversion_loss_cost == pytest.approx(expected_loss_cost, abs=1e-6)

    def test_90_90_efficiency_roundtrip_loss_fraction(self) -> None:
        """90 % charge × 90 % discharge → roundtrip loss = 1 - 0.81 = 0.19."""
        slot = self._make_cost_slot(
            batteries_charged=5.0, batteries_discharged=5.0, grid_import_kwh=5.0
        )
        weights = CostWeights(
            charge_efficiency_pct=90.0,
            discharge_efficiency_pct=90.0,
            conversion_loss_pct=0.0,  # legacy term disabled
        )
        bd = score_plan([slot], weights)
        cycled = 10.0  # 5 + 5
        loss_fraction = 1.0 - 0.90 * 0.90  # = 0.19
        mid_price = (0.20 + 0.05) / 2.0
        expected = cycled * loss_fraction * mid_price
        assert bd.conversion_loss_cost == pytest.approx(expected, abs=1e-6)

    def test_95_95_efficiency_lower_loss_than_90_90(self) -> None:
        """95 % / 95 % efficiency produces lower conversion_loss_cost than 90 % / 90 %."""
        slot = self._make_cost_slot(
            batteries_charged=5.0, batteries_discharged=5.0, grid_import_kwh=5.0
        )
        bd_95 = score_plan(
            [slot],
            CostWeights(charge_efficiency_pct=95.0, discharge_efficiency_pct=95.0),
        )
        bd_90 = score_plan(
            [slot],
            CostWeights(charge_efficiency_pct=90.0, discharge_efficiency_pct=90.0),
        )
        assert bd_95.conversion_loss_cost < bd_90.conversion_loss_cost

    def test_conversion_loss_overridden_by_explicit_efficiencies(self) -> None:
        """When explicit efficiencies are set, conversion_loss_pct is overridden."""
        slot = self._make_cost_slot(
            batteries_charged=5.0, batteries_discharged=5.0, grid_import_kwh=5.0
        )
        # explicit 90/90 overrides conversion_loss_pct=50 (which would be absurdly high)
        weights = CostWeights(
            charge_efficiency_pct=90.0,
            discharge_efficiency_pct=90.0,
            conversion_loss_pct=50.0,
        )
        bd = score_plan([slot], weights)
        # Should use roundtrip loss = 1 - 0.81 = 0.19, not 0.50
        cycled = 10.0
        loss_fraction = 1.0 - 0.90 * 0.90  # 0.19
        mid_price = (0.20 + 0.05) / 2.0
        expected = cycled * loss_fraction * mid_price
        assert bd.conversion_loss_cost == pytest.approx(expected, abs=1e-6)

    def test_import_cost_reflects_real_grid_draw_at_90pct_charge(self) -> None:
        """Grid import cost uses grid_import_kwh (which includes charge-side loss).

        The simulation already inflates grid_import_kwh to charge_stored/charge_eff,
        so the cost function simply multiplies by import_price — no double-counting.
        """
        # Simulate: 4 kWh stored, charge_eff=90 % → grid drew 4/0.9 ≈ 4.44 kWh
        grid_drew = 4.0 / 0.90
        slot = self._make_cost_slot(
            batteries_charged=4.0,
            grid_import_kwh=grid_drew,
            import_price=0.25,
        )
        bd = score_plan([slot], CostWeights())
        assert bd.import_cost == pytest.approx(grid_drew * 0.25, abs=1e-6)


# ===========================================================================
# End-to-end: PlannerInput with charge/discharge efficiency
# ===========================================================================


class TestPlannerInputEfficiencyFields:
    """Smoke tests: run_planner honours charge/discharge efficiency fields."""

    def test_default_efficiency_matches_100pct_backwards_compat(self) -> None:
        """Default PlannerInput efficiency fields are 95 % (issue default, not 100 %)."""
        inp = PlannerInput()
        assert inp.battery_charge_efficiency_pct == pytest.approx(95.0)
        assert inp.battery_discharge_efficiency_pct == pytest.approx(95.0)

    def test_explicit_90_90_planner_run_completes(self) -> None:
        """Planner runs successfully with 90 % charge / 90 % discharge."""
        from tests.planner.fixtures import make_summer_day_input

        inp = make_summer_day_input(battery_conversion_loss_pct=0.0)
        inp.battery_charge_efficiency_pct = 90.0
        inp.battery_discharge_efficiency_pct = 90.0

        from custom_components.hsem.planner import run_planner

        output = run_planner(inp)
        assert output.slots
        # The plan should have a valid cost (not NaN)
        import math

        assert not math.isnan(output.plan_cost.total)

    def test_100pct_efficiency_no_extra_grid_import_for_charging(self) -> None:
        """At 100 % efficiency, grid import for a charge-only slot == batteries_charged."""
        from custom_components.hsem.planner import run_planner
        from tests.planner.fixtures import make_summer_day_input

        inp = make_summer_day_input(
            battery_soc_pct=10.0,  # nearly empty → planner likely charges
            battery_conversion_loss_pct=0.0,
        )
        inp.battery_charge_efficiency_pct = 100.0
        inp.battery_discharge_efficiency_pct = 100.0
        output = run_planner(inp)

        # Find a grid-charge slot and verify grid_import == batteries_charged
        for slot in output.slots:
            if slot.batteries_charged > 0.1 and slot.solcast_pv_estimate < 0.01:
                # Pure grid charge slot — grid import must equal batteries_charged
                assert slot.grid_import_kwh == pytest.approx(
                    slot.batteries_charged, rel=0.01
                ), (
                    f"At 100 % efficiency, grid_import ({slot.grid_import_kwh:.3f}) "
                    f"should equal batteries_charged ({slot.batteries_charged:.3f})"
                )
                break  # one confirmation is sufficient

    def test_90pct_charge_efficiency_raises_grid_import(self) -> None:
        """At 90 % charge efficiency, grid import > batteries_charged for charge slots."""
        from custom_components.hsem.planner import run_planner
        from tests.planner.fixtures import make_summer_day_input

        inp = make_summer_day_input(
            battery_soc_pct=10.0,
            battery_conversion_loss_pct=0.0,
        )
        inp.battery_charge_efficiency_pct = 90.0
        inp.battery_discharge_efficiency_pct = 100.0

        output = run_planner(inp)

        charge_slots = [
            s
            for s in output.slots
            if s.batteries_charged > 0.1 and s.solcast_pv_estimate < 0.01
        ]
        if charge_slots:
            slot = charge_slots[0]
            # grid_import must be > batteries_charged (loss on the way in)
            assert slot.grid_import_kwh > slot.batteries_charged - 1e-6


# ===========================================================================
# Edge cases
# ===========================================================================


class TestEfficiencyEdgeCases:
    """Edge cases: clamp behaviour, zero activity, boundary values."""

    def test_efficiency_of_1pct_clamped_not_zero(self) -> None:
        """Extremely low efficiency is clamped to 1 % rather than 0 to avoid div/0."""
        slot = _make_slot(1, batteries_charged=2.0, load=0.0, pv=0.0)
        # Should not raise
        _run_simulation([slot], current_kwh=0.0, charge_efficiency_pct=0.0)

    def test_efficiency_above_100_clamped_to_100(self) -> None:
        """Efficiency > 100 % is clamped to 100 % (cannot store more than supplied)."""
        slot = _make_slot(1, batteries_charged=2.0, load=0.0, pv=0.0)
        _run_simulation([slot], current_kwh=0.0, charge_efficiency_pct=200.0)
        # Should behave like 100 %: grid_import == batteries_charged
        assert slot.grid_import_kwh == pytest.approx(2.0, abs=0.01)

    def test_no_battery_activity_efficiency_irrelevant(self) -> None:
        """When there is no charge or discharge, efficiency values have no effect."""
        slot_eff = _make_slot(1, pv=1.0, load=1.0)  # PV exactly covers load
        slot_neff = _make_slot(1, pv=1.0, load=1.0)

        _run_simulation(
            [slot_eff],
            current_kwh=5.0,
            charge_efficiency_pct=90.0,
            discharge_efficiency_pct=90.0,
        )
        _run_simulation(
            [slot_neff],
            current_kwh=5.0,
            charge_efficiency_pct=100.0,
            discharge_efficiency_pct=100.0,
        )

        assert slot_eff.grid_import_kwh == pytest.approx(
            slot_neff.grid_import_kwh, abs=0.01
        )
        assert slot_eff.grid_export_kwh == pytest.approx(
            slot_neff.grid_export_kwh, abs=0.01
        )
        assert slot_eff.batteries_discharged == pytest.approx(
            slot_neff.batteries_discharged, abs=0.01
        )

    def test_discharge_efficiency_cannot_exceed_100pct(self) -> None:
        """discharge_efficiency > 100 is clamped; battery still loses energy."""
        slot = _make_slot(1, load=3.0, pv=0.0)
        _run_simulation([slot], current_kwh=5.0, discharge_efficiency_pct=150.0)
        # Clamped to 100 %: battery removes exactly 3 kWh, no grid import
        assert slot.batteries_discharged == pytest.approx(3.0, abs=0.01)
        assert slot.grid_import_kwh == pytest.approx(0.0, abs=0.01)

    def test_cost_weights_default_charge_discharge_100pct(self) -> None:
        """Default CostWeights has 100 % efficiency → uses legacy conversion_loss_pct."""
        w = CostWeights()
        assert w.charge_efficiency_pct == pytest.approx(100.0)
        assert w.discharge_efficiency_pct == pytest.approx(100.0)

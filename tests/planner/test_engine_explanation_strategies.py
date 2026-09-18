"""Tests for the plan explanation's strategy labelling and rejected plans.

The explanation is the only place the user learns *why* HSEM picked a plan, so
each strategy label has to follow from the recommendations actually in the
horizon, and the do-nothing baseline has to stay honest when the chosen plan
is the more expensive one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.engine_explanation import (
    _build_explanation,
    _derive_windows,
)
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_SLOT = timedelta(hours=1)


def _slot(
    index: int,
    recommendation: str | None = None,
    *,
    import_price: float = 1.0,
    export_price: float = 0.5,
    net_kwh: float = 1.0,
    cost: float = 1.0,
    charged_kwh: float = 0.0,
    pv_kwh: float = 0.0,
) -> PlannedSlot:
    """Return a future slot carrying *recommendation* and its prices."""
    start = _NOW + index * _SLOT
    return PlannedSlot(
        start=start,
        end=start + _SLOT,
        recommendation=recommendation,
        price=SlotPrice(import_price, export_price),
        estimated_net_consumption_kwh=net_kwh,
        estimated_cost_currency=cost,
        batteries_charged_kwh=charged_kwh,
        solcast_pv_estimate_kwh=pv_kwh,
    )


def _inp(**overrides: object) -> PlannerInput:
    """Return planner inputs with *overrides* applied."""
    inp = PlannerInput()
    for key, value in overrides.items():
        setattr(inp, key, value)
    return inp


def _explain(slots: list[PlannedSlot], **overrides: object) -> PlanExplanation:
    """Build the explanation for *slots* at a fixed June timestamp."""
    return _build_explanation(_inp(**overrides), slots, 50.0, _NOW)


_GRID_CHARGE = Recommendations.BatteriesChargeGrid.value
_SOLAR_CHARGE = Recommendations.BatteriesChargeSolar.value
_DISCHARGE = Recommendations.BatteriesDischargeMode.value
_FORCE_DISCHARGE = Recommendations.ForceBatteriesDischarge.value
_FORCE_EXPORT = Recommendations.ForceExport.value


class TestStrategyLabel:
    """The label names the plan's dominant intent, in priority order."""

    def test_grid_charge_plus_discharge_is_arbitrage(self) -> None:
        """Charging cheap and discharging at peak is the arbitrage strategy."""
        expl = _explain([_slot(0, _GRID_CHARGE), _slot(1, _DISCHARGE)])

        assert expl.selected_strategy == "charge_grid_discharge_peak"
        assert "charged from the grid" in expl.summary

    def test_grid_charge_without_discharge_is_opportunistic(self) -> None:
        """Charging at a negative price with no discharge plan is opportunistic."""
        expl = _explain([_slot(0, _GRID_CHARGE, import_price=-0.2), _slot(1)])

        assert expl.selected_strategy == "opportunistic_charge"
        assert "no scheduled discharge window" in expl.summary

    def test_solar_charge_plus_discharge_is_labelled_separately(self) -> None:
        """Solar charging is not reported as grid arbitrage."""
        expl = _explain([_slot(0, _SOLAR_CHARGE, pv_kwh=3.0), _slot(1, _DISCHARGE)])

        assert expl.selected_strategy == "charge_solar_discharge_peak"
        assert "solar surplus" in expl.summary

    def test_a_forced_discharge_is_reported_as_an_export(self) -> None:
        """Forced discharge sends surplus capacity to the grid."""
        expl = _explain([_slot(0, _FORCE_DISCHARGE, export_price=3.0)])

        assert expl.selected_strategy == "force_export"
        assert "exported to the grid" in expl.summary

    def test_a_pv_export_is_distinguished_from_a_battery_export(self) -> None:
        """``force_export`` on PV surplus gets its own label."""
        expl = _explain([_slot(0, _FORCE_EXPORT, export_price=2.0)])

        assert expl.selected_strategy == "force_export_pv"
        assert "Export price exceeds import price" in expl.summary

    def test_discharge_without_any_charging_is_discharge_only(self) -> None:
        """No affordable charging slot leaves discharge as the only action."""
        expl = _explain([_slot(0, _DISCHARGE), _slot(1, _DISCHARGE)])

        assert expl.selected_strategy == "discharge_only"
        # The do-nothing baseline is not offered for a discharge-only plan.
        assert not any(p.name == "do_nothing" for p in expl.rejected_plans)

    def test_an_idle_winter_horizon_is_a_deliberate_wait(self) -> None:
        """In a winter month an idle battery is held in reserve, not broken."""
        expl = _explain([_slot(0), _slot(1)], months_winter=[6])

        assert expl.selected_strategy == "winter_wait"
        assert "held in reserve" in expl.summary
        assert "winter_month" in expl.constraints

    def test_an_idle_summer_horizon_waits_for_solar(self) -> None:
        """Outside winter an idle battery is waiting on PV."""
        expl = _explain([_slot(0, pv_kwh=4.0)], months_winter=[1, 12])

        assert expl.selected_strategy == "solar_charge_only"
        assert "summer_month" in expl.constraints


class TestConstraints:
    """Active constraints explain why the plan is as small as it is."""

    def test_battery_limits_are_reported(self) -> None:
        """A full, disabled or empty battery is named explicitly."""
        expl = _explain(
            [_slot(0, import_price=1.0), _slot(1, import_price=1.0)],
            battery_rated_capacity_kwh=0.0,
            battery_soc_pct=100.0,
            battery_max_soc_pct=100.0,
            excess_export_enabled=True,
        )

        assert "no_price_spread" in expl.constraints
        assert "battery_disabled" in expl.constraints
        assert "battery_full" in expl.constraints
        assert "excess_export_enabled" in expl.constraints

    def test_an_empty_battery_and_a_low_end_soc_are_reported(self) -> None:
        """An empty battery now and at the horizon end are separate facts."""
        inp = _inp(battery_soc_pct=10.0, battery_end_of_discharge_soc_pct=10.0)

        expl = _build_explanation(inp, [_slot(0)], 5.0, _NOW)

        assert "battery_empty" in expl.constraints
        assert "battery_low_at_end" in expl.constraints


class TestRejectedPlans:
    """The rejected alternatives always include an honest baseline."""

    def test_a_cheaper_plan_reports_its_savings(self) -> None:
        """Beating the idle baseline is reported as a saving."""
        expl = _explain([_slot(0, _GRID_CHARGE, net_kwh=2.0, cost=0.5)])

        do_nothing = next(p for p in expl.rejected_plans if p.name == "do_nothing")
        assert "saves" in do_nothing.reason

    def test_a_costlier_plan_admits_the_overhead(self) -> None:
        """Pre-charging that costs more than idling is not dressed up."""
        expl = _explain([_slot(0, _GRID_CHARGE, net_kwh=0.5, cost=3.0)])

        do_nothing = next(p for p in expl.rejected_plans if p.name == "do_nothing")
        assert "charging overhead" in do_nothing.reason

    def test_an_equal_cost_plan_cites_schedule_adherence(self) -> None:
        """A plan that changes nothing financially says so."""
        expl = _explain([_slot(0, _GRID_CHARGE, net_kwh=1.0, cost=1.0)])

        do_nothing = next(p for p in expl.rejected_plans if p.name == "do_nothing")
        assert "approximately equal" in do_nothing.reason

    def test_solar_charging_alone_is_offered_as_an_alternative(self) -> None:
        """A discharge plan fed by solar lists the charge-only alternative."""
        expl = _explain([_slot(0, _SOLAR_CHARGE, pv_kwh=5.0), _slot(1, _DISCHARGE)])

        assert any(p.name == "charge_only_solar" for p in expl.rejected_plans)

    def test_a_spread_below_the_depreciation_threshold_is_explained(self) -> None:
        """Grid charging skipped for a thin spread names the threshold."""
        expl = _explain(
            [_slot(0, import_price=1.0), _slot(1, import_price=1.001)],
            battery_purchase_price=50000.0,
            battery_expected_cycles=3000,
            months_winter=[6],
        )

        rejected = next(
            p for p in expl.rejected_plans if p.name == "grid_charge_rejected_spread"
        )
        assert "not profitable" in rejected.reason


class TestDeriveWindows:
    """Contiguous runs of the same intent become one window."""

    def test_adjacent_charge_slots_form_one_window(self) -> None:
        """Two cheap hours in a row are a single charge window."""
        slots = [
            _slot(0, _GRID_CHARGE, import_price=0.5, charged_kwh=2.0),
            _slot(1, _GRID_CHARGE, import_price=0.7, charged_kwh=3.0),
            _slot(2),
        ]

        charge, discharge = _derive_windows(slots)

        assert len(charge) == 1
        assert charge[0].total_energy_kwh == pytest.approx(5.0)
        assert charge[0].avg_import_price == pytest.approx(0.6)
        assert discharge == []

    def test_a_charge_run_broken_by_a_discharge_closes_the_window(self) -> None:
        """Switching intent ends the open window immediately."""
        slots = [
            _slot(0, _GRID_CHARGE, charged_kwh=1.0),
            _slot(1, _DISCHARGE, import_price=2.0),
            _slot(2, _GRID_CHARGE, charged_kwh=1.0),
        ]

        charge, discharge = _derive_windows(slots)

        assert len(charge) == 2
        assert len(discharge) == 1
        assert discharge[0].avg_import_price == pytest.approx(2.0)

    def test_an_idle_slot_after_a_discharge_run_closes_the_window(self) -> None:
        """Returning to idle ends the discharge window at the last active slot."""
        slots = [
            _slot(0, _DISCHARGE, import_price=2.0),
            _slot(1),
            _slot(2, _DISCHARGE, import_price=3.0),
        ]

        charge, discharge = _derive_windows(slots)

        assert charge == []
        assert len(discharge) == 2
        assert discharge[0].end == slots[0].end

    def test_a_trailing_discharge_run_is_flushed(self) -> None:
        """A window still open at the end of the horizon is still reported."""
        slots = [_slot(0), _slot(1, _DISCHARGE), _slot(2, _DISCHARGE)]

        charge, discharge = _derive_windows(slots)

        assert charge == []
        assert len(discharge) == 1
        assert discharge[0].end == slots[2].end

    def test_an_empty_horizon_has_no_windows(self) -> None:
        """No slots means no windows to describe."""
        assert _derive_windows([]) == ([], [])

"""Plan self-consistency gate — label ⇒ energy contracts (issue #1035).

A slot's recommendation and its energy fields must agree.  The rule was
violated three times in one week (issues #989, #1026, #1032) and each violation
had to be found by a human.  These tests cover the mechanical check that
replaces that.

Structure:

1. Contract completeness — a new ``Recommendations`` member without a contract
   fails here, which is the whole point of centralising the table.
2. The checker itself, per label, on synthetic slots.
3. The two contracts issue #1035 required to be decided explicitly:
   ``force_export`` and ``ev_smart_charging``.
4. Reporting behaviour — warnings and diagnostics, never raise, never correct.
5. End-to-end: zero violations across the stock fixtures × starting SoC.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.cost_function import CostWeights, score_plan
from custom_components.hsem.planner.discharge_scheduler import (
    apply_optimization_strategy,
)
from custom_components.hsem.planner.engine_core import _label_commanded_ev_slots
from custom_components.hsem.planner.plan_consistency import (
    check_plan_self_consistency,
    consistency_warning,
)
from custom_components.hsem.planner.soc_simulation import simulate_soc
from custom_components.hsem.utils.diagnostics import build_diagnostics_dump
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import (
    LABEL_ENERGY_CONTRACTS,
    EnergyExpectation,
    Recommendations,
)
from tests.planner.fixtures import (
    make_flat_price_input,
    make_negative_price_input,
    make_summer_day_input,
    make_winter_day_input,
)

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)


def _cost_weights_for(inp: PlannerInput) -> CostWeights:
    """Mirror the weights the engine scores with, as test_invariants.py does."""
    return CostWeights(
        min_soc_pct=inp.battery_end_of_discharge_soc_pct,
        max_soc_pct=inp.battery_max_soc_pct,
        battery_purchase_price=inp.battery_purchase_price,
        battery_rated_capacity_kwh=inp.battery_rated_capacity_kwh,
        battery_expected_cycles=inp.battery_expected_cycles,
        charge_efficiency_pct=inp.battery_charge_efficiency_pct,
        discharge_efficiency_pct=inp.battery_discharge_efficiency_pct,
    )


def _slot(
    *,
    hour: int = 0,
    recommendation: str | None = None,
    charged: float = 0.0,
    discharged: float = 0.0,
    load: float = 1.0,
    pv: float = 0.0,
    import_price: float = 0.20,
    export_price: float = 0.05,
) -> PlannedSlot:
    """Build one synthetic slot with explicit label and energy fields."""
    start = _NOW + timedelta(hours=hour)
    slot = PlannedSlot(start=start, end=start + timedelta(hours=1))
    slot.recommendation = recommendation
    slot.batteries_charged_kwh = charged
    slot.batteries_discharged_kwh = discharged
    slot.avg_house_consumption_kwh = load
    slot.solcast_pv_estimate_kwh = pv
    slot.estimated_net_consumption_kwh = load - pv
    slot.price = SlotPrice(import_price=import_price, export_price=export_price)
    return slot


# ===========================================================================
# 1. Contract completeness
# ===========================================================================


class TestContractCompleteness:
    """Every label must carry an explicit decision about its energy."""

    def test_every_recommendation_has_a_contract(self) -> None:
        """A new Recommendations member must come with a contract.

        This is the test issue #1035 asks for: adding an enum member without
        deciding its energy contract fails here rather than silently defaulting
        to "unchecked".
        """
        missing = [
            member.name
            for member in Recommendations
            if member not in LABEL_ENERGY_CONTRACTS
        ]
        assert missing == [], (
            f"Recommendations member(s) {missing} have no entry in "
            f"LABEL_ENERGY_CONTRACTS. Add one to "
            f"custom_components/hsem/utils/recommendations.py — including an "
            f"explicit UNCONSTRAINED with a rationale if the label genuinely "
            f"guarantees nothing about battery energy."
        )

    def test_no_contract_for_unknown_member(self) -> None:
        """The table must not outlive a removed enum member."""
        extra = [
            member
            for member in LABEL_ENERGY_CONTRACTS
            if member not in set(Recommendations)
        ]
        assert extra == []

    def test_every_contract_states_a_rationale(self) -> None:
        """A contract without a reason is a guess; UNCONSTRAINED especially."""
        for member, contract in LABEL_ENERGY_CONTRACTS.items():
            assert contract.rationale.strip(), f"{member.name} has no rationale"

    def test_unconstrained_is_documented_as_a_decision(self) -> None:
        """Both 'no guarantee' contracts must say so in the rationale."""
        for member, contract in LABEL_ENERGY_CONTRACTS.items():
            if member in (
                Recommendations.TimePassed,
                Recommendations.MissingInputEntities,
            ):
                continue
            if EnergyExpectation.UNCONSTRAINED in (contract.charge, contract.discharge):
                assert "DECIDED" in contract.rationale, (
                    f"{member.name} is UNCONSTRAINED but its rationale does not "
                    f"record that as a decision"
                )


# ===========================================================================
# 2. The checker, per label
# ===========================================================================


class TestCheckerDetectsViolations:
    """The three shipped bugs, plus the converse of each."""

    @pytest.mark.parametrize(
        "label",
        [
            Recommendations.BatteriesChargeGrid.value,
            Recommendations.BatteriesChargeSolar.value,
        ],
    )
    def test_charge_label_with_zero_energy_is_reported(self, label: str) -> None:
        """Issue #989: a charge label that stores nothing."""
        violations = check_plan_self_consistency([_slot(recommendation=label)])
        assert len(violations) == 1
        assert "batteries_charged_kwh" in violations[0]
        assert label in violations[0]

    @pytest.mark.parametrize(
        "label",
        [
            Recommendations.BatteriesDischargeMode.value,
            Recommendations.ForceBatteriesDischarge.value,
        ],
    )
    def test_discharge_label_with_zero_energy_is_reported(self, label: str) -> None:
        """Issue #1026: a discharge label that dispatches nothing."""
        violations = check_plan_self_consistency([_slot(recommendation=label)])
        assert len(violations) == 1
        assert "batteries_discharged_kwh" in violations[0]

    def test_wait_label_with_discharge_is_reported(self) -> None:
        """Issue #1032: a wait slot that still accounts for discharge."""
        violations = check_plan_self_consistency(
            [
                _slot(
                    recommendation=Recommendations.BatteriesWaitMode.value,
                    discharged=1.5,
                )
            ]
        )
        assert len(violations) == 1
        assert "batteries_discharged_kwh=1.500" in violations[0]

    def test_wait_label_with_charge_is_reported(self) -> None:
        """Strict Wait executes as 0 W, so charge contradicts it too."""
        violations = check_plan_self_consistency(
            [
                _slot(
                    recommendation=Recommendations.BatteriesWaitMode.value,
                    charged=1.5,
                )
            ]
        )
        assert len(violations) == 1
        assert "batteries_charged_kwh=1.500" in violations[0]

    def test_window_label_with_discharge_is_reported(self) -> None:
        """The window label means the plan holds the battery."""
        violations = check_plan_self_consistency(
            [
                _slot(
                    recommendation=(Recommendations.BatteriesDischargeWindowMode.value),
                    discharged=2.0,
                )
            ]
        )
        assert len(violations) == 1

    def test_charge_label_carrying_discharge_is_reported(self) -> None:
        """A charge slot must not also dispatch the battery."""
        violations = check_plan_self_consistency(
            [
                _slot(
                    recommendation=Recommendations.BatteriesChargeGrid.value,
                    charged=1.0,
                    discharged=1.0,
                )
            ]
        )
        assert len(violations) == 1
        assert "expected zero" in violations[0]

    def test_both_fields_wrong_reported_once_with_both_reasons(self) -> None:
        """One slot yields one entry, naming every field that broke."""
        violations = check_plan_self_consistency(
            [
                _slot(
                    recommendation=Recommendations.BatteriesDischargeMode.value,
                    charged=1.0,
                    discharged=0.0,
                )
            ]
        )
        assert len(violations) == 1
        assert "batteries_charged_kwh" in violations[0]
        assert "batteries_discharged_kwh" in violations[0]


class TestCheckerAcceptsValidSlots:
    """Correct slots must never be reported."""

    @pytest.mark.parametrize(
        ("label", "charged", "discharged"),
        [
            (Recommendations.BatteriesChargeGrid.value, 1.0, 0.0),
            (Recommendations.BatteriesChargeSolar.value, 1.0, 0.0),
            (Recommendations.BatteriesDischargeMode.value, 0.0, 1.0),
            (Recommendations.ForceBatteriesDischarge.value, 0.0, 1.0),
            (Recommendations.ForceExport.value, 0.0, 1.0),
            (Recommendations.BatteriesDischargeWindowMode.value, 0.0, 0.0),
            (Recommendations.BatteriesWaitMode.value, 0.0, 0.0),
        ],
    )
    def test_conforming_slot_is_clean(
        self, label: str, charged: float, discharged: float
    ) -> None:
        assert (
            check_plan_self_consistency(
                [_slot(recommendation=label, charged=charged, discharged=discharged)]
            )
            == []
        )

    @pytest.mark.parametrize(
        "label",
        [
            Recommendations.TimePassed.value,
            Recommendations.MissingInputEntities.value,
        ],
    )
    def test_sentinels_are_exempt(self, label: str) -> None:
        """A past slot is a record of what happened, not a promise."""
        assert (
            check_plan_self_consistency(
                [_slot(recommendation=label, charged=2.0, discharged=3.0)]
            )
            == []
        )

    def test_unlabelled_slot_is_skipped(self) -> None:
        """``recommendation is None`` predates labelling — nothing to check."""
        assert check_plan_self_consistency([_slot(recommendation=None)]) == []

    def test_energy_at_the_epsilon_is_not_material(self) -> None:
        """The check uses the same 1e-9 threshold as the guards it verifies."""
        assert (
            check_plan_self_consistency(
                [
                    _slot(
                        recommendation=Recommendations.BatteriesWaitMode.value,
                        discharged=1e-9,
                    )
                ]
            )
            == []
        )

    def test_empty_plan_is_clean(self) -> None:
        assert check_plan_self_consistency([]) == []


# ===========================================================================
# 3. The two contracts issue #1035 required to be decided
# ===========================================================================


class TestForceExportContract:
    """DECIDED: material discharge, zero charge — enforced by simulate_soc."""

    def _force_export_slots(self, count: int) -> list[PlannedSlot]:
        return [
            _slot(hour=h, load=1.0, pv=0.0, import_price=0.10, export_price=0.50)
            for h in range(count)
        ]

    def test_seasonal_fill_produces_force_export(self) -> None:
        """The label is genuinely reachable in a plan, not dead."""
        slots = self._force_export_slots(3)
        apply_optimization_strategy(
            slots,
            _NOW,
            current_capacity=9.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 10, 11, 12],
            export_min_price=0.0,
        )
        assert all(s.recommendation == Recommendations.ForceExport.value for s in slots)

    def test_surviving_force_export_slot_dispatches_the_battery(self) -> None:
        """A published force_export slot carries material discharge.

        The enum docstring's "battery unchanged (may still charge/discharge per
        schedule)" describes the *applier* — FullyFedToGrid re-routes PV — but
        the planner's own simulation dispatches the battery at max rate for
        this label.  That is the behaviour the contract pins down.
        """
        slots = self._force_export_slots(3)
        apply_optimization_strategy(
            slots,
            _NOW,
            current_capacity=9.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 10, 11, 12],
            export_min_price=0.0,
        )
        simulate_soc(
            slots,
            _NOW,
            current_kwh=9.0,
            usable_kwh=9.0,
            max_capacity_kwh=9.0,
            max_charge_per_slot=1.25,
            max_discharge_per_slot=None,
            rated_kwh=10.0,
            end_of_discharge_soc_pct=10.0,
        )
        surviving = [
            s for s in slots if s.recommendation == Recommendations.ForceExport.value
        ]
        assert surviving, "expected at least one surviving force_export slot"
        for slot in surviving:
            assert slot.batteries_discharged_kwh > 1e-9
            assert slot.batteries_charged_kwh == pytest.approx(0.0, abs=1e-9)
        assert check_plan_self_consistency(slots) == []

    def test_force_export_with_empty_battery_is_cleared_not_published(self) -> None:
        """A zero-discharge force_export cannot reach a published plan.

        ``simulate_soc`` clears it to wait mode, which is why MATERIAL is the
        honest contract rather than UNCONSTRAINED.
        """
        slots = self._force_export_slots(3)
        apply_optimization_strategy(
            slots,
            _NOW,
            current_capacity=0.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 10, 11, 12],
            export_min_price=0.0,
        )
        simulate_soc(
            slots,
            _NOW,
            current_kwh=0.0,
            usable_kwh=9.0,
            max_capacity_kwh=9.0,
            max_charge_per_slot=0.0,
            max_discharge_per_slot=None,
            rated_kwh=10.0,
            end_of_discharge_soc_pct=10.0,
        )
        assert all(
            s.recommendation == Recommendations.BatteriesWaitMode.value for s in slots
        )
        assert check_plan_self_consistency(slots) == []

    def test_contract_is_material_discharge_and_zero_charge(self) -> None:
        """Guard the decision itself against a silent future edit."""
        contract = LABEL_ENERGY_CONTRACTS[Recommendations.ForceExport]
        assert contract.discharge is EnergyExpectation.MATERIAL
        assert contract.charge is EnergyExpectation.ZERO


class TestEvSmartChargingContract:
    """DECIDED: no battery guarantee in either direction."""

    def test_contract_is_unconstrained(self) -> None:
        contract = LABEL_ENERGY_CONTRACTS[Recommendations.EVSmartCharging]
        assert contract.charge is EnergyExpectation.UNCONSTRAINED
        assert contract.discharge is EnergyExpectation.UNCONSTRAINED

    def test_relabel_overwrites_a_discharging_slot(self) -> None:
        """Why the contract must be UNCONSTRAINED, demonstrated.

        ``_label_commanded_ev_slots`` runs *after* the SoC simulation and
        overwrites the label of a slot that is actively discharging.  Any
        non-trivial contract would therefore report a slot that is behaving
        exactly as designed (issue #862).
        """
        slot = _slot(
            recommendation=Recommendations.BatteriesDischargeMode.value,
            discharged=2.0,
        )
        slot.ev_charger_calculated_power = 3680.0
        _label_commanded_ev_slots([slot])
        assert slot.recommendation == Recommendations.EVSmartCharging.value
        assert slot.batteries_discharged_kwh == pytest.approx(2.0)
        assert check_plan_self_consistency([slot]) == []

    @pytest.mark.parametrize(
        ("charged", "discharged"),
        [(0.0, 0.0), (2.0, 0.0), (0.0, 2.0)],
    )
    def test_any_energy_combination_is_accepted(
        self, charged: float, discharged: float
    ) -> None:
        """The label says HSEM commands a charger, not what the battery does."""
        assert (
            check_plan_self_consistency(
                [
                    _slot(
                        recommendation=Recommendations.EVSmartCharging.value,
                        charged=charged,
                        discharged=discharged,
                    )
                ]
            )
            == []
        )


# ===========================================================================
# 4. Reporting behaviour — never raise, never auto-correct
# ===========================================================================


class TestReportingBehaviour:
    """A violation is an HSEM bug, not a reason to stop controlling a battery."""

    def _bad_slot(self) -> PlannedSlot:
        return _slot(
            recommendation=Recommendations.BatteriesWaitMode.value, discharged=1.5
        )

    def test_check_does_not_mutate_slots(self) -> None:
        """No auto-correction — the fix issue #1033 rejected."""
        slot = self._bad_slot()
        check_plan_self_consistency([slot])
        assert slot.recommendation == Recommendations.BatteriesWaitMode.value
        assert slot.batteries_discharged_kwh == pytest.approx(1.5)

    def test_check_never_raises_on_malformed_input(self) -> None:
        """An unknown label is skipped, not raised on."""
        slot = _slot(recommendation="some_future_label", discharged=5.0)
        assert check_plan_self_consistency([slot]) == []

    def test_warning_names_the_count_and_offending_slots(self) -> None:
        violations = check_plan_self_consistency([self._bad_slot()])
        message = consistency_warning(violations)
        assert "1 slot(s)" in message
        assert Recommendations.BatteriesWaitMode.value in message

    def test_warning_truncates_long_lists(self) -> None:
        violations = check_plan_self_consistency(
            [
                _slot(
                    hour=h,
                    recommendation=Recommendations.BatteriesWaitMode.value,
                    discharged=1.0,
                )
                for h in range(10)
            ]
        )
        assert len(violations) == 10
        assert "+7 more" in consistency_warning(violations)

    def test_clean_plan_leaves_output_fields_empty(self) -> None:
        """A correct plan publishes no violations and no extra warning."""
        output = run_planner(make_summer_day_input())
        assert output.plan_consistency_violations == []
        assert not any("self-consistency" in w for w in output.warnings)

    def test_diagnostics_dump_always_carries_the_key(self) -> None:
        """A clean plan must be visibly clean in a dump (issue #1035)."""
        inp = make_summer_day_input()
        output = run_planner(inp)
        dump = build_diagnostics_dump(inp, output)
        assert dump["planner_output"]["plan_consistency_violations"] == []

    def test_diagnostics_dump_reports_injected_violations(self) -> None:
        """The dump surfaces violations, so a user report carries them."""
        inp = make_summer_day_input()
        output = run_planner(inp)
        output.plan_consistency_violations = ["2024-06-15T00:00:00 wait: bad"]
        dump = build_diagnostics_dump(inp, output)
        assert dump["planner_output"]["plan_consistency_violations"] == [
            "2024-06-15T00:00:00 wait: bad"
        ]


# ===========================================================================
# 5. End-to-end — zero violations across the stock fixtures
# ===========================================================================


class TestSelectedPlanIsSelfConsistent:
    """The gate runs on the winner, so assert on full planner output."""

    @pytest.mark.parametrize(
        "build",
        [
            make_summer_day_input,
            make_winter_day_input,
            make_flat_price_input,
            make_negative_price_input,
        ],
    )
    @pytest.mark.parametrize("soc_pct", [0.0, 5.0, 10.0, 25.0, 50.0, 75.0, 95.0, 100.0])
    def test_no_violations_across_fixtures_and_soc(
        self, build: Callable[[], PlannerInput], soc_pct: float
    ) -> None:
        """Parametrized over starting SoC, as the #1026/#1032 invariants are."""
        inp = build()
        inp.battery_soc_pct = soc_pct
        output = run_planner(inp)
        assert output.plan_consistency_violations == [], (
            f"Selected plan violates its own label/energy contract: "
            f"{output.plan_consistency_violations[:5]}"
        )

    @pytest.mark.parametrize("soc_pct", [0.0, 25.0, 50.0, 100.0])
    def test_no_violations_with_export_above_import(self, soc_pct: float) -> None:
        """The price shape that drives the plan toward export labels."""
        inp = make_summer_day_input()
        inp.battery_soc_pct = soc_pct
        inp.excess_export_enabled = True
        inp.price_points = [
            PricePoint(hour=p.hour, import_price=0.10, export_price=0.40)
            for p in inp.price_points
        ]
        output = run_planner(inp)
        assert output.plan_consistency_violations == []

    def test_gate_does_not_disturb_the_plan(self) -> None:
        """Reporting must not move the winner's slots or its cost.

        The gate is a pure read, so the published slots must still be the
        winner's own list and ``plan_cost`` must still describe them.
        """
        inp = make_summer_day_input()
        output = run_planner(inp)
        assert output.plan_cost is not None
        winner = next(c for c in output.candidates if c.name == output.winner_name)
        assert winner.slots is output.slots
        fresh = score_plan(
            output.slots,
            _cost_weights_for(inp),
            slot_duration_hours=1.0,
        )
        assert fresh.total_cost == pytest.approx(output.plan_cost.total_cost, abs=1e-6)

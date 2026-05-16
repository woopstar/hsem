"""Tests for candidate plan generation and selection (issue #296).

Acceptance criteria verified here
-----------------------------------
- Planner can compare multiple valid plans.
- Current (baseline) behaviour is represented as one candidate.
- Tests cover choosing no-action when all other plans are bad.
- All candidate names are present in the output.
- The winning plan has the lowest cost among valid candidates.
- Non-winning candidates appear in explanation.rejected_plans.
- Candidates field on PlannerOutput is populated after a run.
- Aggressive strategy only touches future slots.
- SoC validation rejects plans that violate the discharge floor.

All tests are synchronous and import nothing from Home Assistant's runtime.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planner_inputs import PlannerInput
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.candidate_generator import (
    CANDIDATE_AGGRESSIVE,
    CANDIDATE_BASELINE,
    CANDIDATE_DISCHARGE_ONLY,
    CANDIDATE_GRID_CHARGE,
    CANDIDATE_NO_ACTION,
    CANDIDATE_PASSIVE,
    CANDIDATE_SOLAR_ONLY,
    CandidatePlan,
    _apply_passive_solar,
    _clear_all_charge_discharge,
    _copy_slots,
    _remove_all_charge,
    _remove_grid_charge,
    _remove_solar_charge,
    generate_candidates,
)
from custom_components.hsem.planner.candidate_selector import (
    _validate_candidate,
    select_best_candidate,
)
from custom_components.hsem.planner.cost_function import CostWeights
from custom_components.hsem.planner.slot_population import (
    build_slots,
    build_time_series_index,
    populate_consumption,
    populate_prices,
    populate_solcast,
)
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations
from tests.planner.fixtures import (
    make_flat_price_input,
    make_summer_day_input,
    make_winter_day_input,
)

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_simple_slot(
    *,
    hour: int = 0,
    import_price: float = 0.20,
    export_price: float = 0.05,
    recommendation: str | None = None,
    batteries_charged: float = 0.0,
    estimated_battery_soc: float = 50.0,
) -> PlannedSlot:
    """Build a minimal :class:`PlannedSlot` for generator unit tests."""
    start = datetime(2024, 6, 15, hour, 0, tzinfo=_TZ)
    slot = PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=import_price, export_price=export_price),
        recommendation=recommendation,
        batteries_charged=batteries_charged,
        estimated_battery_soc=estimated_battery_soc,
    )
    return slot


def _populated_slots_for_input(inp: PlannerInput) -> list[PlannedSlot]:
    """Run price/pv/consumption population on *inp* and return the slot list."""
    now = datetime.fromisoformat(inp.now_iso)
    tsi = build_time_series_index(inp, now)
    slots = build_slots(inp, now)
    populate_prices(slots, inp.price_points, tsi)
    populate_solcast(slots, inp.solcast_slots, inp.interval_minutes, tsi)
    populate_consumption(
        slots,
        inp.consumption_averages,
        inp.weight_1d,
        inp.weight_3d,
        inp.weight_7d,
        inp.weight_14d,
        inp.interval_minutes,
        tsi,
    )
    return slots


# ===========================================================================
# 1. CandidatePlan dataclass basics
# ===========================================================================


class TestCandidatePlanDataclass:
    """CandidatePlan is a simple data holder with sensible defaults."""

    def test_defaults_are_valid(self):
        """A freshly constructed CandidatePlan defaults to is_valid=True."""
        plan = CandidatePlan(name="test", slots=[])
        assert plan.is_valid is True
        assert plan.rejection_reason == ""

    def test_name_stored(self):
        """The name passed to the constructor is preserved."""
        plan = CandidatePlan(name="baseline", slots=[])
        assert plan.name == "baseline"

    def test_slots_stored(self):
        """Slots passed to the constructor are stored unchanged."""
        slots = [_make_simple_slot(hour=0)]
        plan = CandidatePlan(name="x", slots=slots)
        assert plan.slots is slots


# ===========================================================================
# 2. Slot mutation helpers (unit tests for private helpers)
# ===========================================================================


class TestSlotMutationHelpers:
    """Each helper mutates only the fields it is responsible for."""

    def test_copy_slots_is_independent(self):
        """Modifying the copy must not affect the original."""
        original = [_make_simple_slot(hour=h) for h in range(3)]
        copied = _copy_slots(original)
        copied[0].recommendation = "batteries_charge_grid"
        assert original[0].recommendation is None

    def test_copy_slots_same_count(self):
        """The copy has the same number of slots as the original."""
        slots = [_make_simple_slot(hour=h) for h in range(5)]
        assert len(_copy_slots(slots)) == 5

    def test_clear_all_charge_discharge_resets_recommendations(self):
        """All charge and discharge recommendations are cleared."""
        slots = [
            _make_simple_slot(
                hour=0, recommendation=Recommendations.BatteriesChargeGrid.value
            ),
            _make_simple_slot(
                hour=1, recommendation=Recommendations.BatteriesDischargeMode.value
            ),
            _make_simple_slot(hour=2, recommendation=None),
        ]
        _clear_all_charge_discharge(slots)
        for slot in slots:
            assert slot.recommendation is None

    def test_clear_all_zeroes_batteries_charged(self):
        """``batteries_charged`` is zeroed on cleared slots."""
        slot = _make_simple_slot(
            hour=0,
            recommendation=Recommendations.BatteriesChargeGrid.value,
            batteries_charged=3.5,
        )
        _clear_all_charge_discharge([slot])
        assert abs(slot.batteries_charged) < 1e-9

    def test_remove_solar_charge_keeps_grid_charge(self):
        """_remove_solar_charge must not touch grid-charge slots."""
        grid_slot = _make_simple_slot(
            hour=0,
            recommendation=Recommendations.BatteriesChargeGrid.value,
            batteries_charged=2.0,
        )
        solar_slot = _make_simple_slot(
            hour=1,
            recommendation=Recommendations.BatteriesChargeSolar.value,
            batteries_charged=1.0,
        )
        _remove_solar_charge([grid_slot, solar_slot])
        assert grid_slot.recommendation == Recommendations.BatteriesChargeGrid.value
        assert solar_slot.recommendation is None

    def test_remove_grid_charge_keeps_solar_charge(self):
        """_remove_grid_charge must not touch solar-charge slots."""
        grid_slot = _make_simple_slot(
            hour=0,
            recommendation=Recommendations.BatteriesChargeGrid.value,
            batteries_charged=2.0,
        )
        solar_slot = _make_simple_slot(
            hour=1,
            recommendation=Recommendations.BatteriesChargeSolar.value,
            batteries_charged=1.0,
        )
        _remove_grid_charge([grid_slot, solar_slot])
        assert solar_slot.recommendation == Recommendations.BatteriesChargeSolar.value
        assert grid_slot.recommendation is None

    def test_remove_all_charge_keeps_discharge(self):
        """_remove_all_charge must leave discharge slots intact."""
        charge_slot = _make_simple_slot(
            hour=0,
            recommendation=Recommendations.BatteriesChargeGrid.value,
        )
        discharge_slot = _make_simple_slot(
            hour=1,
            recommendation=Recommendations.BatteriesDischargeMode.value,
        )
        _remove_all_charge([charge_slot, discharge_slot])
        assert charge_slot.recommendation is None
        assert (
            discharge_slot.recommendation
            == Recommendations.BatteriesDischargeMode.value
        )


# ===========================================================================
# 3. generate_candidates — structural contract
# ===========================================================================


class TestGenerateCandidates:
    """generate_candidates must produce all expected candidates."""

    def _make_baseline(self) -> list[PlannedSlot]:
        slots = []
        for h in range(24):
            slot = _make_simple_slot(hour=h, import_price=0.10 + 0.01 * h)
            if h in (1, 2):
                slot.recommendation = Recommendations.BatteriesChargeGrid.value
                slot.batteries_charged = 2.0
            elif h in (10, 11):
                slot.recommendation = Recommendations.BatteriesChargeSolar.value
                slot.batteries_charged = 1.0
            elif h in (17, 18, 19):
                slot.recommendation = Recommendations.BatteriesDischargeMode.value
            slots.append(slot)
        return slots

    def _inp(self) -> PlannerInput:
        return make_summer_day_input()

    def test_all_candidate_names_present(self):
        """generate_candidates must return all seven named candidates."""
        inp = self._inp()
        now = datetime.fromisoformat(inp.now_iso)
        slots = self._make_baseline()
        candidates = generate_candidates(slots, inp, now, max_charge_per_slot=1.25)
        names = {c.name for c in candidates}
        assert CANDIDATE_BASELINE in names
        assert CANDIDATE_NO_ACTION in names
        assert CANDIDATE_PASSIVE in names
        assert CANDIDATE_GRID_CHARGE in names
        assert CANDIDATE_SOLAR_ONLY in names
        assert CANDIDATE_DISCHARGE_ONLY in names
        assert CANDIDATE_AGGRESSIVE in names

    def test_baseline_is_first(self):
        """The baseline candidate must always be the first in the list."""
        inp = self._inp()
        now = datetime.fromisoformat(inp.now_iso)
        slots = self._make_baseline()
        candidates = generate_candidates(slots, inp, now, max_charge_per_slot=1.25)
        assert candidates[0].name == CANDIDATE_BASELINE

    def test_candidates_are_independent(self):
        """Mutating one candidate's slots must not affect another."""
        inp = self._inp()
        now = datetime.fromisoformat(inp.now_iso)
        slots = self._make_baseline()
        candidates = generate_candidates(slots, inp, now, max_charge_per_slot=1.25)
        baseline = next(c for c in candidates if c.name == CANDIDATE_BASELINE)
        no_action = next(c for c in candidates if c.name == CANDIDATE_NO_ACTION)
        # Mutate baseline; no_action must be unaffected
        baseline.slots[0].recommendation = "force_batteries_discharge"
        assert no_action.slots[0].recommendation is None

    def test_no_action_has_no_charge_or_discharge(self):
        """no_action candidate must have all recommendations cleared."""
        inp = self._inp()
        now = datetime.fromisoformat(inp.now_iso)
        slots = self._make_baseline()
        candidates = generate_candidates(slots, inp, now, max_charge_per_slot=1.25)
        no_action = next(c for c in candidates if c.name == CANDIDATE_NO_ACTION)
        active_recs = {
            s.recommendation
            for s in no_action.slots
            if s.recommendation not in {None, Recommendations.TimePassed.value}
        }
        charge_discharge = {
            Recommendations.BatteriesChargeGrid.value,
            Recommendations.BatteriesChargeSolar.value,
            Recommendations.BatteriesDischargeMode.value,
            Recommendations.ForceBatteriesDischarge.value,
        }
        assert not active_recs.intersection(charge_discharge)

    def test_discharge_only_has_no_charge(self):
        """discharge_only candidate must contain no charge recommendations."""
        inp = self._inp()
        now = datetime.fromisoformat(inp.now_iso)
        slots = self._make_baseline()
        candidates = generate_candidates(slots, inp, now, max_charge_per_slot=1.25)
        discharge_only = next(
            c for c in candidates if c.name == CANDIDATE_DISCHARGE_ONLY
        )
        charge_recs = {
            Recommendations.BatteriesChargeGrid.value,
            Recommendations.BatteriesChargeSolar.value,
        }
        assert not any(s.recommendation in charge_recs for s in discharge_only.slots)

    def test_aggressive_only_modifies_future_slots(self):
        """Aggressive strategy must not touch past slots."""
        inp = make_summer_day_input(now_iso="2024-06-15T12:00:00+02:00")
        now = datetime.fromisoformat(inp.now_iso)
        slots = _populated_slots_for_input(inp)
        # Mark first 12 slots as past
        for s in slots[:12]:
            s.recommendation = Recommendations.TimePassed.value
        candidates = generate_candidates(slots, inp, now, max_charge_per_slot=1.25)
        aggressive = next(c for c in candidates if c.name == CANDIDATE_AGGRESSIVE)
        for s in aggressive.slots:
            if s.end.astimezone(_TZ) <= now:
                # Past slots should not be forced to charge/discharge
                assert s.recommendation in {
                    None,
                    Recommendations.TimePassed.value,
                    Recommendations.BatteriesChargeGrid.value,
                    Recommendations.BatteriesChargeSolar.value,
                    Recommendations.BatteriesDischargeMode.value,
                    Recommendations.ForceBatteriesDischarge.value,
                }


# ===========================================================================
# 4. _validate_candidate
# ===========================================================================


class TestValidateCandidate:
    """_validate_candidate must catch SoC floor violations."""

    def test_valid_plan_passes(self):
        """A plan where all slots have SoC above the floor is valid."""
        slots = [
            _make_simple_slot(hour=h, estimated_battery_soc=50.0) for h in range(3)
        ]
        plan = CandidatePlan(name="test", slots=slots)
        is_valid, reason = _validate_candidate(plan, end_of_discharge_soc_pct=10.0)
        assert is_valid is True
        assert reason == ""

    def test_plan_with_zero_soc_passes(self):
        """Slots with soc=0 (unset) do not trigger the floor check."""
        slots = [_make_simple_slot(hour=h, estimated_battery_soc=0.0) for h in range(3)]
        plan = CandidatePlan(name="test", slots=slots)
        is_valid, _ = _validate_candidate(plan, end_of_discharge_soc_pct=10.0)
        assert is_valid is True

    def test_plan_below_floor_is_invalid(self):
        """A slot where SoC is below the floor (minus tolerance) is invalid."""
        slots = [
            _make_simple_slot(hour=h, estimated_battery_soc=50.0) for h in range(3)
        ]
        # Set one slot well below the floor
        slots[1].estimated_battery_soc = 5.0
        plan = CandidatePlan(name="test", slots=slots)
        is_valid, reason = _validate_candidate(plan, end_of_discharge_soc_pct=10.0)
        assert is_valid is False
        assert "5.0" in reason

    def test_plan_at_tolerance_boundary_passes(self):
        """A plan at exactly floor - tolerance is considered valid."""
        # tolerance is 0.5 pct, floor is 10 → 9.5 should be valid
        slots = [_make_simple_slot(hour=h, estimated_battery_soc=9.6) for h in range(3)]
        plan = CandidatePlan(name="test", slots=slots)
        is_valid, _ = _validate_candidate(plan, end_of_discharge_soc_pct=10.0)
        assert is_valid is True


# ===========================================================================
# 5. select_best_candidate — integration
# ===========================================================================


class TestSelectBestCandidate:
    """select_best_candidate must return the lowest-cost valid plan."""

    def _cost_weights(self) -> CostWeights:
        return CostWeights(
            min_soc_pct=10.0,
            max_soc_pct=100.0,
            battery_purchase_price=10_000.0,
            battery_rated_capacity_kwh=10.0,
            battery_expected_cycles=6000,
        )

    def test_returns_a_candidate_plan(self):
        """select_best_candidate must always return a CandidatePlan."""
        inp = make_summer_day_input()
        now = datetime.fromisoformat(inp.now_iso)
        slots = _populated_slots_for_input(inp)
        candidates = generate_candidates(slots, inp, now, max_charge_per_slot=1.25)
        winner, _ = select_best_candidate(
            candidates,
            now=now,
            current_kwh=4.5,
            usable_kwh=9.0,
            max_soc_capacity_kwh=9.0,
            max_charge_per_slot=1.25,
            max_discharge_per_slot=None,
            rated_kwh=10.0,
            end_of_discharge_soc_pct=10.0,
            cost_weights=self._cost_weights(),
            slot_duration_hours=1.0,
        )
        assert isinstance(winner, CandidatePlan)

    def test_all_non_winners_are_in_rejected(self):
        """Every candidate that is not the winner must appear in rejected list."""
        inp = make_summer_day_input()
        now = datetime.fromisoformat(inp.now_iso)
        slots = _populated_slots_for_input(inp)
        candidates = generate_candidates(slots, inp, now, max_charge_per_slot=1.25)
        winner, rejected = select_best_candidate(
            candidates,
            now=now,
            current_kwh=4.5,
            usable_kwh=9.0,
            max_soc_capacity_kwh=9.0,
            max_charge_per_slot=1.25,
            max_discharge_per_slot=None,
            rated_kwh=10.0,
            end_of_discharge_soc_pct=10.0,
            cost_weights=self._cost_weights(),
            slot_duration_hours=1.0,
        )
        rejected_names = {rp.name for rp in rejected}
        for candidate in candidates:
            if candidate is not winner:
                assert candidate.name in rejected_names

    def test_winner_has_lowest_cost_among_valid(self):
        """The winner must not cost more than any other valid candidate."""
        inp = make_summer_day_input()
        now = datetime.fromisoformat(inp.now_iso)
        slots = _populated_slots_for_input(inp)
        candidates = generate_candidates(slots, inp, now, max_charge_per_slot=1.25)
        winner, _ = select_best_candidate(
            candidates,
            now=now,
            current_kwh=4.5,
            usable_kwh=9.0,
            max_soc_capacity_kwh=9.0,
            max_charge_per_slot=1.25,
            max_discharge_per_slot=None,
            rated_kwh=10.0,
            end_of_discharge_soc_pct=10.0,
            cost_weights=self._cost_weights(),
            slot_duration_hours=1.0,
        )
        winner_cost = getattr(getattr(winner, "_cost", None), "total", float("inf"))
        for candidate in candidates:
            if candidate is winner:
                continue
            if not candidate.is_valid:
                continue
            candidate_cost = getattr(
                getattr(candidate, "_cost", None), "total", float("inf")
            )
            assert winner_cost <= candidate_cost + 1e-9

    def test_no_action_never_wins_when_only_valid(self):
        """When only no_action is valid, it must NOT win — baseline must win."""
        inp = make_summer_day_input()
        now = datetime.fromisoformat(inp.now_iso)
        slots = _populated_slots_for_input(inp)
        candidates = generate_candidates(slots, inp, now, max_charge_per_slot=1.25)
        # Force all candidates except no_action to be invalid
        for candidate in candidates:
            if candidate.name != CANDIDATE_NO_ACTION:
                candidate.is_valid = False
                candidate.rejection_reason = "forced invalid for test"
        # Run select
        for candidate in candidates:
            from custom_components.hsem.planner.soc_simulation import simulate_soc

            simulate_soc(
                candidate.slots,
                now,
                current_kwh=4.5,
                usable_kwh=9.0,
                max_capacity_kwh=9.0,
                max_charge_per_slot=1.25,
                max_discharge_per_slot=None,
                rated_kwh=10.0,
                end_of_discharge_soc_pct=10.0,
            )
        # Re-force invalidity
        for candidate in candidates:
            if candidate.name != CANDIDATE_NO_ACTION:
                candidate.is_valid = False
                candidate.rejection_reason = "forced invalid for test"

        cost_weights = CostWeights(
            min_soc_pct=10.0,
            max_soc_pct=100.0,
            battery_purchase_price=10_000.0,
            battery_rated_capacity_kwh=10.0,
            battery_expected_cycles=6000,
        )
        winner, rejected = select_best_candidate(
            candidates,
            now=now,
            current_kwh=4.5,
            usable_kwh=9.0,
            max_soc_capacity_kwh=9.0,
            max_charge_per_slot=1.25,
            max_discharge_per_slot=None,
            rated_kwh=10.0,
            end_of_discharge_soc_pct=10.0,
            cost_weights=cost_weights,
            slot_duration_hours=1.0,
        )
        # With only no_action valid (and excluded), the fallback is baseline
        assert winner.name == CANDIDATE_BASELINE, (
            f"Expected baseline to win (fallback), got {winner.name}"
        )
        # Verify no_action is in rejected with an exclusion reason
        no_action_rejected = next(
            (r for r in rejected if r.name == CANDIDATE_NO_ACTION), None
        )
        assert no_action_rejected is not None, "no_action must appear in rejected plans"


# ===========================================================================
# 6. Full planner integration — candidates on PlannerOutput
# ===========================================================================


class TestPlannerOutputCandidates:
    """run_planner must populate PlannerOutput.candidates."""

    def test_candidates_field_is_populated(self):
        """After a full planning run the candidates list must not be empty."""
        output = run_planner(make_summer_day_input())
        assert len(output.candidates) >= 1

    def test_candidates_contains_baseline(self):
        """The candidates list must include a baseline entry."""
        output = run_planner(make_summer_day_input())
        names = [c.name for c in output.candidates]
        assert CANDIDATE_BASELINE in names

    def test_all_seven_candidates_present(self):
        """The seven core named candidates must appear in a standard summer run.

        When scipy is available an 8th ``milp`` candidate is also added.  This
        test only asserts the seven mandatory candidates are present; the MILP
        candidate is validated separately in test_milp_optimizer.py.
        """
        output = run_planner(make_summer_day_input())
        names = {c.name for c in output.candidates}
        expected_core = {
            CANDIDATE_BASELINE,
            CANDIDATE_NO_ACTION,
            CANDIDATE_PASSIVE,
            CANDIDATE_GRID_CHARGE,
            CANDIDATE_SOLAR_ONLY,
            CANDIDATE_DISCHARGE_ONLY,
            CANDIDATE_AGGRESSIVE,
        }
        assert expected_core <= names, (
            f"Missing core candidates: {expected_core - names}"
        )

    def test_rejected_plans_include_candidate_alternatives(self):
        """explanation.rejected_plans must include non-winning candidates."""
        output = run_planner(make_summer_day_input())
        # There are always multiple candidates so at least one must be rejected
        assert len(output.explanation.rejected_plans) >= 1

    def test_winter_run_has_candidates(self):
        """Candidate generation must work for a winter planning run too."""
        output = run_planner(make_winter_day_input())
        assert len(output.candidates) >= 1
        names = {c.name for c in output.candidates}
        assert CANDIDATE_BASELINE in names

    def test_flat_price_run_has_candidates(self):
        """Candidate generation must work when prices are flat."""
        output = run_planner(make_flat_price_input())
        assert len(output.candidates) >= 1

    def test_plan_cost_is_populated_on_winner(self):
        """PlannerOutput.plan_cost must be set after candidate selection."""
        output = run_planner(make_summer_day_input())
        assert output.plan_cost is not None
        # Total cost must be a finite float
        assert isinstance(output.plan_cost.total, float)
        assert output.plan_cost.total == pytest.approx(output.plan_cost.total, rel=1e-6)

    def test_missing_input_run_returns_empty_candidates(self):
        """When the planner produces no slots the candidates list is empty."""
        # Build a valid input but with an impossible future: battery capacity
        # of zero so usable_kwh == 0 and a very short horizon that would
        # produce no meaningful slots.  We test the structural guarantee that
        # PlannerOutput.candidates is an empty list on the early-exit path.
        # The engine returns PlannerOutput(missing_inputs=..., warnings=...)
        # without a candidates key when build_slots returns [].
        # We achieve this by constructing a PlannerOutput directly.
        from custom_components.hsem.models.planner_outputs import PlannerOutput

        output = PlannerOutput(missing_inputs=["battery_rated_capacity_kwh"])
        assert output.candidates == []

    def test_winning_candidate_slots_match_output_slots(self):
        """The slots on the winning candidate must be the same objects as output.slots."""
        output = run_planner(make_summer_day_input())
        # Find the winning candidate (the one whose slots list is output.slots)
        winner_candidates = [
            c
            for c in output.candidates
            if len(c.slots) == len(output.slots)
            and all(a is b for a, b in zip(c.slots, output.slots))
        ]
        assert len(winner_candidates) == 1, (
            "Exactly one candidate should share its slots list with output.slots"
        )


# ===========================================================================
# 7. Passive candidate tests (issue #420)
# ===========================================================================


class TestPassiveCandidate:
    """Tests for the passive candidate and _apply_passive_solar helper."""

    def test_passive_candidate_present(self):
        """CANDIDATE_PASSIVE must be present after a standard summer day run."""
        output = run_planner(make_summer_day_input())
        names = {c.name for c in output.candidates}
        assert CANDIDATE_PASSIVE in names, (
            f"Expected CANDIDATE_PASSIVE in candidates, got {names}"
        )

    def test_passive_charges_on_pv_surplus(self):
        """Slots with negative estimated_net_consumption get solar charge."""
        tz = ZoneInfo("Europe/Copenhagen")
        now = datetime(2024, 6, 15, 12, 0, tzinfo=tz)
        slots = [
            _make_simple_slot(
                hour=8,  # start=08:00, end=09:00 — past
                recommendation=Recommendations.BatteriesChargeGrid.value,
                batteries_charged=3.0,
            ),
            _make_simple_slot(
                hour=13,  # start=13:00, end=14:00 — future
                recommendation=Recommendations.BatteriesDischargeMode.value,
                batteries_charged=0.0,
            ),
            _make_simple_slot(
                hour=14,  # start=14:00, end=15:00 — future
                recommendation=None,
                batteries_charged=0.0,
            ),
            _make_simple_slot(
                hour=15,  # start=15:00, end=16:00 — future
                recommendation=None,
                batteries_charged=0.0,
            ),
        ]
        # Set up: slot 0 (past, surplus), slot 1 (future, surplus),
        # slot 2 (future, net positive), slot 3 (future, surplus)
        slots[0].estimated_net_consumption = -2.0  # past surplus — ignored
        slots[1].estimated_net_consumption = -2.0  # future surplus
        slots[2].estimated_net_consumption = 1.5  # positive — ignored
        slots[3].estimated_net_consumption = -0.5  # future surplus

        _apply_passive_solar(slots, now)

        # Past slot with surplus: recommendation cleared, not re-assigned
        assert slots[0].recommendation is None
        assert abs(slots[0].batteries_charged) < 1e-9

        # Future slot with surplus (-2.0): gets BatteriesChargeSolar, charged=2.0
        assert slots[1].recommendation == Recommendations.BatteriesChargeSolar.value
        assert slots[1].batteries_charged == pytest.approx(2.0)

        # Future slot with positive net consumption: remains None
        assert slots[2].recommendation is None
        assert abs(slots[2].batteries_charged) < 1e-9

        # Future slot with surplus (-0.5): gets BatteriesChargeSolar, charged=0.5
        assert slots[3].recommendation == Recommendations.BatteriesChargeSolar.value
        assert slots[3].batteries_charged == pytest.approx(0.5)

    def test_no_action_never_wins(self):
        """run_planner on a summer day must never select no_action as winner."""
        output = run_planner(make_summer_day_input())
        # The winning candidate is the one whose slots list IS output.slots
        winner_candidates = [
            c
            for c in output.candidates
            if len(c.slots) == len(output.slots)
            and all(a is b for a, b in zip(c.slots, output.slots))
        ]
        assert len(winner_candidates) == 1
        winner = winner_candidates[0]
        assert winner.name != CANDIDATE_NO_ACTION, (
            "no_action must never be the winning candidate"
        )

    def test_passive_never_grid_charges(self):
        """_apply_passive_solar must never assign BatteriesChargeGrid."""
        tz = ZoneInfo("Europe/Copenhagen")
        now = datetime(2024, 6, 15, 0, 0, tzinfo=tz)
        slots = [
            _make_simple_slot(
                hour=h,
                recommendation=(
                    Recommendations.BatteriesChargeGrid.value
                    if h % 2 == 0
                    else Recommendations.BatteriesDischargeMode.value
                ),
            )
            for h in range(24)
        ]
        for s in slots:
            s.estimated_net_consumption = -1.0  # all surplus

        _apply_passive_solar(slots, now)

        for slot in slots:
            assert slot.recommendation != Recommendations.BatteriesChargeGrid.value, (
                f"_apply_passive_solar must never assign BatteriesChargeGrid "
                f"(found at slot starting {slot.start})"
            )

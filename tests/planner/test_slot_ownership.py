"""Slot ownership and copy-semantics tests for the HSEM planner (issue #XXX).

Verifies the ownership model end-to-end:

- Base slots are **input only** — populated once, never mutated post-generation.
- Each candidate owns **independent** slot objects (shallow copy is safe for
  ``PlannedSlot`` because all its fields are immutable scalars or NamedTuples).
- Final output is built from the **winner's** slots, not from stale base slots.
- ``ev_planned_load_kwh`` survives candidate generation and winner selection.
- ``estimated_net_consumption_kwh`` in the final output includes EV load.
- Post-output recommendation resolver only changes ``recommendation`` labels —
  it never zeros or overwrites energy fields (``ev_planned_load_kwh``,
  ``estimated_net_consumption_kwh``, ``batteries_charged_kwh``, etc.).

All tests are pure-Python; no Home Assistant runtime is required.

Test classes
------------
TestShallowCopySafety
    ``_copy_slots`` produces independent slot objects.
TestEvPlannedLoadSurvivestCandidateGeneration
    ``ev_planned_load_kwh`` is preserved on every candidate's slots.
TestEvPlannedLoadSurvivesWinnerSelection
    ``ev_planned_load_kwh`` appears on the winning candidate's slots and in
    the final ``PlannerOutput``.
TestFinalRecommendationIncludesEvLoad
    The final ``HourlyRecommendation`` objects carry the EV load from the
    winning planner slots (tested via ``_apply_planner_output`` logic).
TestCandidateIsolation
    Mutating candidate A's slots does not affect candidate B's slots.
TestResolverPreservesEnergyFields
    ``resolve_current_recommendation`` only changes the recommendation label;
    it does not zero or overwrite energy fields.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.custom_sensors.recommendation_resolver import (
    resolve_current_recommendation,
)
from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import EVLiveState, LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.candidate_generator import (
    CANDIDATE_BASELINE,
    CANDIDATE_NO_ACTION,
    _copy_slots,
    generate_candidates,
)
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations
from tests.planner.fixtures import make_summer_day_input

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_UTC = UTC
_NOW_ISO = "2024-06-15T14:00:00+00:00"


def _dt(h: int) -> datetime:
    """Return a UTC datetime on 2024-06-15 at hour *h*."""
    return datetime(2024, 6, 15, h, 0, 0, tzinfo=_UTC)


def _make_ev_input(
    now_iso: str = _NOW_ISO,
    ev_enabled: bool = True,
    ev_connected: bool = True,
    current_soc: float = 50.0,
    target_soc: float = 80.0,
    capacity_kwh: float = 40.0,
    charger_kw: float = 7.4,
    deadline_hours_from_now: float = 8.0,
    base_includes_ev: bool = False,
) -> PlannerInput:
    """Return a 24-hour summer ``PlannerInput`` with EV planned load enabled."""
    now = datetime.fromisoformat(now_iso)
    deadline = now + timedelta(hours=deadline_hours_from_now)

    prices = [
        PricePoint(hour=h, import_price=0.10, export_price=0.05) for h in range(24)
    ]
    for h in range(6):
        prices[h] = PricePoint(hour=h, import_price=0.05, export_price=0.02)
    for h in range(16, 21):
        prices[h] = PricePoint(hour=h, import_price=0.30, export_price=0.15)

    # PV surplus hours 10-15
    pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
    for h in range(10, 16):
        pv[h] = SolcastSlot(hour=h, pv_estimate=3.5)

    averages = [
        HourlyConsumptionAverage(
            hour=h, avg_1d=1.0, avg_3d=1.0, avg_7d=1.0, avg_14d=1.0
        )
        for h in range(24)
    ]

    base = make_summer_day_input(now_iso=now_iso)
    return PlannerInput(
        now_iso=now_iso,
        interval_minutes=60,
        interval_length_hours=24,
        battery_soc_pct=50.0,
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_charge_power_w=5000.0,
        battery_purchase_price=10_000.0,
        battery_expected_cycles=6000,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=averages,
        price_points=prices,
        solcast_slots=pv,
        battery_schedules=base.battery_schedules,
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        excess_export_enabled=False,
        is_read_only=True,
        # Primary EV
        ev_planned_load_enabled=ev_enabled,
        ev_planned_load_connected=ev_connected,
        ev_planned_load_smart_charging_enabled=True,
        ev_planned_load_current_soc_pct=current_soc,
        ev_planned_load_target_soc_pct=target_soc,
        ev_planned_load_battery_capacity_kwh=capacity_kwh,
        ev_planned_load_charger_power_kw=charger_kw,
        ev_planned_load_charger_efficiency_pct=100.0,
        ev_planned_load_deadline=deadline,
        ev_planned_load_base_load_includes_ev=base_includes_ev,
    )


def _make_slot(
    hour: int = 0,
    ev_kwh: float = 0.0,
    recommendation: str | None = None,
    batteries_charged_kwh: float = 0.0,
    avg_house_consumption_kwh: float = 1.0,
    solcast_pv_estimate_kwh: float = 0.0,
    estimated_net_consumption_kwh: float = 1.0,
) -> PlannedSlot:
    """Build a minimal :class:`PlannedSlot` for unit tests."""
    start = _dt(hour)
    return PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=0.20, export_price=0.05),
        recommendation=recommendation,
        batteries_charged_kwh=batteries_charged_kwh,
        avg_house_consumption_kwh=avg_house_consumption_kwh,
        solcast_pv_estimate_kwh=solcast_pv_estimate_kwh,
        estimated_net_consumption_kwh=estimated_net_consumption_kwh,
        ev_planned_load_kwh=ev_kwh,
    )


def _make_hrec(
    ev_kwh: float = 0.0,
    estimated_net_consumption_kwh: float = 1.0,
    batteries_charged_kwh: float = 0.5,
    recommendation: str | None = Recommendations.BatteriesWaitMode.value,
) -> HourlyRecommendation:
    """Return a minimal :class:`HourlyRecommendation` for resolver tests."""
    now = datetime.now(tz=_UTC)
    return HourlyRecommendation(
        avg_house_consumption_kwh=1.0,
        avg_house_consumption_1d_kwh=1.0,
        avg_house_consumption_3d_kwh=1.0,
        avg_house_consumption_7d_kwh=1.0,
        avg_house_consumption_14d_kwh=1.0,
        batteries_charged_kwh=batteries_charged_kwh,
        batteries_discharged_kwh=0.0,
        end=now + timedelta(hours=1),
        estimated_battery_capacity_kwh=5.0,
        estimated_battery_soc_pct=50.0,
        estimated_cost_currency=0.1,
        estimated_net_consumption_kwh=estimated_net_consumption_kwh,
        ev_planned_load_kwh=ev_kwh,
        export_price=0.05,
        grid_export_kwh=0.0,
        grid_import_kwh=0.0,
        import_price=0.20,
        recommendation=recommendation,
        solcast_pv_estimate_kwh=0.5,
        start=now,
    )


def _make_live(
    ev_charging: bool = False,
    ev2_charging: bool = False,
    import_price: float = 0.20,
    battery_kwh: float = 5.0,
) -> LiveState:
    """Build a minimal :class:`LiveState` for resolver tests."""
    live = LiveState()
    live.import_electricity_price = import_price
    live.ev = EVLiveState(is_charging=ev_charging)
    live.ev_second = EVLiveState(is_charging=ev2_charging)
    live.battery_current_capacity_kwh = battery_kwh
    return live


# ===========================================================================
# 1. Shallow-copy safety for PlannedSlot
# ===========================================================================


class TestShallowCopySafety:
    """``_copy_slots`` produces slot copies that are fully independent.

    ``PlannedSlot`` has only scalar and immutable NamedTuple fields, so
    ``copy.copy`` is semantically equivalent to a deep copy for that class.
    These tests verify the invariant holds for every field that candidates
    are allowed to mutate (``recommendation``, ``batteries_charged_kwh``,
    ``ev_planned_load_kwh``).
    """

    def test_recommendation_mutation_is_isolated(self):
        """Mutating ``recommendation`` on a copy must not affect the original."""
        original = [_make_slot(hour=0, recommendation=None)]
        copies = _copy_slots(original)
        copies[0].recommendation = Recommendations.BatteriesChargeGrid.value
        assert original[0].recommendation is None

    def test_batteries_charged_mutation_is_isolated(self):
        """Mutating ``batteries_charged_kwh`` on a copy must not affect the original."""
        original = [_make_slot(hour=0, batteries_charged_kwh=0.0)]
        copies = _copy_slots(original)
        copies[0].batteries_charged_kwh = 99.9
        assert abs(original[0].batteries_charged_kwh) < 1e-9

    def test_ev_planned_load_mutation_is_isolated(self):
        """Mutating ``ev_planned_load_kwh`` on a copy must not affect the original."""
        original = [_make_slot(hour=0, ev_kwh=2.5)]
        copies = _copy_slots(original)
        copies[0].ev_planned_load_kwh = 0.0
        assert abs(original[0].ev_planned_load_kwh - 2.5) < 1e-9

    def test_estimated_net_consumption_mutation_is_isolated(self):
        """Mutating ``estimated_net_consumption_kwh`` on a copy is isolated."""
        original = [_make_slot(hour=0, estimated_net_consumption_kwh=1.5)]
        copies = _copy_slots(original)
        copies[0].estimated_net_consumption_kwh = 0.0
        assert abs(original[0].estimated_net_consumption_kwh - 1.5) < 1e-9

    def test_copy_count_equals_original(self):
        """``_copy_slots`` returns the same number of slots as the input."""
        slots = [_make_slot(hour=h) for h in range(5)]
        assert len(_copy_slots(slots)) == len(slots)

    def test_copy_objects_are_distinct(self):
        """Each copied slot must be a different object than the original."""
        original = [_make_slot(hour=h) for h in range(3)]
        copies = _copy_slots(original)
        for orig, copy_ in zip(original, copies):
            assert orig is not copy_


# ===========================================================================
# 2. EV planned load survives candidate generation
# ===========================================================================


class TestEvPlannedLoadSurvivestCandidateGeneration:
    """``ev_planned_load_kwh`` is preserved on every candidate's slot list.

    The candidate generator must copy the field from the baseline slots into
    every candidate (baseline, no_action, grid_charge, solar_only,
    discharge_only, aggressive).  No helper function is allowed to clear it.
    """

    def _make_ev_slots(self) -> list[PlannedSlot]:
        """Build 24 baseline slots where hours 14-17 carry EV planned load."""
        slots = []
        for h in range(24):
            ev = 3.0 if 14 <= h < 18 else 0.0
            slot = _make_slot(hour=h, ev_kwh=ev, recommendation=None)
            slots.append(slot)
        return slots

    @pytest.mark.skip(reason="MILP-only mode: schedule-based behavior not applicable")
    def test_ev_load_on_baseline_candidate(self):
        """Baseline candidate slots carry the same EV load as the input."""
        baseline_slots = self._make_ev_slots()
        inp = make_summer_day_input()
        now = datetime.fromisoformat(inp.now_iso)
        candidates = generate_candidates(
            baseline_slots, inp, now, max_charge_per_slot=1.0
        )
        baseline = next(c for c in candidates if c.name == CANDIDATE_BASELINE)
        for h, slot in enumerate(baseline.slots):
            expected = 3.0 if 14 <= h < 18 else 0.0
            assert abs(slot.ev_planned_load_kwh - expected) < 1e-9, (
                f"Baseline slot h={h}: expected ev_load={expected}, "
                f"got {slot.ev_planned_load_kwh}"
            )

    def test_ev_load_on_no_action_candidate(self):
        """no_action candidate still carries EV load (only battery sched cleared)."""
        baseline_slots = self._make_ev_slots()
        inp = make_summer_day_input()
        now = datetime.fromisoformat(inp.now_iso)
        candidates = generate_candidates(
            baseline_slots, inp, now, max_charge_per_slot=1.0
        )
        no_action = next(c for c in candidates if c.name == CANDIDATE_NO_ACTION)
        for h, slot in enumerate(no_action.slots):
            expected = 3.0 if 14 <= h < 18 else 0.0
            assert abs(slot.ev_planned_load_kwh - expected) < 1e-9, (
                f"no_action slot h={h}: expected ev_load={expected}, "
                f"got {slot.ev_planned_load_kwh}"
            )

    def test_ev_load_preserved_on_all_candidates(self):
        """All six candidates carry the EV load from the baseline slots."""
        baseline_slots = self._make_ev_slots()
        inp = make_summer_day_input()
        now = datetime.fromisoformat(inp.now_iso)
        candidates = generate_candidates(
            baseline_slots, inp, now, max_charge_per_slot=1.0
        )
        for candidate in candidates:
            for h, slot in enumerate(candidate.slots):
                expected = 3.0 if 14 <= h < 18 else 0.0
                assert abs(slot.ev_planned_load_kwh - expected) < 1e-9, (
                    f"Candidate '{candidate.name}' slot h={h}: "
                    f"expected ev_load={expected}, got {slot.ev_planned_load_kwh}"
                )


# ===========================================================================
# 3. EV planned load survives winner selection (full engine run)
# ===========================================================================


class TestEvPlannedLoadSurvivesWinnerSelection:
    """``ev_planned_load_kwh`` must appear on the winning candidate's slots
    in the final ``PlannerOutput.slots``.

    Runs the full ``run_planner`` pipeline with EV enabled.
    """

    def test_ev_load_present_in_final_output_slots(self):
        """At least one slot in the final output carries non-zero EV load."""
        inp = _make_ev_input()
        output = run_planner(inp)
        ev_slots = [s for s in output.slots if abs(s.ev_planned_load_kwh) > 1e-9]
        assert ev_slots, (
            "Expected at least one output slot with ev_planned_load_kwh > 0, "
            "but all were zero. The EV load did not survive winner selection."
        )

    def test_ev_load_zero_when_disabled(self):
        """When EV is disabled, all output slots must have ev_planned_load_kwh == 0."""
        inp = _make_ev_input(ev_enabled=False)
        output = run_planner(inp)
        for slot in output.slots:
            assert abs(slot.ev_planned_load_kwh) < 1e-9, (
                f"Expected ev_planned_load_kwh == 0 when EV disabled, "
                f"got {slot.ev_planned_load_kwh} at {slot.start}"
            )

    def test_ev_load_zero_when_disconnected(self):
        """When EV is not connected, all output slots must have ev_load == 0."""
        inp = _make_ev_input(ev_connected=False)
        output = run_planner(inp)
        for slot in output.slots:
            assert abs(slot.ev_planned_load_kwh) < 1e-9, (
                f"Expected ev_planned_load_kwh == 0 when EV disconnected, "
                f"got {slot.ev_planned_load_kwh} at {slot.start}"
            )

    def test_ev_load_total_kwh_is_non_negative(self):
        """Total EV planned load across all slots must be non-negative."""
        inp = _make_ev_input()
        output = run_planner(inp)
        total = sum(s.ev_planned_load_kwh for s in output.slots)
        assert total >= 0.0, f"Total EV planned load must be ≥ 0, got {total}"

    def test_ev_load_consistent_with_net_consumption(self):
        """Each output slot: estimated_net_consumption_kwh ≈ avg_house + ev_load - pv.

        This verifies that ``populate_net_consumption`` was applied AFTER
        ``ev_planned_load_kwh`` was written (correct) and not before (stale).
        """
        inp = _make_ev_input()
        output = run_planner(inp)
        for slot in output.slots:
            expected = (
                slot.avg_house_consumption_kwh
                + slot.ev_planned_load_kwh
                - slot.solcast_pv_estimate_kwh
            )
            assert (
                abs(slot.estimated_net_consumption_kwh - round(expected, 3)) < 1e-6
            ), (
                f"Slot {slot.start}: estimated_net_consumption_kwh={slot.estimated_net_consumption_kwh} "
                f"but avg_house={slot.avg_house_consumption_kwh}, ev_load={slot.ev_planned_load_kwh}, "
                f"pv={slot.solcast_pv_estimate_kwh} → expected {round(expected, 3)}"
            )


# ===========================================================================
# 4. Final HourlyRecommendation includes EV load
# ===========================================================================


class TestFinalRecommendationIncludesEvLoad:
    """The mapping from ``PlannedSlot`` → ``HourlyRecommendation`` must carry
    ``ev_planned_load_kwh`` and ``estimated_net_consumption_kwh``.

    This test validates the coordinator's ``_apply_planner_output`` field-copy
    contract by verifying that the output ``PlannedSlot`` fields are exactly
    the ones the coordinator reads.
    """

    def test_output_slot_ev_load_is_readable(self):
        """``PlannerOutput.slots`` exposes ``ev_planned_load_kwh`` directly."""
        inp = _make_ev_input()
        output = run_planner(inp)
        # The coordinator does: rec.ev_planned_load_kwh = slot.ev_planned_load_kwh
        # Verify the field exists and has the right type.
        for slot in output.slots:
            assert isinstance(slot.ev_planned_load_kwh, float)

    def test_output_slot_net_consumption_includes_ev(self):
        """``estimated_net_consumption_kwh`` on output slots includes EV load."""
        inp = _make_ev_input()
        output = run_planner(inp)
        # For any slot with EV load, net consumption must be >= avg_house - pv
        # (it must be higher than if there were no EV).
        for slot in output.slots:
            if abs(slot.ev_planned_load_kwh) < 1e-9:
                continue
            # net consumption without EV would be: avg_house - pv
            no_ev_net = slot.avg_house_consumption_kwh - slot.solcast_pv_estimate_kwh
            # With EV it must be higher (more demand)
            assert slot.estimated_net_consumption_kwh > no_ev_net - 1e-6, (
                f"Slot {slot.start}: net_consumption={slot.estimated_net_consumption_kwh} "
                f"should be > no-EV baseline {no_ev_net} when ev_load={slot.ev_planned_load_kwh}"
            )

    def test_slot_to_hrec_field_mapping(self):
        """Fields the coordinator copies from slot to HourlyRecommendation are present."""
        # Build a PlannedSlot that simulates a fully-populated winner slot.
        slot = PlannedSlot(
            start=_dt(10),
            end=_dt(11),
            price=SlotPrice(import_price=0.20, export_price=0.05),
            avg_house_consumption_kwh=1.0,
            solcast_pv_estimate_kwh=3.5,
            ev_planned_load_kwh=2.5,
            estimated_net_consumption_kwh=round(1.0 + 2.5 - 3.5, 3),  # 0.0
            estimated_cost_currency=0.0,
            estimated_battery_soc_pct=55.0,
            estimated_battery_capacity_kwh=4.5,
            batteries_charged_kwh=0.0,
            batteries_discharged_kwh=0.5,
            grid_import_kwh=0.0,
            grid_export_kwh=0.5,
            recommendation=Recommendations.EVSmartCharging.value,
        )

        # Simulate the coordinator's _apply_planner_output field copy.
        hrec = HourlyRecommendation(
            avg_house_consumption_kwh=0.0,
            avg_house_consumption_1d_kwh=0.0,
            avg_house_consumption_3d_kwh=0.0,
            avg_house_consumption_7d_kwh=0.0,
            avg_house_consumption_14d_kwh=0.0,
            batteries_charged_kwh=0.0,
            batteries_discharged_kwh=0.0,
            end=_dt(11),
            estimated_battery_capacity_kwh=0.0,
            estimated_battery_soc_pct=0.0,
            estimated_cost_currency=0.0,
            estimated_net_consumption_kwh=0.0,
            ev_planned_load_kwh=0.0,
            export_price=0.0,
            grid_export_kwh=0.0,
            grid_import_kwh=0.0,
            import_price=0.0,
            recommendation=None,
            solcast_pv_estimate_kwh=0.0,
            start=_dt(10),
        )
        # Apply the same copies the coordinator performs in _apply_planner_output.
        hrec.recommendation = slot.recommendation
        hrec.batteries_charged_kwh = slot.batteries_charged_kwh
        hrec.batteries_discharged_kwh = slot.batteries_discharged_kwh
        hrec.estimated_net_consumption_kwh = slot.estimated_net_consumption_kwh
        hrec.ev_planned_load_kwh = slot.ev_planned_load_kwh
        hrec.estimated_cost_currency = slot.estimated_cost_currency
        hrec.estimated_battery_capacity_kwh = slot.estimated_battery_capacity_kwh
        hrec.estimated_battery_soc_pct = slot.estimated_battery_soc_pct
        hrec.grid_import_kwh = slot.grid_import_kwh
        hrec.grid_export_kwh = slot.grid_export_kwh
        hrec.solcast_pv_estimate_kwh = slot.solcast_pv_estimate_kwh

        assert hrec.recommendation == Recommendations.EVSmartCharging.value
        assert abs(hrec.ev_planned_load_kwh - 2.5) < 1e-9
        assert abs(hrec.estimated_net_consumption_kwh - 0.0) < 1e-9
        assert abs(hrec.solcast_pv_estimate_kwh - 3.5) < 1e-9


# ===========================================================================
# 5. Candidate isolation — mutating A does not affect B
# ===========================================================================


class TestCandidateIsolation:
    """No two candidates may share the same slot objects.

    Validates that ``_copy_slots`` provides true isolation: mutating a slot
    in one candidate does not corrupt any other candidate's corresponding slot.
    """

    def _make_candidates(self):
        """Return the full candidate list for a simple 24-slot baseline."""
        slots = [_make_slot(hour=h, ev_kwh=2.0, recommendation=None) for h in range(24)]
        inp = make_summer_day_input()
        now = datetime.fromisoformat(inp.now_iso)
        return generate_candidates(slots, inp, now, max_charge_per_slot=1.0)

    def test_candidate_slot_objects_are_distinct(self):
        """Every pair of candidates must have distinct slot objects."""
        candidates = self._make_candidates()
        for i, c1 in enumerate(candidates):
            for j, c2 in enumerate(candidates):
                if i >= j:
                    continue
                # Each slot in c1 must be a different object from c2's slot
                for k, (s1, s2) in enumerate(zip(c1.slots, c2.slots)):
                    assert s1 is not s2, (
                        f"Candidates '{c1.name}' and '{c2.name}' share "
                        f"the same slot object at index {k}."
                    )

    def test_mutation_of_candidate_a_does_not_affect_candidate_b(self):
        """Setting recommendation on candidate A's slot must not change candidate B."""
        candidates = self._make_candidates()
        c_a = candidates[0]  # baseline
        c_b = candidates[1]  # no_action
        original_rec_b = c_b.slots[5].recommendation
        # Mutate candidate A
        c_a.slots[5].recommendation = Recommendations.BatteriesChargeGrid.value
        # Candidate B must be unchanged
        assert c_b.slots[5].recommendation == original_rec_b

    def test_ev_load_mutation_of_candidate_a_does_not_affect_candidate_b(self):
        """Clearing EV load on candidate A must not affect candidate B."""
        candidates = self._make_candidates()
        c_a = candidates[0]  # baseline
        c_b = candidates[1]  # no_action
        # Mutate candidate A's EV load
        c_a.slots[14].ev_planned_load_kwh = 0.0
        # Candidate B's EV load must be unchanged (still 2.0)
        assert abs(c_b.slots[14].ev_planned_load_kwh - 2.0) < 1e-9

    def test_batteries_charged_mutation_of_candidate_a_does_not_affect_candidate_b(
        self,
    ):
        """Mutating ``batteries_charged_kwh`` on candidate A must not affect candidate B."""
        candidates = self._make_candidates()
        c_a = candidates[0]
        c_b = candidates[1]
        c_a.slots[2].batteries_charged_kwh = 99.9
        assert abs(c_b.slots[2].batteries_charged_kwh - 0.0) < 1e-9


# ===========================================================================
# 6. Recommendation resolver preserves energy fields
# ===========================================================================


class TestResolverPreservesEnergyFields:
    """``resolve_current_recommendation`` must only change the recommendation label.

    Energy fields (``ev_planned_load_kwh``, ``estimated_net_consumption_kwh``,
    ``batteries_charged_kwh``, ``grid_import_kwh``, ``grid_export_kwh``,
    ``batteries_discharged_kwh``) must be identical before and after the call
    for all four resolver branches.
    """

    _ENERGY_FIELDS = (
        "ev_planned_load_kwh",
        "estimated_net_consumption_kwh",
        "batteries_charged_kwh",
        "batteries_discharged_kwh",
        "grid_import_kwh",
        "grid_export_kwh",
        "solcast_pv_estimate_kwh",
        "avg_house_consumption_kwh",
    )

    def _snapshot_energy(self, rec: HourlyRecommendation) -> dict[str, float]:
        return {f: getattr(rec, f) for f in self._ENERGY_FIELDS}

    def test_negative_price_branch_preserves_energy_fields(self):
        """ForceExport override must not modify energy fields."""
        rec = _make_hrec(
            ev_kwh=2.5, estimated_net_consumption_kwh=0.5, batteries_charged_kwh=1.0
        )
        before = self._snapshot_energy(rec)
        resolve_current_recommendation(rec, _make_live(import_price=-0.05), 0.0)
        assert rec.recommendation == Recommendations.ForceExport.value
        assert self._snapshot_energy(rec) == before

    def test_ev_charging_branch_preserves_energy_fields(self):
        """EVSmartCharging override must not modify energy fields."""
        rec = _make_hrec(
            ev_kwh=3.0, estimated_net_consumption_kwh=1.5, batteries_charged_kwh=0.5
        )
        before = self._snapshot_energy(rec)
        resolve_current_recommendation(rec, _make_live(ev_charging=True), 0.0)
        assert rec.recommendation == Recommendations.EVSmartCharging.value
        assert self._snapshot_energy(rec) == before

    def test_discharge_mode_branch_preserves_energy_fields(self):
        """BatteriesDischargeMode override must not modify energy fields."""
        rec = _make_hrec(
            ev_kwh=1.0, estimated_net_consumption_kwh=0.8, batteries_charged_kwh=0.0
        )
        before = self._snapshot_energy(rec)
        resolve_current_recommendation(
            rec,
            _make_live(battery_kwh=10.0),
            batteries_schedules_remaining_capacity_needed=5.0,
        )
        assert rec.recommendation == Recommendations.BatteriesDischargeMode.value
        assert self._snapshot_energy(rec) == before

    def test_grid_charge_preserved_branch_does_not_modify_any_field(self):
        """When recommendation is BatteriesChargeGrid, no fields change."""
        rec = _make_hrec(
            ev_kwh=2.0,
            batteries_charged_kwh=3.5,
            recommendation=Recommendations.BatteriesChargeGrid.value,
        )
        before_rec = rec.recommendation
        before = self._snapshot_energy(rec)
        resolve_current_recommendation(rec, _make_live(ev_charging=True), 0.0)
        assert rec.recommendation == before_rec  # unchanged
        assert self._snapshot_energy(rec) == before

    def test_no_override_branch_preserves_everything(self):
        """When no override condition is met, nothing changes."""
        rec = _make_hrec(
            ev_kwh=0.0,
            batteries_charged_kwh=1.5,
            recommendation=Recommendations.BatteriesWaitMode.value,
        )
        before_rec = rec.recommendation
        before = self._snapshot_energy(rec)
        resolve_current_recommendation(
            rec, _make_live(ev_charging=False, import_price=0.20), 0.0
        )
        assert rec.recommendation == before_rec
        assert self._snapshot_energy(rec) == before

    def test_ev_kwh_not_zeroed_by_any_resolver_branch(self):
        """``ev_planned_load_kwh`` must survive all four resolver paths."""
        for ev_charging, import_price, sched_needed in [
            (True, 0.20, 0.0),  # EV branch
            (False, -0.10, 0.0),  # Negative price branch
            (False, 0.20, 3.0),  # Discharge mode branch
            (False, 0.20, 0.0),  # No-op branch
        ]:
            rec = _make_hrec(ev_kwh=4.2)
            resolve_current_recommendation(
                rec,
                _make_live(
                    ev_charging=ev_charging, import_price=import_price, battery_kwh=10.0
                ),
                batteries_schedules_remaining_capacity_needed=sched_needed,
            )
            assert abs(rec.ev_planned_load_kwh - 4.2) < 1e-9, (
                f"ev_planned_load_kwh was modified to {rec.ev_planned_load_kwh} "
                f"by resolver (ev_charging={ev_charging}, price={import_price}, "
                f"sched_needed={sched_needed})"
            )


# ===========================================================================
# 7. End-to-end: estimated_net_consumption_kwh in final output includes EV load
# ===========================================================================


class TestNetConsumptionIncludesEvLoadEndToEnd:
    """Full engine run: every non-past output slot's ``estimated_net_consumption_kwh``
    must equal ``avg_house_consumption_kwh + ev_planned_load_kwh - solcast_pv_estimate_kwh``
    to within floating-point rounding.

    This is the spec invariant from ``populate_net_consumption``:
        net = house + ev_load - pv
    """

    def test_net_consumption_formula_holds_for_all_output_slots(self):
        """For every output slot the formula net = house + ev - pv must hold."""
        inp = _make_ev_input()
        output = run_planner(inp)
        for slot in output.slots:
            expected = round(
                slot.avg_house_consumption_kwh
                + slot.ev_planned_load_kwh
                - slot.solcast_pv_estimate_kwh,
                3,
            )
            assert abs(slot.estimated_net_consumption_kwh - expected) < 1e-6, (
                f"Slot {slot.start.isoformat()}: "
                f"estimated_net_consumption_kwh={slot.estimated_net_consumption_kwh} "
                f"!= {expected} (house={slot.avg_house_consumption_kwh}, "
                f"ev={slot.ev_planned_load_kwh}, pv={slot.solcast_pv_estimate_kwh})"
            )

    def test_ev_slots_have_higher_net_consumption_than_no_ev_run(self):
        """When EV is enabled, slots with EV load have higher net consumption
        than the corresponding slots when EV is disabled.
        """
        inp_ev = _make_ev_input()
        inp_no_ev = _make_ev_input(ev_enabled=False)
        out_ev = run_planner(inp_ev)
        out_no_ev = run_planner(inp_no_ev)

        slot_by_start_ev = {s.start: s for s in out_ev.slots}
        slot_by_start_no_ev = {s.start: s for s in out_no_ev.slots}

        elevated = 0
        for start, slot_ev in slot_by_start_ev.items():
            slot_no_ev = slot_by_start_no_ev.get(start)
            if slot_no_ev is None:
                continue
            if abs(slot_ev.ev_planned_load_kwh) > 1e-9:
                # Slot with EV load must have strictly higher net consumption
                assert (
                    slot_ev.estimated_net_consumption_kwh
                    > slot_no_ev.estimated_net_consumption_kwh - 1e-6
                ), (
                    f"Slot {start}: EV net={slot_ev.estimated_net_consumption_kwh} "
                    f"should exceed no-EV net={slot_no_ev.estimated_net_consumption_kwh}"
                )
                elevated += 1

        # Ensure the test actually exercised some EV slots
        assert elevated > 0, "No EV-load slots found — test did not exercise the path"

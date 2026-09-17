"""Seasonal fill must not label LP-idle slots as discharge slots (issue #1041).

Backport to the 6.3.x line, where the fill's summer label is
``batteries_discharge_mode`` (there is no ``batteries_discharge_window_mode``).

Before this fix the label an optimizer-declined slot received depended only on
its calendar month: winter → ``batteries_wait_mode`` (the plan holds the
battery), summer with no PV surplus → ``batteries_discharge_mode``, which
``concentrate_discharge_on_expensive_slots`` then had to clear back to wait using
a price-ranked heuristic unrelated to why the label was wrong.

That cleanup was incomplete — any mislabelled slot that fitted inside the per-day
budget survived and was published.  ``batteries_discharge_mode`` executes as
``MaximizeSelfConsumption`` and is exempt from the hold-derived 0 W cap, so the
firmware could drain the battery — below the dynamic discharge floor too — in
intervals the LP deliberately left idle, while the plan published grid import
and zero discharge for them (issue #1094).

The fix is label-only in the plan by construction: the MILP candidate is
simulated with ``milp_prepopulated=True``, so ``simulate_soc`` never re-derives
energy from the recommendation.  These tests pin down both halves — the labels
change, the economics do not.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

import pytest

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.planner import candidate_selector, run_planner
from custom_components.hsem.planner.candidate_generator import CANDIDATE_MILP
from custom_components.hsem.planner.discharge_scheduler import (
    apply_optimization_strategy,
    concentrate_discharge_on_expensive_slots,
)
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import (
    DISCHARGE_RECS,
    Recommendations,
)
from tests.planner.fixtures import (
    make_flat_price_input,
    make_negative_price_input,
    make_summer_day_input,
    make_winter_day_input,
)

_DISCHARGE = Recommendations.BatteriesDischargeMode.value
_WAIT = Recommendations.BatteriesWaitMode.value

_BUILDERS: dict[str, Callable[[], PlannerInput]] = {
    "summer": make_summer_day_input,
    "winter": make_winter_day_input,
    "flat": make_flat_price_input,
    "negative": make_negative_price_input,
}


def _scale_load(inp: PlannerInput, factor: float) -> PlannerInput:
    """Scale house consumption to reach the regime where the fill is busy."""
    return replace(
        inp,
        consumption_averages=[
            HourlyConsumptionAverage(
                hour=c.hour,
                avg_1d=c.avg_1d * factor,
                avg_3d=c.avg_3d * factor,
                avg_7d=c.avg_7d * factor,
                avg_14d=c.avg_14d * factor,
                day_offset=c.day_offset,
            )
            for c in inp.consumption_averages
        ],
    )


def _idle_summer_slots() -> tuple[datetime, list[PlannedSlot]]:
    """Four unassigned summer-night slots with house load and no PV."""
    now = datetime.fromisoformat("2024-06-15T00:00:00+02:00")
    slots = []
    for hour in range(4):
        start = now.replace(hour=hour)
        slot = PlannedSlot(start=start, end=start.replace(hour=hour + 1))
        slot.avg_house_consumption_kwh = 1.0
        slot.solcast_pv_estimate_kwh = 0.0
        slot.estimated_net_consumption_kwh = 1.0
        slot.price = SlotPrice(import_price=0.20, export_price=0.05)
        slots.append(slot)
    return now, slots


def _milp_slots(output: PlannerOutput) -> list[PlannedSlot]:
    """Return the MILP candidate's slots from a planner run."""
    return next(c.slots for c in output.candidates if c.name == CANDIDATE_MILP)


@pytest.fixture
def pre_1041(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the pre-#1041 behaviour by forcing the new flag off.

    The whole change is gated on ``unassigned_slots_are_lp_decisions``, so
    pinning it to ``False`` reproduces the old labelling exactly — which makes
    a genuine before/after comparison possible without a second checkout.
    """
    real = apply_optimization_strategy

    def wrapper(*args: object, **kwargs: object) -> None:
        kwargs["unassigned_slots_are_lp_decisions"] = False
        real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(candidate_selector, "apply_optimization_strategy", wrapper)


@pytest.fixture
def fill_discharge_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[list[PlannedSlot], int]]:
    """Record, per fill call, how many slots it labelled ``batteries_discharge_mode``.

    Each entry pairs the slot list the fill ran on (the candidate's own list
    object) with the number of slots that went from unassigned to
    ``batteries_discharge_mode`` inside that call.  Wraps whatever the selector
    currently calls, so it composes with ``pre_1041`` when that runs first.
    """
    collected: list[tuple[list[PlannedSlot], int]] = []
    real = candidate_selector.apply_optimization_strategy

    def wrapper(slots: list[PlannedSlot], *args: object, **kwargs: object) -> None:
        before = [s.recommendation for s in slots]
        real(slots, *args, **kwargs)  # type: ignore[arg-type]
        added = sum(
            1
            for old, s in zip(before, slots, strict=True)
            if old is None and s.recommendation == _DISCHARGE
        )
        collected.append((slots, added))

    monkeypatch.setattr(candidate_selector, "apply_optimization_strategy", wrapper)
    return collected


@pytest.fixture
def concentration_clearings(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[list[PlannedSlot], int]]:
    """Record, per concentration call, how many discharge labels it cleared.

    The 6.3.x function returns nothing, so the count is derived from the
    labels before and after the call.
    """
    collected: list[tuple[list[PlannedSlot], int]] = []
    real = concentrate_discharge_on_expensive_slots

    def wrapper(slots: list[PlannedSlot], *args: object, **kwargs: object) -> None:
        before = [s.recommendation in DISCHARGE_RECS for s in slots]
        real(slots, *args, **kwargs)  # type: ignore[arg-type]
        cleared = sum(
            1
            for was, s in zip(before, slots, strict=True)
            if was and s.recommendation not in DISCHARGE_RECS
        )
        collected.append((slots, cleared))

    monkeypatch.setattr(
        candidate_selector, "concentrate_discharge_on_expensive_slots", wrapper
    )
    return collected


class TestSeasonalFillUnit:
    """The flag's effect on ``apply_optimization_strategy`` in isolation."""

    def test_flag_holds_the_battery_on_a_summer_idle_slot(self) -> None:
        """A summer LP-idle slot becomes wait, not a discharge slot."""
        now, slots = _idle_summer_slots()
        apply_optimization_strategy(
            slots,
            now,
            current_capacity=5.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 10, 11, 12],
            unassigned_slots_are_lp_decisions=True,
        )
        assert [s.recommendation for s in slots] == [_WAIT] * 4

    def test_default_keeps_the_discharge_label(self) -> None:
        """Without the flag the non-MILP behaviour is unchanged."""
        now, slots = _idle_summer_slots()
        apply_optimization_strategy(
            slots,
            now,
            current_capacity=5.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 10, 11, 12],
        )
        assert [s.recommendation for s in slots] == [_DISCHARGE] * 4


class TestFillNeverLabelsMilpDischarge:
    """Every ``batteries_discharge_mode`` on the MILP candidate comes from the LP."""

    @pytest.mark.parametrize("name", sorted(_BUILDERS))
    @pytest.mark.parametrize("soc_pct", [10.0, 50.0, 100.0])
    def test_fill_adds_no_discharge_label_to_the_milp_candidate(
        self,
        fill_discharge_labels: list[tuple[list[PlannedSlot], int]],
        name: str,
        soc_pct: float,
    ) -> None:
        """The fill may label non-MILP candidates, never the MILP one."""
        inp = _BUILDERS[name]()
        inp.battery_soc_pct = soc_pct
        milp = _milp_slots(run_planner(inp))

        added = [n for slots, n in fill_discharge_labels if slots is milp]
        assert added == [0]

    @pytest.mark.usefixtures("pre_1041")
    def test_the_gap_existed_before_the_fix(
        self, fill_discharge_labels: list[tuple[list[PlannedSlot], int]]
    ) -> None:
        """Guard the regression test itself: the old behaviour really did leak.

        If this stops finding fill-added labels, the fixtures have silently
        stopped reproducing the old behaviour, and the test above would pass
        for the wrong reason.
        """
        inp = make_flat_price_input()
        inp.battery_soc_pct = 10.0
        milp = _milp_slots(run_planner(inp))

        added = [n for slots, n in fill_discharge_labels if slots is milp]
        assert added and added[0] > 0

    def test_reported_evening_slots_hold_below_the_dynamic_floor(self) -> None:
        """Issue #1094's shape: 11 % SoC under a 75.74 % dynamic floor, summer evening.

        The LP cannot discharge (the battery is below the floor) and imports
        instead.  Before the fix the fill published those slots as
        ``batteries_discharge_mode`` with zero discharge, which the applier
        executes as Maximize Self Consumption — draining the battery the plan
        says it is holding.
        """
        inp = make_summer_day_input(
            now_iso="2024-06-15T20:45:00+02:00",
            battery_soc_pct=11.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=5.0,
        )
        inp.dynamic_discharge_floor_pct = 75.74
        output = run_planner(inp)
        now = datetime.fromisoformat(inp.now_iso)
        future = [s for s in output.slots if as_tz(s.end, now.tzinfo) > now]

        assert future
        for slot in future:
            if slot.batteries_discharged_kwh <= 1e-9:
                assert slot.recommendation != _DISCHARGE, slot.start.isoformat()


class TestSeasonIndependence:
    """An LP-idle slot gets the same label whatever the month."""

    @pytest.mark.parametrize("factor", [1.0, 5.0])
    def test_concentration_clears_nothing_on_milp_in_any_season(
        self,
        concentration_clearings: list[tuple[list[PlannedSlot], int]],
        factor: float,
    ) -> None:
        """Summer needed clearings before the fix; winter always needed 0."""
        for name in ("summer", "winter"):
            concentration_clearings.clear()
            milp = _milp_slots(run_planner(_scale_load(_BUILDERS[name](), factor)))
            cleared = [n for slots, n in concentration_clearings if slots is milp]
            assert cleared, f"no MILP concentration pass on {name}"
            assert cleared == [0], (
                f"{name} load x{factor}: concentration still cleared "
                f"{cleared[0]} slot(s) on the MILP candidate"
            )


class TestLabelOnlyChange:
    """The fix must not move a single kWh or currency unit in the plan."""

    @pytest.mark.parametrize("name", sorted(_BUILDERS))
    @pytest.mark.parametrize("factor", [1.0, 5.0])
    def test_cost_and_energy_are_bit_identical(
        self, request: pytest.FixtureRequest, name: str, factor: float
    ) -> None:
        """Plan cost, score and every energy field compare exactly equal.

        Exact equality, not ``approx`` — the MILP candidate trusts its energy
        verbatim and labels carry no price, so any drift means the change
        stopped being label-only.
        """
        inp = _scale_load(_BUILDERS[name](), factor)
        after = run_planner(inp)
        request.getfixturevalue("pre_1041")
        before = run_planner(inp)

        assert before.plan_cost is not None and after.plan_cost is not None
        assert before.plan_cost.total_cost == after.plan_cost.total_cost
        assert before.plan_cost.score == after.plan_cost.score
        for a, b in zip(before.slots, after.slots, strict=True):
            assert a.batteries_charged_kwh == b.batteries_charged_kwh
            assert a.batteries_discharged_kwh == b.batteries_discharged_kwh
            assert a.grid_import_kwh == b.grid_import_kwh
            assert a.grid_export_kwh == b.grid_export_kwh

    def test_only_discharge_to_wait_transitions_occur(
        self, request: pytest.FixtureRequest
    ) -> None:
        """Every label that changes moves discharge → wait, never the reverse."""
        inp = make_flat_price_input()
        inp.battery_soc_pct = 10.0
        after = run_planner(inp)
        request.getfixturevalue("pre_1041")
        before = run_planner(inp)

        flips = [
            (a.recommendation, b.recommendation)
            for a, b in zip(before.slots, after.slots, strict=True)
            if a.recommendation != b.recommendation
        ]
        assert flips, "expected the flat fixture to exercise the change"
        for old, new in flips:
            assert old == _DISCHARGE and new == _WAIT


class TestInvariantsStillHold:
    """Planner invariants, including the one #1032 established."""

    @pytest.mark.parametrize("name", sorted(_BUILDERS))
    @pytest.mark.parametrize("soc_pct", [0.0, 50.0, 100.0])
    def test_no_wait_slot_retains_discharge(self, name: str, soc_pct: float) -> None:
        """Issue #1032 — relabelling to wait must not leave discharge behind."""
        inp = _BUILDERS[name]()
        inp.battery_soc_pct = soc_pct
        output = run_planner(inp)
        offenders = [
            s.start.isoformat()
            for s in output.slots
            if s.recommendation == _WAIT and s.batteries_discharged_kwh > 1e-9
        ]
        assert offenders == []

    @pytest.mark.parametrize("name", sorted(_BUILDERS))
    def test_soc_stays_in_bounds(self, name: str) -> None:
        """SoC-bounds invariant on every fixture."""
        for slot in run_planner(_BUILDERS[name]()).slots:
            assert 0.0 <= slot.estimated_battery_soc_pct <= 100.0

    def test_winner_slots_are_the_published_slots(self) -> None:
        """Cost identity: the gate must not detach the winner from the output."""
        output = run_planner(make_summer_day_input())
        winner = next(c for c in output.candidates if c.name == output.winner_name)
        assert winner.slots is output.slots

"""Seasonal fill must not label LP-idle slots as discharge windows (issue #1041).

Before this fix the label an optimizer-declined slot received depended only on
its calendar month: winter → ``batteries_wait_mode`` (the plan holds the
battery), summer with no PV surplus → ``batteries_discharge_window_mode``, which
``concentrate_discharge_on_expensive_slots`` then had to clear back to wait using
a price-ranked heuristic unrelated to why the label was wrong.

That cleanup was incomplete — any mislabelled slot that fitted inside the per-day
budget survived and was published, reaching the applier as
``MaximizeSelfConsumption`` so the firmware could drain the battery in intervals
the LP deliberately left idle.

The fix is label-only by construction: the MILP candidate is simulated with
``milp_prepopulated=True``, so ``simulate_soc`` never re-derives energy from the
recommendation.  These tests pin down both halves — the labels change, the
economics do not.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner import candidate_selector, run_planner
from custom_components.hsem.planner.candidate_generator import CANDIDATE_MILP
from custom_components.hsem.planner.discharge_scheduler import (
    ConcentrationStats,
    apply_optimization_strategy,
    concentrate_discharge_on_expensive_slots,
)
from custom_components.hsem.utils.recommendations import Recommendations
from tests.planner.fixtures import (
    make_flat_price_input,
    make_negative_price_input,
    make_summer_day_input,
    make_winter_day_input,
)

_WINDOW = Recommendations.BatteriesDischargeWindowMode.value
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
def capture_concentration(monkeypatch: pytest.MonkeyPatch) -> list[ConcentrationStats]:
    """Record what concentration did, per candidate."""
    collected: list[ConcentrationStats] = []
    real = concentrate_discharge_on_expensive_slots

    def wrapper(*args: object, **kwargs: object) -> ConcentrationStats:
        stats = real(*args, **kwargs)  # type: ignore[arg-type]
        collected.append(stats)
        return stats

    monkeypatch.setattr(
        candidate_selector, "concentrate_discharge_on_expensive_slots", wrapper
    )
    return collected


class TestSeasonalFillUnit:
    """The flag's effect on ``apply_optimization_strategy`` in isolation."""

    def _summer_slot_input(self) -> PlannerInput:
        return make_summer_day_input()

    def test_flag_holds_the_battery_on_a_summer_idle_slot(self) -> None:
        """A summer LP-idle slot becomes wait, not a discharge window."""
        from datetime import datetime

        from custom_components.hsem.models.planned_slot import PlannedSlot
        from custom_components.hsem.utils.prices import SlotPrice

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

    def test_default_keeps_the_discharge_window(self) -> None:
        """Without the flag the non-MILP behaviour is unchanged."""
        from datetime import datetime

        from custom_components.hsem.models.planned_slot import PlannedSlot
        from custom_components.hsem.utils.prices import SlotPrice

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

        apply_optimization_strategy(
            slots,
            now,
            current_capacity=5.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 10, 11, 12],
        )
        assert [s.recommendation for s in slots] == [_WINDOW] * 4


class TestNoMislabelledWindowIsPublished:
    """The gap the price-ranked cleanup left open."""

    @pytest.mark.parametrize("name", sorted(_BUILDERS))
    @pytest.mark.parametrize("soc_pct", [10.0, 50.0, 100.0])
    def test_published_window_slots_always_carry_discharge(
        self, name: str, soc_pct: float
    ) -> None:
        """No slot may be published as a discharge window with no discharge."""
        inp = _BUILDERS[name]()
        inp.battery_soc_pct = soc_pct
        output = run_planner(inp)
        offenders = [
            s.start.isoformat()
            for s in output.slots
            if s.recommendation == _WINDOW and s.batteries_discharged_kwh <= 1e-9
        ]
        assert offenders == [], (
            f"{len(offenders)} slot(s) published as {_WINDOW} with no discharge"
        )

    @pytest.mark.usefixtures("pre_1041")
    def test_the_gap_existed_before_the_fix(self) -> None:
        """Guard the regression test itself: the old behaviour really did leak.

        If this stops finding offenders the fixture above has silently stopped
        reproducing the old behaviour, and the test above would pass for the
        wrong reason.
        """
        inp = make_flat_price_input()
        inp.battery_soc_pct = 10.0
        output = run_planner(inp)
        offenders = [
            s
            for s in output.slots
            if s.recommendation == _WINDOW and s.batteries_discharged_kwh <= 1e-9
        ]
        assert len(offenders) > 0


class TestSeasonIndependence:
    """An LP-idle slot gets the same label whatever the month."""

    @pytest.mark.parametrize("factor", [1.0, 5.0])
    def test_concentration_clears_nothing_on_milp_in_any_season(
        self, capture_concentration: list[ConcentrationStats], factor: float
    ) -> None:
        """Summer needed 5-12 clearings before the fix; winter always needed 0."""
        for name in ("summer", "winter"):
            capture_concentration.clear()
            run_planner(_scale_load(_BUILDERS[name](), factor))
            milp = [
                s for s in capture_concentration if s.candidate_name == CANDIDATE_MILP
            ]
            assert milp, f"no MILP concentration pass on {name}"
            for stats in milp:
                assert stats.cleared == 0, (
                    f"{name} load x{factor}: concentration still cleared "
                    f"{stats.cleared} slot(s) on the MILP candidate"
                )


class TestLabelOnlyChange:
    """The fix must not move a single kWh or currency unit."""

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

    def test_only_window_to_wait_transitions_occur(
        self, request: pytest.FixtureRequest
    ) -> None:
        """Every label that changes moves window → wait, never the reverse."""
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
            assert old == _WINDOW and new == _WAIT


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
        for slot in run_planner(_BUILDERS[name]()).slots:
            assert 0.0 <= slot.estimated_battery_soc_pct <= 100.0

    def test_winner_slots_are_the_published_slots(self) -> None:
        """Cost identity: the gate must not detach the winner from the output."""
        output = run_planner(make_summer_day_input())
        winner = next(c for c in output.candidates if c.name == output.winner_name)
        assert winner.slots is output.slots

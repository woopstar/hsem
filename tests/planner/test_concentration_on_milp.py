"""Concentration's behaviour on the MILP candidate (issue #1036).

Issue #1036 asked whether ``concentrate_discharge_on_expensive_slots`` should
run on the MILP candidate at all.  The measured answer was **yes, keep it** —
but not for the reason its docstring gives.  On the MILP candidate it has no
effect on plan cost whatsoever; its entire effect is to replace the seasonal
fill's ``batteries_discharge_window_mode`` label on LP-idle slots with
``batteries_wait_mode``, which is the difference between the applier running
``MaximizeSelfConsumption`` and holding the battery at 0 W.

These tests lock in the three measured properties so a future change cannot
quietly remove the label effect while the cost-based tests stay green.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner import candidate_selector, run_planner
from custom_components.hsem.planner.candidate_generator import CANDIDATE_MILP
from custom_components.hsem.planner.discharge_scheduler import (
    ConcentrationStats,
    concentrate_discharge_on_expensive_slots,
)
from custom_components.hsem.utils.recommendations import Recommendations
from tests.planner.fixtures import make_summer_day_input


def _scale_load(inp: PlannerInput, factor: float) -> PlannerInput:
    """Scale house consumption so the per-day discharge budget is exceeded.

    Concentration only clears when a day's discharge need exceeds the battery
    budget.  At the stock fixture's load it never has to, so a test that did
    not scale would assert on a no-op.
    """
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
def capture_stats(monkeypatch: pytest.MonkeyPatch) -> list[ConcentrationStats]:
    """Record every concentration pass the selector runs."""
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


@pytest.fixture
def skip_milp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip concentration for the MILP candidate only."""
    real = concentrate_discharge_on_expensive_slots

    def wrapper(*args: object, **kwargs: object) -> ConcentrationStats:
        name = kwargs.get("candidate_name", "")
        if name == CANDIDATE_MILP:
            return ConcentrationStats(candidate_name=str(name))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        candidate_selector, "concentrate_discharge_on_expensive_slots", wrapper
    )


class TestConcentrationStatsInstrumentation:
    """Issue #1036 step 1: the split must be visible per candidate."""

    def test_stats_are_reported_per_candidate(
        self, capture_stats: list[ConcentrationStats]
    ) -> None:
        run_planner(_scale_load(make_summer_day_input(), 5.0))
        names = [s.candidate_name for s in capture_stats]
        assert CANDIDATE_MILP in names, f"no MILP pass recorded, got {names}"
        assert all(names), "every pass must be attributed to a candidate"

    def test_cleared_is_the_sum_of_its_parts(
        self, capture_stats: list[ConcentrationStats]
    ) -> None:
        run_planner(_scale_load(make_summer_day_input(), 5.0))
        for stats in capture_stats:
            assert stats.cleared == (stats.cleared_seasonal_fill + stats.cleared_other)

    def test_empty_plan_returns_zeroed_stats(self) -> None:
        """No discharge slots means an early return that still reports."""
        stats = concentrate_discharge_on_expensive_slots(
            [],
            datetime.now().astimezone(),
            current_kwh=5.0,
            usable_kwh=9.0,
            max_discharge_per_slot=1.25,
            candidate_name="milp",
        )
        assert stats.candidate_name == "milp"
        assert stats.cleared == 0
        assert stats.kept == 0


class TestConcentrationOnMilpCandidate:
    """The measured properties from issue #1036."""

    @pytest.mark.parametrize("factor", [3.0, 5.0, 8.0])
    def test_only_seasonal_fill_slots_are_cleared(
        self, capture_stats: list[ConcentrationStats], factor: float
    ) -> None:
        """Post-#1032, concentration never touches an LP-dispatched slot.

        Everything it clears on the MILP candidate carried the seasonal fill's
        window label, so ``cleared_other`` must stay at zero.
        """
        run_planner(_scale_load(make_summer_day_input(), factor))
        milp = [s for s in capture_stats if s.candidate_name == CANDIDATE_MILP]
        assert milp, "expected a MILP concentration pass"
        for stats in milp:
            assert stats.cleared_other == 0, (
                f"concentration cleared {stats.cleared_other} non-seasonal-fill "
                f"slot(s) on the MILP candidate"
            )

    def test_skipping_leaves_cost_and_energy_identical(
        self, request: pytest.FixtureRequest
    ) -> None:
        """Plan cost is bit-identical with concentration on vs skipped.

        Not "within epsilon" — the MILP candidate is simulated with
        ``milp_prepopulated=True`` so energy is trusted verbatim, and labels
        carry no price.  Any drift here means concentration started changing
        MILP energy, which would be a regression.
        """
        inp = _scale_load(make_summer_day_input(), 5.0)
        on = run_planner(inp)
        request.getfixturevalue("skip_milp")
        off = run_planner(inp)

        assert on.winner_name == off.winner_name
        assert on.plan_cost is not None and off.plan_cost is not None
        assert on.plan_cost.total_cost == off.plan_cost.total_cost
        assert on.plan_cost.score == off.plan_cost.score
        for a, b in zip(on.slots, off.slots, strict=True):
            assert a.batteries_discharged_kwh == pytest.approx(
                b.batteries_discharged_kwh, abs=1e-12
            )
            assert a.batteries_charged_kwh == pytest.approx(
                b.batteries_charged_kwh, abs=1e-12
            )

    def test_skipping_is_now_a_no_op_on_the_milp_candidate(
        self, request: pytest.FixtureRequest
    ) -> None:
        """Concentration has nothing left to do on the MILP candidate.

        Until issue #1041 this asserted the opposite: skipping concentration
        flipped LP-idle slots from ``batteries_wait_mode`` back to
        ``batteries_discharge_window_mode``, which executes as
        MaximizeSelfConsumption and let the firmware drain the battery.  That
        label effect was concentration's real job here (issue #1036).

        #1041 removed the *cause* instead — the seasonal fill no longer opens a
        discharge window on a slot the LP declined — so concentration now finds
        nothing to thin on this candidate and skipping it changes nothing.
        Concentration still runs, because that is a property of the current
        fill rather than a guarantee, and it stays load-bearing for the
        non-MILP candidates.
        """
        inp = _scale_load(make_summer_day_input(), 5.0)
        on = run_planner(inp)
        request.getfixturevalue("skip_milp")
        off = run_planner(inp)

        flips = [
            (a.start.isoformat(), a.recommendation, b.recommendation)
            for a, b in zip(on.slots, off.slots, strict=True)
            if a.recommendation != b.recommendation
        ]
        assert flips == [], (
            f"concentration still changes labels on the MILP candidate: {flips[:5]} "
            f"— the seasonal fill is producing discharge windows on LP-idle slots "
            f"again (issue #1041)"
        )

    def test_1032_invariant_holds_either_way(
        self, request: pytest.FixtureRequest
    ) -> None:
        """No wait slot may retain discharge, with or without concentration."""
        inp = _scale_load(make_summer_day_input(), 5.0)
        outputs = [run_planner(inp)]
        request.getfixturevalue("skip_milp")
        outputs.append(run_planner(inp))

        for output in outputs:
            offenders = [
                s.start.isoformat()
                for s in output.slots
                if s.recommendation == Recommendations.BatteriesWaitMode.value
                and s.batteries_discharged_kwh > 1e-9
            ]
            assert offenders == []

    def test_soc_stays_in_bounds_either_way(
        self, request: pytest.FixtureRequest
    ) -> None:
        inp = _scale_load(make_summer_day_input(), 5.0)
        outputs = [run_planner(inp)]
        request.getfixturevalue("skip_milp")
        outputs.append(run_planner(inp))

        for output in outputs:
            for slot in output.slots:
                assert 0.0 <= slot.estimated_battery_soc_pct <= 100.0

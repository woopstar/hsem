"""Replay the committed corpus and check it against the planner spec (#1037).

These tests are the point of the harness: the invariants in
``docs/planner-spec.md`` run against **real** recorded planner inputs, not
synthetic fixtures. Issue #1032 shipped a label/energy contradiction to v6.3.2
while every fixture-based test passed, because no fixture contained the input
combination that produced it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.planner.engine_core import run_planner
from custom_components.hsem.utils.diagnostics import build_diagnostics_dump
from tests.backtest.conftest import corpus_paths, replayed_cycles
from tests.backtest.invariants import check_invariants, format_violations
from tests.backtest.replay import load_planner_input, planner_input_from_dict


def _assert_same_plan(a: PlannerOutput, b: PlannerOutput) -> None:
    """Assert two planner outputs are the same plan.

    ``PlannedSlot`` and ``PlanCostBreakdown`` are dataclasses, so comparing the
    objects is an exact field-by-field check — which is what "bit-identical"
    means here, and stronger than comparing a handful of scalars.
    """
    assert a.winner_name == b.winner_name
    assert [s.recommendation for s in a.slots] == [s.recommendation for s in b.slots]
    assert a.slots == b.slots
    assert a.plan_cost == b.plan_cost
    assert a.battery_soc_at_end == pytest.approx(b.battery_soc_at_end)
    assert a.current_recommendation == b.current_recommendation
    assert a.plan_consistency_violations == b.plan_consistency_violations


def test_corpus_is_not_empty() -> None:
    """A committed corpus is what makes this harness runnable in CI."""
    assert corpus_paths(), (
        "tests/backtest/corpus/ has no dumps — see its README for how to add one"
    )


class TestCorpusReplay:
    """Every committed dump must replay losslessly and hold the invariants."""

    def test_dump_replays_losslessly(self, corpus_dump: Path) -> None:
        """A corpus entry that no longer round-trips is drift, not a nuisance."""
        cycles = 0
        for index, _planner_input, report in replayed_cycles(corpus_dump):
            cycles += 1
            assert report.is_faithful, (
                f"{corpus_dump.name} cycle {index} no longer round-trips onto "
                f"the current PlannerInput:\n{report.describe()}\n"
                f"Regenerate it — see tests/backtest/corpus/README.md"
            )
        assert cycles, f"{corpus_dump.name} carries no cycles"

    def test_replay_produces_a_plan(self, corpus_dump: Path) -> None:
        for _index, planner_input, _report in replayed_cycles(corpus_dump):
            out = run_planner(planner_input)
            assert out.slots
            assert out.winner_name
            assert out.plan_cost is not None

    def test_spec_invariants_hold(self, corpus_dump: Path) -> None:
        """The ``docs/planner-spec.md`` invariants, against real inputs."""
        for index, planner_input, _report in replayed_cycles(corpus_dump):
            out = run_planner(planner_input)
            violations = check_invariants(planner_input, out)
            assert not violations, (
                f"{corpus_dump.name} cycle {index} violates the planner spec:\n"
                f"{format_violations(violations)}"
            )

    def test_replay_is_deterministic(self, corpus_dump: Path) -> None:
        """Same input, same process, same plan — twice."""
        for _index, planner_input, _report in replayed_cycles(corpus_dump):
            _assert_same_plan(run_planner(planner_input), run_planner(planner_input))

    def test_serialise_reload_reproduces_an_identical_plan(
        self, corpus_dump: Path, tmp_path: Path
    ) -> None:
        """The full loop: dump -> input -> plan -> dump -> input -> same plan.

        This is the acceptance criterion for the shim. If serialisation lost a
        field that changes a decision, the second plan diverges here.
        """
        planner_input, _ = load_planner_input(corpus_dump)
        first = run_planner(planner_input)

        regenerated = build_diagnostics_dump(
            planner_input, first, None, integration_version="roundtrip-test"
        )
        path = tmp_path / "regenerated.json"
        path.write_text(json.dumps(regenerated), encoding="utf-8")

        reloaded, report = load_planner_input(path)
        assert report.is_faithful, report.describe()
        assert reloaded == planner_input
        _assert_same_plan(run_planner(reloaded), first)

    def test_dump_carries_no_entity_ids(self, corpus_dump: Path) -> None:
        """Committed dumps must stay redacted — they are a real home."""
        text = corpus_dump.read_text(encoding="utf-8")
        payload = json.loads(text)
        assert "**REDACTED**" not in text
        for domain in ("sensor.", "binary_sensor.", "input_number.", "switch."):
            assert domain not in text, f"{corpus_dump.name} leaks {domain}* ids"
        # The shim only ever reads planner_input; make sure it is really there.
        assert "planner_input" in payload.get("data", payload)


class TestCorpusCoverage:
    """What the corpus actually exercises, so gaps stay visible."""

    def test_corpus_exercises_a_multi_day_sub_hourly_horizon(self) -> None:
        """Guard the one thing fixtures habitually get wrong: scale."""
        shapes = []
        for path in corpus_paths():
            planner_input, _ = load_planner_input(path)
            shapes.append(
                (planner_input.interval_minutes, planner_input.interval_length_hours)
            )
        assert any(minutes < 60 for minutes, _ in shapes), (
            f"corpus has no sub-hourly cycle: {shapes}"
        )
        assert any(hours > 24 for _, hours in shapes), (
            f"corpus has no multi-day horizon: {shapes}"
        )

    def test_payload_shape_is_accepted_directly(self) -> None:
        """``planner_input_from_dict`` works on an in-memory payload too."""
        path = corpus_paths()[0]
        payload = json.loads(path.read_text(encoding="utf-8"))
        rebuilt, report = planner_input_from_dict(payload.get("data", payload))
        assert report.is_faithful
        assert rebuilt.price_points

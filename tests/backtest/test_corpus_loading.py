"""Tests for corpus discovery and multi-cycle dump loading (issue #1037)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.utils.diagnostics import _planner_input_to_dict
from tests.backtest import conftest as backtest_conftest
from tests.backtest.conftest import (
    CORPUS_ENV_VAR,
    DEFAULT_MAX_CYCLES,
    MAX_CYCLES_ENV_VAR,
    corpus_paths,
    max_cycles,
    replayed_cycles,
)
from tests.backtest.replay import iter_dumps, load_planner_input


def _payload(interval_minutes: int) -> dict[str, Any]:
    """Build a minimal diagnostics payload with a recognisable field."""
    return {
        "hsem_version": "7.0.0-test",
        "dump_timestamp": "2026-09-14T17:24:38+02:00",
        "planner_input": _planner_input_to_dict(
            PlannerInput(interval_minutes=interval_minutes)
        ),
    }


def _jsonl(
    tmp_path: Path, payloads: list[dict[str, Any]], *, blanks: bool = False
) -> Path:
    """Write payloads as an append log, optionally with blank lines."""
    path = tmp_path / "corpus.jsonl"
    lines = []
    for payload in payloads:
        lines.append(json.dumps(payload))
        if blanks:
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestIterDumps:
    """An append log and a single download must both read."""

    def test_single_json_yields_one_cycle(self, tmp_path: Path) -> None:
        path = tmp_path / "cycle.json"
        path.write_text(json.dumps(_payload(15)), encoding="utf-8")
        assert len(list(iter_dumps(path))) == 1

    def test_jsonl_yields_every_line(self, tmp_path: Path) -> None:
        path = _jsonl(tmp_path, [_payload(15), _payload(30), _payload(60)])
        dumps = list(iter_dumps(path))
        assert [d["planner_input"]["interval_minutes"] for d in dumps] == [15, 30, 60]

    def test_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        """A partially flushed append log must still read."""
        path = _jsonl(tmp_path, [_payload(15), _payload(30)], blanks=True)
        assert len(list(iter_dumps(path))) == 2

    def test_jsonl_lines_may_carry_the_ha_data_wrapper(self, tmp_path: Path) -> None:
        path = _jsonl(tmp_path, [{"data": _payload(15)}])
        assert next(iter_dumps(path))["planner_input"]["interval_minutes"] == 15

    def test_load_planner_input_reads_the_first_cycle(self, tmp_path: Path) -> None:
        path = _jsonl(tmp_path, [_payload(30), _payload(60)])
        rebuilt, report = load_planner_input(path)
        assert rebuilt.interval_minutes == 30
        assert report.is_faithful


class TestCorpusDiscovery:
    """The committed corpus, plus an opt-in private one."""

    def test_committed_corpus_is_found(self) -> None:
        assert corpus_paths()

    def test_external_corpus_is_appended(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "extra.jsonl").write_text(
            json.dumps(_payload(15)) + "\n", encoding="utf-8"
        )
        monkeypatch.setenv(CORPUS_ENV_VAR, str(tmp_path))
        names = [p.name for p in corpus_paths()]
        assert "extra.jsonl" in names
        assert any(name.startswith("cycle-") for name in names)

    def test_missing_external_corpus_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo'd path that silently replayed nothing would look clean."""
        monkeypatch.setenv(CORPUS_ENV_VAR, str(tmp_path / "nope"))
        with pytest.raises(ValueError, match=CORPUS_ENV_VAR):
            corpus_paths()


class TestCycleCap:
    """A three-week append log must not silently blow the test timeout."""

    def test_default_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(MAX_CYCLES_ENV_VAR, raising=False)
        assert max_cycles() == DEFAULT_MAX_CYCLES

    def test_cap_is_overridable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MAX_CYCLES_ENV_VAR, "3")
        assert max_cycles() == 3

    def test_replayed_cycles_stops_at_the_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _jsonl(tmp_path, [_payload(15) for _ in range(10)])
        monkeypatch.setenv(MAX_CYCLES_ENV_VAR, "4")
        assert len(list(replayed_cycles(path))) == 4

    def test_replayed_cycles_rebuilds_each_input(self, tmp_path: Path) -> None:
        path = _jsonl(tmp_path, [_payload(15), _payload(60)])
        cycles = list(replayed_cycles(path))
        assert [c[0] for c in cycles] == [0, 1]
        assert [c[1].interval_minutes for c in cycles] == [15, 60]
        assert all(c[2].is_faithful for c in cycles)

    def test_default_cap_constant_is_used(self) -> None:
        """Guard the constant the docs quote."""
        assert backtest_conftest.DEFAULT_MAX_CYCLES == 25

"""Tests for the per-module coverage floor gate.

The gate exists because ``pytest --cov`` only reports aggregate coverage, so a
single module can fall below the 95 % Silver threshold while the suite stays
green — which is what happened in PR #1067. Since the gate is now what stops
that, a silently broken gate is worse than no gate, so its own failure paths
are covered here: a missing report, an unparseable one, and a partial one must
all fail loudly rather than pass by default.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_coverage_floor.py"


def _load() -> ModuleType:
    """Load the checker script as a module."""
    spec = importlib.util.spec_from_file_location("check_coverage_floor", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_CHECKER = _load()


def _report(tmp_path: Path, modules: dict[str, tuple[int, int]]) -> Path:
    """Write a Cobertura report where each module has (covered, total) lines."""
    parts = ['<?xml version="1.0" ?>', "<coverage><packages><package><classes>"]
    for name, (covered, total) in modules.items():
        parts.append(f'<class filename="{name}"><lines>')
        for index in range(total):
            hits = 1 if index < covered else 0
            parts.append(f'<line number="{index + 1}" hits="{hits}"/>')
        parts.append("</lines></class>")
    parts.append("</classes></package></packages></coverage>")
    path = tmp_path / "coverage.xml"
    path.write_text("".join(parts))
    return path


class TestFloorEnforcement:
    """A module below the floor fails the gate; one at it passes."""

    def test_every_module_above_the_floor_passes(self, tmp_path: Path) -> None:
        """A fully covered report is accepted."""
        report = _report(tmp_path, {"hsem/a.py": (100, 100), "hsem/b.py": (96, 100)})

        assert _CHECKER.main(["--report", str(report)]) == 0

    def test_a_module_below_the_floor_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The offending module is named with its uncovered count."""
        report = _report(tmp_path, {"hsem/a.py": (100, 100), "hsem/b.py": (90, 100)})

        assert _CHECKER.main(["--report", str(report)]) == 1

        err = capsys.readouterr().err
        assert "hsem/b.py" in err
        assert "10 of 100 statements uncovered" in err
        # The passing module is not reported as a problem.
        assert "hsem/a.py" not in err

    def test_a_module_exactly_at_the_floor_passes(self, tmp_path: Path) -> None:
        """95 % is the threshold, not the first failing value."""
        report = _report(tmp_path, {"hsem/a.py": (95, 100)})

        assert _CHECKER.main(["--report", str(report)]) == 0

    def test_the_floor_is_configurable(self, tmp_path: Path) -> None:
        """A stricter floor rejects what the default accepts."""
        report = _report(tmp_path, {"hsem/a.py": (96, 100)})

        assert _CHECKER.main(["--report", str(report)]) == 0
        assert _CHECKER.main(["--report", str(report), "--floor", "99"]) == 1

    def test_a_module_with_no_statements_is_ignored(self, tmp_path: Path) -> None:
        """An empty module has nothing to cover and is not 0 %."""
        report = _report(tmp_path, {"hsem/a.py": (100, 100), "hsem/empty.py": (0, 0)})

        assert _CHECKER.main(["--report", str(report)]) == 0


class TestUnusableReports:
    """The gate fails closed on anything it cannot trust."""

    def test_a_missing_report_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No report means the tests did not run — that is not a pass."""
        assert _CHECKER.main(["--report", str(tmp_path / "absent.xml")]) == 1
        assert "not found" in capsys.readouterr().err

    def test_an_unparseable_report_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A truncated report is reported, not silently skipped."""
        path = tmp_path / "coverage.xml"
        path.write_text("<coverage><packages>")

        assert _CHECKER.main(["--report", str(path)]) == 1
        assert "could not parse" in capsys.readouterr().err

    def test_a_report_with_no_modules_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty report usually means a partial run, so fail closed."""
        report = _report(tmp_path, {})

        assert _CHECKER.main(["--report", str(report)]) == 1
        assert "no modules" in capsys.readouterr().err


class TestRealReportShape:
    """The checker understands the report the suite actually writes."""

    def test_the_repo_report_is_accepted_if_present(self) -> None:
        """A real coverage.xml parses into a non-empty module map."""
        report = Path(__file__).resolve().parents[1] / "coverage.xml"
        if not report.is_file():
            pytest.skip("coverage.xml not written in this run")

        modules = _CHECKER._module_coverage(report)

        assert modules
        # Paths are relative to the coverage source dir.
        assert all(name.startswith("hsem/") for name in modules)

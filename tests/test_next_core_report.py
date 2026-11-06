"""Tests for the next-core tracking issue builder (issue #1372).

Home Assistant 2026.11 broke HSEM twice and the pinned CI run saw neither
(issue #1370). ``scripts/next_core_report.py`` turns a failed run against the
next core pre-release into one tracking issue per core version; its title,
the test and mypy extraction and the body's caps are pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.script_loader import load_script

_REPORT = load_script("next_core_report")

_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="1" failures="2" skipped="1" tests="5">
    <testcase classname="tests.test_a.TestOne" name="test_passes" time="0.001"/>
    <testcase classname="tests.test_a.TestOne" name="test_fails" time="0.001">
      <failure message="assert 1 == 2">traceback</failure>
    </testcase>
    <testcase classname="tests.test_b" name="test_errors" time="0.001">
      <error message="ImportError">traceback</error>
    </testcase>
    <testcase classname="tests.test_b" name="test_skipped" time="0.001">
      <skipped message="no hardware"/>
    </testcase>
    <testcase classname="tests.test_a.TestOne" name="test_fails" time="0.001">
      <failure message="rerun">traceback</failure>
    </testcase>
  </testsuite>
</testsuites>
"""

_MYPY_LOG = """custom_components/hsem/utils/ocpp_tls.py:48: error: Module "homeassistant.util.ssl" has no attribute "server_context_modern"  [attr-defined]
custom_components/hsem/x.py:1: note: See https://mypy.readthedocs.io
Found 1 error in 1 file (checked 400 source files)
"""


class TestIssueTitle:
    """One title per core version, searchable with ``in:title``."""

    def test_names_the_version(self) -> None:
        title = _REPORT.issue_title("2026.12.0b1")

        assert title == "chore(ci): test suite fails on Home Assistant 2026.12.0b1"


class TestFailingTests:
    """Failures and errors are listed; passes, skips and duplicates are not."""

    def test_lists_failures_and_errors_once(self, tmp_path: Path) -> None:
        report = tmp_path / "junit.xml"
        report.write_text(_JUNIT)

        assert _REPORT.failing_tests(report) == [
            "tests.test_a.TestOne::test_fails",
            "tests.test_b::test_errors",
        ]

    def test_missing_report_is_empty(self, tmp_path: Path) -> None:
        assert _REPORT.failing_tests(tmp_path / "absent.xml") == []


class TestMypyErrors:
    """Only ``error:`` lines count; notes and the summary line do not."""

    def test_keeps_error_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "mypy.log"
        log.write_text(_MYPY_LOG)

        lines = _REPORT.mypy_errors(log)

        assert len(lines) == 1
        assert lines[0].startswith(
            "custom_components/hsem/utils/ocpp_tls.py:48: error:"
        )

    def test_missing_log_is_empty(self, tmp_path: Path) -> None:
        assert _REPORT.mypy_errors(tmp_path / "absent.log") == []


class TestBuildBody:
    """The body names the version, the run, each failure and how to reproduce."""

    def test_lists_tests_and_mypy_errors(self) -> None:
        body = _REPORT.build_body(
            "2026.12.0b1",
            "failure",
            "failure",
            ["tests.test_a::test_x"],
            ["a.py:1: error: boom  [misc]"],
            "https://example.test/run/1",
        )

        assert "Home Assistant **2026.12.0b1**" in body
        assert "https://example.test/run/1" in body
        assert "- pytest: failure" in body
        assert "- mypy: failure" in body
        assert "## Failing tests (1)" in body
        assert "- `tests.test_a::test_x`" in body
        assert "## mypy errors (1)" in body
        assert "- `a.py:1: error: boom  [misc]`" in body
        assert "homeassistant==2026.12.0b1" in body
        assert "#1370" in body

    def test_pytest_failure_without_report_is_explained(self) -> None:
        body = _REPORT.build_body("2026.12.0b1", "failure", "success", [], [], "")

        assert "pytest failed before it wrote a report" in body
        assert "## mypy errors" not in body

    def test_mypy_only_failure_has_no_test_section(self) -> None:
        body = _REPORT.build_body(
            "2026.12.0b1", "success", "failure", [], ["a.py:1: error: x  [misc]"], ""
        )

        assert "## Failing tests" not in body
        assert "## mypy errors (1)" in body

    @pytest.mark.parametrize(
        ("count", "limit", "what"),
        [
            (_REPORT.MAX_TESTS + 7, _REPORT.MAX_TESTS, "tests"),
            (_REPORT.MAX_MYPY_LINES + 3, _REPORT.MAX_MYPY_LINES, "errors"),
        ],
    )
    def test_long_lists_are_capped(self, count: int, limit: int, what: str) -> None:
        items = [f"item{i}" for i in range(count)]
        tests = items if what == "tests" else []
        mypy_lines = items if what == "errors" else []

        body = _REPORT.build_body("v", "failure", "failure", tests, mypy_lines, "")

        assert f"- `item{limit - 1}`" in body
        assert f"- `item{limit}`" not in body
        assert f"- and {count - limit} more {what} (see the run log)" in body


class TestMain:
    """The command line prints the title and writes the body."""

    def test_title_command_prints_the_title(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _REPORT.main(["title", "--version", "2026.12.0b1"]) == 0

        out = capsys.readouterr().out.strip()
        assert out == "chore(ci): test suite fails on Home Assistant 2026.12.0b1"

    def test_body_command_writes_the_file(self, tmp_path: Path) -> None:
        junit = tmp_path / "junit.xml"
        junit.write_text(_JUNIT)
        log = tmp_path / "mypy.log"
        log.write_text(_MYPY_LOG)
        out = tmp_path / "issue.md"

        code = _REPORT.main(
            [
                "body",
                "--version",
                "2026.12.0b1",
                "--pytest-status",
                "failure",
                "--mypy-status",
                "failure",
                "--junit",
                str(junit),
                "--mypy-log",
                str(log),
                "--run-url",
                "https://example.test/run/2",
                "--out",
                str(out),
            ]
        )

        assert code == 0
        body = out.read_text()
        assert "## Failing tests (2)" in body
        assert "- `tests.test_b::test_errors`" in body
        assert "## mypy errors (1)" in body
        assert "https://example.test/run/2" in body

    def test_body_command_tolerates_missing_inputs(self, tmp_path: Path) -> None:
        out = tmp_path / "issue.md"

        code = _REPORT.main(
            [
                "body",
                "--version",
                "2026.12.0b1",
                "--pytest-status",
                "failure",
                "--mypy-status",
                "success",
                "--junit",
                str(tmp_path / "absent.xml"),
                "--mypy-log",
                str(tmp_path / "absent.log"),
                "--out",
                str(out),
            ]
        )

        assert code == 0
        assert "pytest failed before it wrote a report" in out.read_text()

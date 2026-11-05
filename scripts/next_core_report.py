#!/usr/bin/env python3
"""Build the tracking issue for a failed run against the next Home Assistant core.

``.github/workflows/next-core.yml`` runs the test suite and mypy against the
newest Home Assistant pre-release every day (issue #1372). Home Assistant
2026.11 removed ``homeassistant.util.ssl.server_context_modern`` and added a
required ``device_class`` keyword to ``IntegrationSensor.__init__`` (issue
#1370); both were visible on the beta a week before the stable release, and
the pinned CI run could not see either.

This script turns the run's results into the tracking issue's title and body.
The workflow searches open issues for the title, comments on a match and
creates the issue otherwise, so one core version gets one issue.

Usage:
    python3 scripts/next_core_report.py title --version 2026.12.0b1
    python3 scripts/next_core_report.py body --version 2026.12.0b1 \\
        --pytest-status failure --mypy-status success \\
        --junit next-core-results.xml --mypy-log mypy.log \\
        --run-url https://github.com/owner/repo/actions/runs/1 \\
        --out issue.md
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ISSUE_TITLE = "chore(ci): test suite fails on Home Assistant {version}"
#: Failing tests listed in the body; the run log has the rest.
MAX_TESTS = 50
#: mypy error lines listed in the body.
MAX_MYPY_LINES = 40


def issue_title(version: str) -> str:
    """Return the tracking issue title for *version*.

    Args:
        version: The installed Home Assistant version, e.g. ``2026.12.0b1``.

    Returns:
        The title the workflow searches open issues for.
    """
    return ISSUE_TITLE.format(version=version)


def failing_tests(report: Path) -> list[str]:
    """Return ``classname::name`` for every failed or errored test in *report*.

    Args:
        report: A JUnit XML report written by ``pytest --junitxml``.

    Returns:
        Test identifiers in report order, without duplicates. Empty when the
        report is missing (the run may have failed before pytest wrote it).
    """
    if not report.is_file():
        return []
    seen: list[str] = []
    for case in ET.parse(report).getroot().iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        classname = case.get("classname", "")
        name = case.get("name", "")
        test_id = f"{classname}::{name}" if classname else name
        if test_id not in seen:
            seen.append(test_id)
    return seen


def mypy_errors(log: Path) -> list[str]:
    """Return the ``error:`` lines of a mypy log.

    Args:
        log: The captured mypy output.

    Returns:
        The error lines in log order. Empty when the log is missing.
    """
    if not log.is_file():
        return []
    return [
        line.rstrip()
        for line in log.read_text(encoding="utf-8").splitlines()
        if ": error:" in line
    ]


def _capped(items: list[str], limit: int, what: str) -> list[str]:
    """Return *items* as Markdown lines, truncated to *limit* with a note."""
    lines = [f"- `{item}`" for item in items[:limit]]
    if len(items) > limit:
        lines.append(f"- and {len(items) - limit} more {what} (see the run log)")
    return lines


def build_body(
    version: str,
    pytest_status: str,
    mypy_status: str,
    tests: list[str],
    mypy_lines: list[str],
    run_url: str,
) -> str:
    """Return the Markdown body of the tracking issue or follow-up comment.

    Args:
        version: The installed Home Assistant version.
        pytest_status: The pytest step outcome (``success`` or ``failure``).
        mypy_status: The mypy step outcome (``success`` or ``failure``).
        tests: Failing test identifiers from :func:`failing_tests`.
        mypy_lines: mypy error lines from :func:`mypy_errors`.
        run_url: The workflow run to link.

    Returns:
        The Markdown text.
    """
    parts = [
        f"The daily run against Home Assistant **{version}** failed "
        f"(`next-core.yml`, {run_url}).",
        "",
        f"- pytest: {pytest_status}",
        f"- mypy: {mypy_status}",
    ]
    if tests:
        parts += ["", f"## Failing tests ({len(tests)})", ""]
        parts += _capped(tests, MAX_TESTS, "tests")
    elif pytest_status != "success":
        parts += [
            "",
            "## Failing tests",
            "",
            "pytest failed before it wrote a report (collection or import "
            "error); see the run log.",
        ]
    if mypy_lines:
        parts += ["", f"## mypy errors ({len(mypy_lines)})", ""]
        parts += _capped(mypy_lines, MAX_MYPY_LINES, "errors")
    parts += [
        "",
        "## Reproduce",
        "",
        "```bash",
        f"python3 -m venv /tmp/ha-next && /tmp/ha-next/bin/pip install "
        f"homeassistant=={version} -r requirements_test.txt -r requirements_lint.txt",
        '/tmp/ha-next/bin/python -m pytest -o addopts="" tests/',
        "/tmp/ha-next/bin/mypy custom_components tests",
        "```",
        "",
        "The pinned `requirements.txt` core is unaffected; this is the next "
        "release. Keep version-dependent core names behind `getattr` or "
        "`inspect.signature` so both cores work (see #1370).",
    ]
    return "\n".join(parts) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    title = sub.add_parser("title", help="print the tracking issue title")
    title.add_argument("--version", required=True)

    body = sub.add_parser("body", help="write the tracking issue body")
    body.add_argument("--version", required=True)
    body.add_argument("--pytest-status", required=True)
    body.add_argument("--mypy-status", required=True)
    body.add_argument("--junit", type=Path, default=Path("next-core-results.xml"))
    body.add_argument("--mypy-log", type=Path, default=Path("mypy.log"))
    body.add_argument("--run-url", default="")
    body.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line.

    Args:
        argv: Arguments without the program name; ``None`` reads ``sys.argv``.

    Returns:
        The process exit code.
    """
    args = _parser().parse_args(argv)
    if args.command == "title":
        print(issue_title(args.version))
        return 0
    body = build_body(
        args.version,
        args.pytest_status,
        args.mypy_status,
        failing_tests(args.junit),
        mypy_errors(args.mypy_log),
        args.run_url,
    )
    args.out.write_text(body, encoding="utf-8")
    print(f"wrote {args.out} ({len(body)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

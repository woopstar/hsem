#!/usr/bin/env python3
"""Enforce the Home Assistant Silver ``test-coverage`` rule per module.

The rule requires **every** module of the integration to exceed 95 % coverage,
not just the aggregate. ``pytest --cov`` only reports the aggregate and
``coverage.py`` has no per-file threshold, so a new or reworked module can drop
well below the floor while the suite stays green — which is exactly how
``entity_availability.py`` landed at 90.38 % in PR #1067 without CI noticing.

Reads the ``coverage.xml`` Cobertura report written by the test run and exits
non-zero if any module is below the floor.

Usage:
    python3 scripts/check_coverage_floor.py [--report PATH] [--floor PCT]
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# The Silver quality-scale threshold; see custom_components/hsem/quality_scale.yaml.
DEFAULT_FLOOR_PCT = 95.0
DEFAULT_REPORT = Path("coverage.xml")


def _module_coverage(report: Path) -> dict[str, tuple[float, int, int]]:
    """Return ``{module: (percent, missing, total)}`` for every module in *report*.

    Args:
        report: Path to a Cobertura XML coverage report.

    Returns:
        Mapping of module path to its coverage percentage, uncovered statement
        count, and total statement count.

    Raises:
        ET.ParseError: If the report is not parseable XML.
    """
    root = ET.parse(report).getroot()
    modules: dict[str, tuple[float, int, int]] = {}
    for element in root.iter("class"):
        filename = element.get("filename")
        if filename is None:
            continue
        lines = list(element.iter("line"))
        if not lines:
            # A module with no measurable statements cannot be below the floor.
            continue
        total = len(lines)
        covered = sum(1 for line in lines if line.get("hits") != "0")
        modules[filename] = (100.0 * covered / total, total - covered, total)
    return modules


def main(argv: list[str] | None = None) -> int:
    """Check every module in the coverage report against the floor.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` when every module meets the floor, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--floor", type=float, default=DEFAULT_FLOOR_PCT)
    args = parser.parse_args(argv)

    if not args.report.is_file():
        print(
            f"[error] coverage report not found: {args.report} — "
            "run the tests first so the report is written",
            file=sys.stderr,
        )
        return 1

    try:
        modules = _module_coverage(args.report)
    except ET.ParseError as exc:
        print(f"[error] could not parse {args.report}: {exc}", file=sys.stderr)
        return 1

    if not modules:
        print(
            f"[error] {args.report} contains no modules — the report is probably "
            "from a partial test run",
            file=sys.stderr,
        )
        return 1

    below = sorted(
        (pct, name, missing, total)
        for name, (pct, missing, total) in modules.items()
        if pct < args.floor
    )

    if below:
        print(
            f"[error] {len(below)} of {len(modules)} modules are below the "
            f"{args.floor:.0f}% per-module floor required by the Silver "
            "test-coverage rule:",
            file=sys.stderr,
        )
        for pct, name, missing, total in below:
            print(
                f"  {pct:6.2f}%  {name}  ({missing} of {total} statements uncovered)",
                file=sys.stderr,
            )
        print(
            "\n[info] Cover the missing lines, or — if they are genuinely "
            "unreachable through the public interface — restructure so the "
            "branch is testable. Do not lower this floor.",
            file=sys.stderr,
        )
        return 1

    print(
        f"[ok] all {len(modules)} modules are at or above the "
        f"{args.floor:.0f}% per-module floor"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

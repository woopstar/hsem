"""Replay an HSEM diagnostics dump through the current planner.

Thin command-line front end for the Stage 1 backtest harness.  The
reconstruction shim itself lives in ``tests/backtest/replay.py`` (see
``docs/backtest-harness.md``); this script only adds argument parsing and
reporting so a real production dump can be inspected without writing a test.

Usage::

    python3 scripts/replay_planner_input.py logs/extati-diagnostics.json
    python3 scripts/replay_planner_input.py logs/extati-diagnostics.json \
        --regenerate tests/backtest/corpus/cycle.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from custom_components.hsem.planner.engine_core import run_planner  # noqa: E402
from custom_components.hsem.utils.diagnostics import (  # noqa: E402
    build_diagnostics_dump,
)
from tests.backtest.invariants import (  # noqa: E402
    check_invariants,
    format_violations,
)
from tests.backtest.replay import load_planner_input  # noqa: E402


def _integration_version() -> str:
    """Return the version of the code doing the replay.

    A regenerated corpus entry is emitted by *this* checkout, not by whatever
    version produced the source dump, so that is what the dump records.  The
    originating cycle's version stays visible in the replay report and in
    ``tests/backtest/corpus/README.md``.

    Returns:
        The ``version`` field of the integration manifest.
    """
    manifest = _REPO_ROOT / "custom_components" / "hsem" / "manifest.json"
    return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])


def main(argv: list[str] | None = None) -> int:
    """Replay a dump, report replay fidelity and invariant violations.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` when the replay found no invariant violations, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", help="Path to a diagnostics JSON file")
    parser.add_argument(
        "--regenerate",
        metavar="PATH",
        help=(
            "Re-emit the replayed cycle as a current-schema diagnostics dump. "
            "Used to refresh tests/backtest/corpus/ after a PlannerInput "
            "field is added or removed."
        ),
    )
    args = parser.parse_args(argv)

    planner_input, report = load_planner_input(args.dump)
    print(report.describe())

    planner_output = run_planner(planner_input)
    print(
        f"replayed {len(planner_output.slots)} slots  "
        f"winner={planner_output.winner_name!r}  "
        f"terminal_soc={planner_output.battery_soc_at_end:.1f}%"
    )

    violations = check_invariants(planner_input, planner_output)
    print(f"invariants: {len(violations)} violation(s)")
    print(format_violations(violations))

    if args.regenerate:
        regenerated = build_diagnostics_dump(
            planner_input,
            planner_output,
            None,
            integration_version=_integration_version(),
        )
        # Keep the source cycle's timestamp rather than "now", so regenerating
        # an unchanged corpus entry produces an unchanged file.
        regenerated["dump_timestamp"] = report.dump_timestamp
        Path(args.regenerate).write_text(
            json.dumps(regenerated, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {args.regenerate}")

    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())

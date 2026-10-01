"""Split a day's regret into forecast error and planner error (issue #1208).

Replays a day slot by slot through the planner, once on the forecasts HSEM
recorded and once on the realized values, and compares both with the
perfect-foresight oracle.  See ``docs/backtest-harness.md``.

It needs a planner cycle for (almost) every slot of the day, so it runs on a
live corpus, not on the committed sample.  A day takes a few minutes: every
slot is planned twice.

Usage::

    python3 scripts/backtest_attribute.py ~/hsem-actuals/actuals.json \\
        --corpus ~/hsem-actuals/corpus --day 2026-09-29
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.backtest.actuals import load_actuals  # noqa: E402
from tests.backtest.attribution import (  # noqa: E402
    DayAttribution,
    attribute_day,
    cycles_by_slot,
    describe,
)
from tests.backtest.replay import (  # noqa: E402
    generous_solver_limit,
    iter_dumps,
    planner_input_from_dict,
)
from tests.backtest.scoring import SiteLimits, merge_actuals  # noqa: E402


def _files(targets: list[str], patterns: tuple[str, ...]) -> list[Path]:
    """Expand files and directories into files matching *patterns*."""
    files: list[Path] = []
    for target in targets:
        path = Path(target).expanduser()
        if path.is_dir():
            for pattern in patterns:
                files.extend(sorted(path.glob(pattern)))
        elif path.is_file():
            files.append(path)
        else:
            raise SystemExit(f"[error] no such file or directory: {path}")
    return files


def main(argv: list[str] | None = None) -> int:
    """Attribute the regret of the requested days and print the table.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` when at least one day was attributed, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("actuals", nargs="+", help="actuals files or directories")
    parser.add_argument(
        "--corpus",
        nargs="+",
        required=True,
        help="corpus files or directories holding the days' planner cycles",
    )
    parser.add_argument(
        "--day",
        action="append",
        required=True,
        type=date.fromisoformat,
        help="local day to attribute, YYYY-MM-DD; repeat for several",
    )
    parser.add_argument(
        "--tz",
        help="time zone that defines a day (default: the one the cycles record)",
    )
    args = parser.parse_args(argv)

    actuals = merge_actuals(
        [load_actuals(path) for path in _files(args.actuals, ("*.json",))]
    )
    payloads: list[dict[str, Any]] = [
        payload
        for path in _files(args.corpus, ("*.json", "*.jsonl"))
        for payload in iter_dumps(path)
    ]
    if not payloads:
        raise SystemExit("[error] the corpus holds no cycles")

    zone_name = args.tz or next(
        (
            zone
            for payload in payloads
            if (zone := payload["planner_input"].get("time_zone"))
        ),
        None,
    )
    if not zone_name:
        raise SystemExit("[error] the cycles record no time zone: pass --tz")
    zone = ZoneInfo(zone_name)

    results: list[DayAttribution] = []
    with generous_solver_limit():
        for day in sorted(set(args.day)):
            on_day = cycles_by_slot(payloads, day, zone, actuals.slot_minutes)
            if not on_day:
                result = DayAttribution(
                    day=day, unscorable="no planner cycle was recorded on this day"
                )
            else:
                # The limits come from a cycle of the day itself, so they
                # describe the installation that recorded it.
                planner_input, _report = planner_input_from_dict(
                    next(iter(on_day.values()))
                )
                site = SiteLimits.from_planner_input(planner_input)
                result = attribute_day(actuals, payloads, day, zone, site)
            results.append(result)
            print(f"[info] {day}: done", file=sys.stderr, flush=True)
    print(describe(results))
    return 0 if any(result.is_attributed for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())

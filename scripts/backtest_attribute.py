"""Split a day's regret into forecast error and planner error (issue #1208).

Replays a day slot by slot through the planner, once on the forecasts HSEM
recorded and once on the realized values, and compares both with the
perfect-foresight oracle.  See ``docs/backtest-harness.md``.

It needs a planner cycle for (almost) every slot of the day.  Without
arguments it attributes the recorded days committed in ``tests/backtest/days``
(issue #1229); a live corpus is passed explicitly.  A day at 15-minute slots
takes about half a minute: every slot is planned twice.

Usage::

    python3 scripts/backtest_attribute.py                  # the committed days
    python3 scripts/backtest_attribute.py ~/hsem-actuals/actuals.json \\
        --corpus ~/hsem-actuals/corpus --day 2026-09-29

Cycles and actuals are only compared when they carry the same site tag
(issue #1225).  A live corpus carries none, so its cycles are taken to be from
``$HSEM_BACKTEST_SITE`` (``--site-tag``), the tag the actuals were built with.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.backtest.actuals import Actuals, load_actuals  # noqa: E402
from tests.backtest.attribution import (  # noqa: E402
    DayAttribution,
    attribute_day,
    describe,
    site_cycles_by_slot,
)
from tests.backtest.recorded_day import (  # noqa: E402
    load_recorded_day,
    recorded_days,
)
from tests.backtest.replay import (  # noqa: E402
    generous_solver_limit,
    iter_dumps,
    planner_input_from_dict,
)
from tests.backtest.scoring import SiteLimits, merge_actuals  # noqa: E402
from tests.backtest.site import SITE_ENV, SITE_KEY, validate_site  # noqa: E402


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


def _zone(explicit: str | None, payloads: list[dict[str, Any]]) -> ZoneInfo:
    """Return the zone that defines a day: the flag, then the cycles."""
    zone_name = explicit or next(
        (
            zone
            for payload in payloads
            if (zone := payload["planner_input"].get("time_zone"))
        ),
        None,
    )
    if not zone_name:
        raise SystemExit("[error] the cycles record no time zone: pass --tz")
    return ZoneInfo(zone_name)


def _attribute(
    actuals: Actuals,
    payloads: list[dict[str, Any]],
    days: list[date],
    zone: ZoneInfo,
) -> list[DayAttribution]:
    """Attribute *days*, taking the limits from a cycle of each day."""
    results: list[DayAttribution] = []
    with generous_solver_limit():
        for day in days:
            on_day = site_cycles_by_slot(
                payloads, day, zone, actuals.slot_minutes, actuals.site_tag
            )
            if isinstance(on_day, str):
                result = DayAttribution(day=day, unscorable=on_day)
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
    return results


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
    parser.add_argument(
        "actuals",
        nargs="*",
        help="actuals files or directories (default: the committed recorded days)",
    )
    parser.add_argument(
        "--corpus",
        nargs="+",
        help="corpus files or directories holding the days' planner cycles",
    )
    parser.add_argument(
        "--day",
        action="append",
        type=date.fromisoformat,
        help="local day to attribute, YYYY-MM-DD; repeat for several",
    )
    parser.add_argument(
        "--tz",
        help="time zone that defines a day (default: the one the cycles record)",
    )
    parser.add_argument(
        "--site-tag",
        default=os.environ.get(SITE_ENV),
        help=(
            "site tag assumed for cycles that carry none, as a live corpus "
            f"does (default: ${SITE_ENV})"
        ),
    )
    args = parser.parse_args(argv)

    results: list[DayAttribution] = []
    if not args.actuals:
        # The recorded days committed to the repository (issue #1229).
        committed = recorded_days()
        wanted = sorted(set(args.day)) if args.day else sorted(committed)
        if not wanted:
            raise SystemExit(
                "[error] no recorded day is committed: pass actuals, --corpus and --day"
            )
        for day in wanted:
            if day not in committed:
                results.append(
                    DayAttribution(day=day, unscorable="no recorded day is committed")
                )
                continue
            payloads, actuals = load_recorded_day(committed[day])
            results.extend(
                _attribute(actuals, payloads, [day], _zone(args.tz, payloads))
            )
        print(describe(results))
        return 0 if any(result.is_attributed for result in results) else 1

    if not args.corpus or not args.day:
        raise SystemExit("[error] with actuals, --corpus and --day are required")
    try:
        actuals = merge_actuals(
            [load_actuals(path) for path in _files(args.actuals, ("*.json",))]
        )
        corpus_tag = validate_site(args.site_tag)
    except ValueError as err:
        raise SystemExit(f"[error] {err}") from err
    payloads = [
        payload
        for path in _files(args.corpus, ("*.json", "*.jsonl"))
        for payload in iter_dumps(path)
    ]
    if not payloads:
        raise SystemExit("[error] the corpus holds no cycles")
    if corpus_tag is not None:
        for payload in payloads:
            payload.setdefault(SITE_KEY, corpus_tag)

    results = _attribute(
        actuals, payloads, sorted(set(args.day)), _zone(args.tz, payloads)
    )
    print(describe(results))
    return 0 if any(result.is_attributed for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())

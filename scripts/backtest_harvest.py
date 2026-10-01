"""Replay new live cycles and grow the committed backtest corpus.

Front end for ``tests/backtest/harvest.py``; usually run through
``scripts/backtest_update.sh``.  See ``docs/backtest-runbook.md``.

Only cycles newer than the last run are replayed: the resume point is kept in a
``.harvested-until`` file beside the live corpus.  ``--all`` ignores it.

After a ``PlannerInput`` field is added or removed, the corpus tests fail until
the committed cycles are refreshed::

    python3 scripts/backtest_harvest.py --refresh-corpus

A fully recorded day for the regret attribution is written with ``--day``
(issue #1229)::

    python3 scripts/backtest_harvest.py --day 2026-09-29

It writes ``tests/backtest/days/<date>/`` from the live corpus and the actuals,
prints the files for review and commits nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.backtest.harvest import (  # noqa: E402
    DEFAULT_MAX_CORPUS,
    DEFAULT_MAX_NEW,
    harvest,
    refresh_corpus,
    write_actuals_days,
)
from tests.backtest.recorded_day import (  # noqa: E402
    DAYS_DIR,
    refresh_recorded_days,
    write_recorded_day,
)
from tests.backtest.replay import iter_dumps  # noqa: E402
from tests.backtest.site import (  # noqa: E402
    SITE_ENV,
    describe_site,
    site_of,
    validate_site,
)

_DATA = Path.home() / "hsem-actuals"


def _live_files(target: Path) -> list[Path]:
    """Expand a live corpus file or directory into corpus files."""
    if target.is_dir():
        return sorted([*target.glob("*.json"), *target.glob("*.jsonl")])
    if target.is_file():
        return [target]
    raise SystemExit(f"[error] no live corpus at {target}")


def _record_day(args: argparse.Namespace) -> int:
    """Write one fully recorded day and print it for review (issue #1229).

    Args:
        args: Parsed command-line arguments with ``day`` set.

    Returns:
        ``0`` when the day passed every check, ``1`` otherwise.
    """
    if not args.time_zone:
        raise SystemExit("[error] --day needs the site's time zone: set TZ")
    try:
        zone = ZoneInfo(args.time_zone)
    except ZoneInfoNotFoundError as err:
        raise SystemExit(f"[error] unknown time zone {args.time_zone!r}") from err
    actuals_path = args.actuals.expanduser()
    if not actuals_path.exists():
        raise SystemExit(f"[error] no actuals at {actuals_path}")
    payloads = [
        payload
        for path in _live_files(args.live.expanduser())
        for payload in iter_dumps(path)
    ]
    try:
        result = write_recorded_day(
            payloads,
            json.loads(actuals_path.read_text(encoding="utf-8")),
            args.day,
            zone,
            args.site_tag,
            args.days_dir,
            dry_run=args.dry_run,
        )
    except ValueError as err:
        raise SystemExit(f"[error] {err}") from err
    print(result.describe())
    if result.written and not args.dry_run:
        print(
            "\nRead these files before committing them: they are one home's "
            "data.\n"
            f"  git add {result.directory}\n"
            f'  git commit -m "test(backtest): add the recorded day {args.day}"'
        )
    if args.dry_run:
        print("[dry run] nothing written")
    return 0 if result.written else 1


def main(argv: list[str] | None = None) -> int:
    """Harvest new situations into the committed corpus.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` when no replayed cycle violated an invariant, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        type=Path,
        default=Path(os.environ.get("HSEM_BACKTEST_CORPUS", _DATA / "corpus")),
        help="Live corpus file or directory (default: $HSEM_BACKTEST_CORPUS)",
    )
    parser.add_argument("--actuals", type=Path, default=_DATA / "actuals.json")
    parser.add_argument(
        "--corpus-dir", type=Path, default=_REPO_ROOT / "tests/backtest/corpus"
    )
    parser.add_argument(
        "--actuals-dir", type=Path, default=_REPO_ROOT / "tests/backtest/actuals"
    )
    parser.add_argument("--quarantine", type=Path, default=_DATA / "quarantine")
    parser.add_argument(
        "--time-zone",
        default=os.environ.get("TZ"),
        help="The site's IANA zone (default: $TZ)",
    )
    parser.add_argument(
        "--site-tag",
        default=os.environ.get(SITE_ENV),
        help=(
            "Tag of the installation the live corpus was recorded on, written "
            f"to every committed cycle (default: ${SITE_ENV}). Without one "
            "nothing is committed."
        ),
    )
    parser.add_argument("--max-new", type=int, default=DEFAULT_MAX_NEW)
    parser.add_argument("--max-corpus", type=int, default=DEFAULT_MAX_CORPUS)
    parser.add_argument("--all", action="store_true", help="Ignore the resume point")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report only; write nothing"
    )
    parser.add_argument(
        "--day",
        type=date.fromisoformat,
        help=(
            "Write one fully recorded day (YYYY-MM-DD) for the regret "
            "attribution into --days-dir, then exit. It needs a cycle in at "
            "least half of the day's slots and complete actuals for the day "
            "and the following day."
        ),
    )
    parser.add_argument("--days-dir", type=Path, default=DAYS_DIR)
    parser.add_argument(
        "--refresh-corpus",
        action="store_true",
        help=(
            "Bring the committed cycles back in step with PlannerInput after a "
            "field was added or removed, then exit. Needs no live corpus."
        ),
    )
    args = parser.parse_args(argv)
    if args.refresh_corpus:
        refreshed = refresh_corpus(args.corpus_dir, dry_run=args.dry_run)
        print(refreshed.describe())
        days = refresh_recorded_days(args.days_dir, dry_run=args.dry_run)
        if days.unchanged or days.updated or days.unfixable:
            print(f"recorded days: {days.describe()}")
        if args.dry_run:
            print("[dry run] nothing written")
        failed = refreshed.unfixable or refreshed.violations or days.unfixable
        return 1 if failed else 0
    if args.day is not None:
        return _record_day(args)
    # A .env value is taken literally, so a leading ~ arrives unexpanded.
    args.live = args.live.expanduser()
    args.actuals = args.actuals.expanduser()
    args.quarantine = args.quarantine.expanduser()

    zone = None
    if args.time_zone:
        try:
            zone = ZoneInfo(args.time_zone)
        except ZoneInfoNotFoundError as err:
            raise SystemExit(f"[error] unknown time zone {args.time_zone!r}") from err
    else:
        print(
            "[warn] TZ not set: pre-#1169 dumps cannot be committed, "
            "and no actuals will be written",
            file=sys.stderr,
        )

    try:
        site_tag = validate_site(args.site_tag)
    except ValueError as err:
        raise SystemExit(f"[error] {err}") from err
    if site_tag is None:
        print(
            f"[warn] {SITE_ENV} not set: cycles are replayed and checked, but "
            "none is committed",
            file=sys.stderr,
        )

    live_files = _live_files(args.live)
    state = (args.live if args.live.is_dir() else args.live.parent) / ".harvested-until"
    since = None
    if state.exists() and not args.all:
        since = datetime.fromisoformat(state.read_text(encoding="utf-8").strip())
        print(f"[info] resuming after {since.isoformat()} (--all to replay everything)")

    result = harvest(
        live_files,
        args.corpus_dir,
        time_zone=args.time_zone if zone else None,
        site=site_tag,
        since=since,
        max_new=args.max_new,
        max_corpus=args.max_corpus,
        quarantine_dir=args.quarantine,
        dry_run=args.dry_run,
    )
    print(result.describe())
    if result.violations and not args.dry_run:
        print(f"[info] violating cycles copied to {args.quarantine}")

    actuals_path = args.actuals
    actuals = (
        json.loads(actuals_path.read_text(encoding="utf-8"))
        if actuals_path.exists()
        else None
    )
    if zone is not None and actuals is not None and site_of(actuals) is None:
        print(
            f"[info] {actuals_path} carries no site tag; no actuals day committed "
            f"(set {SITE_ENV} and re-run scripts/collect_actuals.sh)"
        )
    elif zone is not None and actuals is not None:
        # Only days covered by a committed cycle of the same installation.
        actuals_site = site_of(actuals)
        written, incomplete = write_actuals_days(
            actuals,
            args.actuals_dir,
            result.horizon_days(actuals_site),
            zone,
            dry_run=args.dry_run,
        )
        print(f"actuals are from {describe_site(actuals_site)}")
        print(
            f"added actuals for {len(written)} day(s): "
            f"{', '.join(d.isoformat() for d in written) or '-'}"
        )
        if incomplete:
            print(
                "  not added, incomplete in the export: "
                f"{', '.join(d.isoformat() for d in incomplete)}"
            )
    elif zone is not None:
        print(f"[info] no actuals at {actuals_path}; skipped")

    if result.latest and not args.dry_run:
        state.write_text(result.latest + "\n", encoding="utf-8")
    if args.dry_run:
        print("[dry run] nothing written")
    return 1 if result.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())

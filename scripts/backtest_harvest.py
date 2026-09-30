"""Replay new live cycles and grow the committed backtest corpus.

Front end for ``tests/backtest/harvest.py``; usually run through
``scripts/backtest_update.sh``.  See ``docs/backtest-runbook.md``.

Only cycles newer than the last run are replayed: the resume point is kept in a
``.harvested-until`` file beside the live corpus.  ``--all`` ignores it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.backtest.harvest import (  # noqa: E402
    DEFAULT_MAX_CORPUS,
    DEFAULT_MAX_NEW,
    harvest,
    write_actuals_days,
)

_DATA = Path.home() / "hsem-actuals"


def _live_files(target: Path) -> list[Path]:
    """Expand a live corpus file or directory into corpus files."""
    if target.is_dir():
        return sorted([*target.glob("*.json"), *target.glob("*.jsonl")])
    if target.is_file():
        return [target]
    raise SystemExit(f"[error] no live corpus at {target}")


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
    parser.add_argument("--max-new", type=int, default=DEFAULT_MAX_NEW)
    parser.add_argument("--max-corpus", type=int, default=DEFAULT_MAX_CORPUS)
    parser.add_argument("--all", action="store_true", help="Ignore the resume point")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report only; write nothing"
    )
    args = parser.parse_args(argv)
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
    if zone is not None and actuals_path.exists():
        written, incomplete = write_actuals_days(
            json.loads(actuals_path.read_text(encoding="utf-8")),
            args.actuals_dir,
            result.horizon_days,
            zone,
            dry_run=args.dry_run,
        )
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

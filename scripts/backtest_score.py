"""Score realized days: cost, savings, regret and capture (issue #1208).

For every complete local day in the actuals this prints what was paid, what
plain inverter self-consumption would have cost, and what a perfect-foresight
oracle would have cost, and from those the savings, the regret and the share
of the day's potential that was captured.  See ``docs/backtest-harness.md``.

Usage::

    python3 scripts/backtest_score.py                      # the committed days
    python3 scripts/backtest_score.py ~/hsem-actuals/actuals.json
    python3 scripts/backtest_score.py ~/hsem-actuals/actuals.json \\
        --site tests/backtest/corpus/cycle-2026-09-25-0816.json

The battery and grid limits must be from the installation the actuals were
recorded on.  Without ``--site`` they come from the newest committed cycle
carrying the same site tag as the actuals (issue #1225).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.backtest.actuals import load_actuals  # noqa: E402
from tests.backtest.harvest import newest_cycle_of_site  # noqa: E402
from tests.backtest.replay import load_dump, load_planner_input  # noqa: E402
from tests.backtest.scoring import (  # noqa: E402
    SiteLimits,
    describe,
    merge_actuals,
    observed_days,
    score_days,
)
from tests.backtest.site import (  # noqa: E402
    SITE_ENV,
    describe_site,
    require_same_site,
    site_of,
    validate_site,
)

_ACTUALS_DIR = _REPO_ROOT / "tests" / "backtest" / "actuals"
_CORPUS_DIR = _REPO_ROOT / "tests" / "backtest" / "corpus"


def _actuals_files(targets: list[str]) -> list[Path]:
    """Expand files and directories into actuals files, sorted by name."""
    files: list[Path] = []
    for target in targets:
        path = Path(target).expanduser()
        if path.is_dir():
            files.extend(sorted(path.glob("*.json")))
        elif path.is_file():
            files.append(path)
        else:
            raise SystemExit(f"[error] no such actuals file or directory: {path}")
    if not files:
        raise SystemExit("[error] no actuals files found")
    return files


def _time_zone(explicit: str | None, files: list[Path], site_zone: str | None) -> str:
    """Return the zone that defines a day: the flag, the files, then the site."""
    if explicit:
        return explicit
    zones = {
        zone
        for path in files
        if (zone := json.loads(path.read_text(encoding="utf-8")).get("time_zone"))
    }
    if len(zones) > 1:
        raise SystemExit(f"[error] actuals files disagree on the zone: {sorted(zones)}")
    zone = next(iter(zones), None) or site_zone
    if not zone:
        raise SystemExit("[error] no time zone: pass --tz")
    return zone


def _default_site_dump(site_tag: str | None) -> Path:
    """Return the newest committed cycle recorded on installation *site_tag*.

    Args:
        site_tag: The site tag of the actuals being scored.

    Returns:
        The path of the cycle whose planner input supplies the limits.

    Raises:
        SystemExit: If no committed cycle carries that tag.
    """
    cycle = newest_cycle_of_site(_CORPUS_DIR, site_tag)
    if cycle is None:
        raise SystemExit(
            f"[error] no committed cycle is from the installation of the actuals "
            f"({describe_site(site_tag)}): pass --site <dump recorded there>, "
            f"and set {SITE_ENV} before collecting so the actuals carry a tag"
        )
    return cycle


def main(argv: list[str] | None = None) -> int:
    """Score every day of the given actuals and print the table.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` when at least one day was scored, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "actuals",
        nargs="*",
        default=[str(_ACTUALS_DIR)],
        help="actuals files or directories (default: the committed days)",
    )
    parser.add_argument(
        "--site",
        help=(
            "a diagnostics dump recorded on the installation being scored; its "
            "planner input supplies the battery and grid limits (default: the "
            "newest committed cycle with the site tag of the actuals)"
        ),
    )
    parser.add_argument(
        "--site-tag",
        default=os.environ.get(SITE_ENV),
        help=(
            "site tag assumed for a --site dump that carries none, as a dump "
            f"straight from Home Assistant does (default: ${SITE_ENV})"
        ),
    )
    parser.add_argument(
        "--tz", help="time zone that defines a day (default: read from the files)"
    )
    args = parser.parse_args(argv)

    files = _actuals_files(args.actuals)
    try:
        actuals = merge_actuals([load_actuals(path) for path in files])
        if args.site:
            site_path = Path(args.site).expanduser()
            dump_tag = site_of(load_dump(site_path)) or validate_site(args.site_tag)
            require_same_site(dump_tag, actuals.site_tag, f"--site {site_path.name}")
        else:
            site_path = _default_site_dump(actuals.site_tag)
    except ValueError as err:
        raise SystemExit(f"[error] {err}") from err
    planner_input, _report = load_planner_input(site_path)
    site = SiteLimits.from_planner_input(planner_input)
    zone = ZoneInfo(_time_zone(args.tz, files, planner_input.time_zone))

    print(
        f"actuals: {describe_site(actuals.site_tag)}; "
        f"site limits from {site_path.name}: {site.rated_kwh:g} kWh, "
        f"{site.min_soc_pct:g}-{site.max_soc_pct:g} % SoC, "
        f"{site.max_charge_kw:g}/{site.max_discharge_kw:g} kW, "
        f"efficiency {site.charge_efficiency:.2f}/{site.discharge_efficiency:.2f}"
    )
    scores = score_days(actuals, observed_days(actuals, zone), zone, site)
    print(describe(scores))
    return 0 if any(score.is_scored for score in scores) else 1


if __name__ == "__main__":
    sys.exit(main())

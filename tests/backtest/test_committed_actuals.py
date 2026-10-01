"""Committed actuals must be complete and line up with the committed corpus.

``scripts/backtest_harvest.py`` commits one actuals file per day that the
horizon of a committed cycle **of the same installation** covers (issue
#1225).  These tests keep that test base honest: a truncated day would make
any later scoring pass quietly score fewer slots, and a cycle paired with
another installation's actuals would turn every difference into a fake
forecast error.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from custom_components.hsem.planner.engine_core import run_planner
from tests.backtest.actuals import ENERGY_SERIES, align_to_slots, load_actuals
from tests.backtest.conftest import CORPUS_DIR
from tests.backtest.harvest import committed_actuals, expected_slots
from tests.backtest.replay import load_dump, load_planner_input
from tests.backtest.site import same_site, site_of

ACTUALS_DIR = Path(__file__).parent / "actuals"


def _committed() -> dict[date, Path]:
    return committed_actuals(sorted(ACTUALS_DIR.glob("actuals-*.json")))


def test_every_committed_day_is_complete() -> None:
    for day, path in _committed().items():
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["day"] == day.isoformat(), path.name
        need = expected_slots(
            day, ZoneInfo(document["time_zone"]), document["slot_minutes"]
        )
        actuals = load_actuals(path)
        assert not actuals.unknown_series, path.name
        for series in ENERGY_SERIES:
            got = len(actuals.energy_kwh.get(series, {}))
            assert got == need, f"{path.name}: {series} has {got}/{need} slots"


def test_every_committed_file_says_which_installation_it_is_from() -> None:
    """Issue #1225: nothing committed is paired by date alone."""
    for cycle in sorted(CORPUS_DIR.glob("*.json")):
        assert site_of(load_dump(cycle)) is not None, cycle.name
    for path in _committed().values():
        assert load_actuals(path).site_tag is not None, path.name


def test_committed_cycles_are_scorable_on_committed_days() -> None:
    """Every slot of a cycle on a committed day of its installation has actuals."""
    days = _committed()
    pairs = 0
    for cycle in sorted(CORPUS_DIR.glob("*.json")):
        cycle_site = site_of(load_dump(cycle))
        inp, _ = load_planner_input(cycle)
        slots = run_planner(inp).slots
        for day, path in days.items():
            actuals = load_actuals(path)
            if not same_site(cycle_site, actuals.site_tag):
                continue
            on_day = [s for s in slots if s.start.date() == day]
            if not on_day:
                continue
            pairs += 1
            rows, _ = align_to_slots(actuals, on_day)
            unscorable = [r.start.isoformat() for r in rows if not r.is_scorable]
            assert not unscorable, f"{cycle.name} on {day}: {unscorable[:3]}"
    assert pairs, "no committed cycle is paired with a committed actuals day"


def test_the_bug_report_cycle_is_not_paired_with_the_home_actuals() -> None:
    """Issue #1225: the 10 kWh dump and the 15 kWh installation's meters."""
    cycle_site = site_of(load_dump(CORPUS_DIR / "cycle-2026-09-14-1721.json"))
    actuals = load_actuals(ACTUALS_DIR / "actuals-2026-09-15.json")
    assert cycle_site == "site-b"
    assert actuals.site_tag == "site-a"
    assert not same_site(cycle_site, actuals.site_tag)

"""Committed actuals must be complete and line up with the committed corpus.

``scripts/backtest_harvest.py`` commits one actuals file per day that a
committed cycle's horizon covers.  These tests keep that test base honest: a
truncated day would make any later scoring pass quietly score fewer slots.
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
from tests.backtest.replay import load_planner_input

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


def test_committed_cycles_are_scorable_on_committed_days() -> None:
    """Every slot of a cycle that falls on a committed day carries actuals."""
    days = _committed()
    for cycle in sorted(CORPUS_DIR.glob("*.json")):
        inp, _ = load_planner_input(cycle)
        slots = run_planner(inp).slots
        for day, path in days.items():
            on_day = [s for s in slots if s.start.date() == day]
            if not on_day:
                continue
            rows, _ = align_to_slots(load_actuals(path), on_day)
            unscorable = [r.start.isoformat() for r in rows if not r.is_scorable]
            assert not unscorable, f"{cycle.name} on {day}: {unscorable[:3]}"

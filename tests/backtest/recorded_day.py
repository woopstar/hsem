"""Fully recorded days for the regret attribution (issue #1229).

The attribution of ``tests/backtest/attribution.py`` replays a day slot by
slot, so it needs a planner cycle for (almost) every slot of that day.  The
committed corpus holds a handful of cycles on different days, chosen because
each covers a situation the planner should be tested on, and every one of
them is replayed on every test run.  A day of 96 consecutive cycles is
neither a new situation nor something to replay on every run, so recorded
days live apart from the corpus::

    tests/backtest/days/<date>/cycles.jsonl          one slim cycle per slot
    tests/backtest/days/<date>/actuals-<date>.json   the day's realized values
    tests/backtest/days/<date>/actuals-<next>.json   and the following day's

``cycles.jsonl`` holds, per slot, the first cycle whose ``now_iso`` falls in
it, which is the cycle :func:`~tests.backtest.attribution.cycles_by_slot`
picks.  The following day's actuals are there because every plan of the day
reaches into it: the hindsight replay puts realized values in the place of
the forecasts wherever it has them.

A recorded day is one home's data in a public repository.  It is written
with the checks a corpus cycle gets (slim, round-tripping, no entity id, a
site tag), and it is never committed by a script.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tests.backtest.actuals import Actuals, load_actuals
from tests.backtest.attribution import MIN_CYCLE_COVERAGE, cycles_by_slot
from tests.backtest.harvest import (
    RefreshResult,
    expected_slots,
    leaks_entity_ids,
    refresh_payload,
    slim_payload,
    write_actuals_days,
)
from tests.backtest.replay import iter_dumps
from tests.backtest.scoring import merge_actuals
from tests.backtest.site import SITE_KEY, describe_site, site_of, validate_site

__all__ = [
    "CYCLES_FILE",
    "DAYS_DIR",
    "RecordedDayResult",
    "load_recorded_day",
    "recorded_days",
    "refresh_recorded_days",
    "write_recorded_day",
]

#: Where committed recorded days live.
DAYS_DIR = Path(__file__).parent / "days"

#: The cycles of one recorded day, one JSON document per line.
CYCLES_FILE = "cycles.jsonl"

#: Days after the recorded one whose actuals are kept with it.
FOLLOWING_DAYS = 1


@dataclass
class RecordedDayResult:
    """What writing one recorded day did.

    Attributes:
        day: The local calendar day.
        directory: Where the day was (or would be) written.
        cycles: Cycles written, one per covered slot.
        slots: Slots the day has (92 or 100 on DST days at 15 minutes).
        files: ``(path, bytes)`` of every file written.
        filled: ``PlannerInput`` fields the cycles predate, with the default
            each was given.  Check that every default describes the site as
            it was recorded (see ``tests/backtest/corpus/README.md``).
        refused: Why nothing was written; empty when the day was written.
    """

    day: date
    directory: Path
    cycles: int = 0
    slots: int = 0
    files: list[tuple[Path, int]] = field(default_factory=list)
    filled: dict[str, Any] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)

    @property
    def written(self) -> bool:
        """Return whether the day passed every check."""
        return not self.refused

    def describe(self) -> str:
        """Render the result for review."""
        if self.refused:
            lines = [f"{self.day}: not written"]
            lines.extend(f"  {reason}" for reason in self.refused)
            return "\n".join(lines)
        lines = [
            f"{self.day}: {self.cycles} cycle(s) for {self.slots} slot(s) "
            f"in {self.directory}"
        ]
        lines.extend(f"  {path.name}  {size:,} bytes" for path, size in self.files)
        if self.filled:
            lines.append(
                "  filled with the PlannerInput default — check each describes "
                "the site as it was recorded:"
            )
            lines.extend(
                f"    {name} = {value!r}" for name, value in sorted(self.filled.items())
            )
        return "\n".join(lines)


def _tagged_actuals(actuals: Mapping[str, Any], site: str) -> dict[str, Any] | str:
    """Return *actuals* carrying *site*, or why they cannot.

    Untagged actuals take the tag of the collection they are written for;
    actuals tagged for another installation are refused.
    """
    tag = site_of(actuals)
    if tag is None:
        return {**actuals, SITE_KEY: site}
    if tag != site:
        return (
            f"the actuals are from {describe_site(tag)}, the day is written "
            f"for {describe_site(site)}"
        )
    return dict(actuals)


def write_recorded_day(
    payloads: Iterable[Mapping[str, Any]],
    actuals: Mapping[str, Any],
    day: date,
    zone: ZoneInfo,
    site: str | None,
    days_dir: Path = DAYS_DIR,
    *,
    dry_run: bool = False,
) -> RecordedDayResult:
    """Write one fully recorded day, or say why it cannot be written.

    Nothing is written unless every check passes: the day has a cycle in at
    least half of its slots, every cycle round-trips losslessly and contains
    no entity id, and the actuals are complete for the day and the following
    day.

    Args:
        payloads: Live cycles (full diagnostics payloads), in any order.
        actuals: An ``hsem-actuals-1`` payload, as ``build_actuals.py`` writes.
        day: The local calendar day to record.
        zone: The site's zone, which defines the day.
        site: Tag of the installation the cycles and actuals are from.
        days_dir: The directory recorded days are kept in.
        dry_run: Run every check, writing nothing.

    Returns:
        A :class:`RecordedDayResult`.

    Raises:
        ValueError: If *site* is missing or not a valid tag.
    """
    site = validate_site(site)
    if site is None:
        raise ValueError(
            "a recorded day needs a site tag: set HSEM_BACKTEST_SITE or pass one"
        )
    slot_minutes = int(actuals["slot_minutes"])
    result = RecordedDayResult(
        day=day,
        directory=days_dir / day.isoformat(),
        slots=expected_slots(day, zone, slot_minutes),
    )
    if result.directory.exists():
        result.refused.append(f"{result.directory} already exists")
        return result

    tagged = _tagged_actuals(actuals, site)
    if isinstance(tagged, str):
        result.refused.append(tagged)
        return result

    picked = cycles_by_slot(payloads, day, zone, slot_minutes)
    lines: list[str] = []
    for key in sorted(picked):
        slim, _filled = slim_payload(dict(picked[key]), zone.key, site)
        # A cycle recorded before a PlannerInput field existed gets the
        # field's default, as a corpus refresh would give it.
        refreshed = refresh_payload(slim)
        if isinstance(refreshed, tuple):
            result.filled.update(refreshed[0])
        text = json.dumps(slim, separators=(",", ":"), sort_keys=True)
        if site_of(slim) != site:
            result.refused.append(
                f"the cycle of {key.isoformat()} is from {describe_site(site_of(slim))}"
            )
        elif isinstance(refreshed, str):
            result.refused.append(
                f"the cycle of {key.isoformat()} does not round-trip: {refreshed}"
            )
        elif leaks_entity_ids(text):
            result.refused.append(
                f"the cycle of {key.isoformat()} contains an entity id"
            )
        lines.append(text)
    result.cycles = len(lines)
    if result.cycles < MIN_CYCLE_COVERAGE * result.slots:
        result.refused.append(
            f"only {result.cycles} of {result.slots} slot(s) have a planner cycle "
            f"recorded in them"
        )

    wanted = [day + timedelta(days=offset) for offset in range(FOLLOWING_DAYS + 1)]
    complete, _incomplete = write_actuals_days(
        tagged, result.directory, wanted, zone, dry_run=True
    )
    for missing in sorted(set(wanted) - set(complete)):
        result.refused.append(f"the actuals of {missing} are not complete")
    if result.refused or dry_run:
        return result

    result.directory.mkdir(parents=True)
    cycles_path = result.directory / CYCLES_FILE
    cycles_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_actuals_days(tagged, result.directory, wanted, zone)
    result.files = [
        (path, path.stat().st_size) for path in sorted(result.directory.iterdir())
    ]
    return result


def recorded_days(days_dir: Path = DAYS_DIR) -> dict[date, Path]:
    """Map every recorded day to its directory."""
    if not days_dir.is_dir():
        return {}
    return {
        date.fromisoformat(path.name): path
        for path in sorted(days_dir.iterdir())
        if path.is_dir() and (path / CYCLES_FILE).is_file()
    }


def load_recorded_day(directory: Path) -> tuple[list[dict[str, Any]], Actuals]:
    """Load one recorded day's cycles and actuals.

    Args:
        directory: A ``tests/backtest/days/<date>`` directory.

    Returns:
        The cycles in file order and the merged actuals of every
        ``actuals-*.json`` beside them.

    Raises:
        ValueError: If the actuals files are from different installations.
    """
    payloads = list(iter_dumps(directory / CYCLES_FILE))
    actuals = merge_actuals(
        [load_actuals(path) for path in sorted(directory.glob("actuals-*.json"))]
    )
    return payloads, actuals


def refresh_recorded_days(
    days_dir: Path = DAYS_DIR, *, dry_run: bool = False
) -> RefreshResult:
    """Bring every recorded day back in step with the current ``PlannerInput``.

    The counterpart of :func:`~tests.backtest.harvest.refresh_corpus` for
    ``cycles.jsonl``: a cycle recorded before a field existed gets the field's
    default, a field that no longer exists is dropped.  The cycles are not
    replayed here; the attribution test replays them.

    Args:
        days_dir: The directory recorded days are kept in.
        dry_run: Report what would change, writing nothing.

    Returns:
        A :class:`~tests.backtest.harvest.RefreshResult` whose names are
        ``<date>/cycles.jsonl:<line>``.
    """
    result = RefreshResult()
    for day, directory in recorded_days(days_dir).items():
        path = directory / CYCLES_FILE
        payloads = list(iter_dumps(path))
        changed = False
        for number, payload in enumerate(payloads, start=1):
            name = f"{day}/{CYCLES_FILE}:{number}"
            outcome = refresh_payload(payload)
            if outcome is None:
                result.unchanged += 1
            elif isinstance(outcome, str):
                result.unfixable.append((name, outcome))
            else:
                result.updated.append((name, *outcome))
                changed = True
        if changed and not dry_run:
            path.write_text(
                "".join(
                    json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
                    for payload in payloads
                ),
                encoding="utf-8",
            )
    return result

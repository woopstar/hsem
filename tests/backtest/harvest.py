"""Grow the committed backtest corpus from a live one (issue #1037).

A live corpus holds one planner input per 5-minute cycle — about 290 a day, most
of them repeating a situation already covered.  Committing them all would add
tens of megabytes a day to the repository and make the suite slower without
making it broader.  Harvesting replays every new cycle against the planner spec
(that is the backtest) and commits only the cycles that cover a situation the
committed corpus does not have yet.

What makes a cycle worth keeping is captured by :class:`Situation`: which plan
won, which operating modes it uses, the starting SoC band, whether an EV is
charging, whether prices go negative, and whether the horizon crosses a DST
change.  Cycles that violate an invariant are never committed — that would turn
CI red before the bug is fixed — but copied to a quarantine directory for an
issue report.

A committed cycle keeps only what the harness reads: ``planner_input`` plus the
version, timestamp and ``apply_result`` — about 44 KB instead of 135.

Realized actuals are committed per calendar day, for the days a committed
cycle's horizon covers and only when every energy series is complete, so a
future scoring pass can be reproduced from the repository alone.

Every committed cycle and actuals day carries the tag of the installation it
was recorded on (``tests/backtest/site.py``, issue #1225).  Actuals are only
committed for days covered by a cycle with the same tag, so a plan is never
paired with another house's meters.
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.planner.engine_core import run_planner
from custom_components.hsem.utils.diagnostics import _planner_input_to_dict
from custom_components.hsem.utils.recommendations import SENTINEL_RECS
from tests.backtest.actuals import ACTUALS_SCHEMA, ENERGY_SERIES
from tests.backtest.invariants import check_invariants, format_violations
from tests.backtest.replay import (
    generous_solver_limit,
    iter_dumps,
    load_dump,
    load_planner_input,
    planner_input_from_dict,
)
from tests.backtest.site import (
    SITE_KEY,
    describe_site,
    same_site,
    site_of,
    validate_site,
)

__all__ = [
    "DEFAULT_MAX_CORPUS",
    "DEFAULT_MAX_NEW",
    "HarvestResult",
    "RefreshResult",
    "Situation",
    "committed_actuals",
    "cycle_file_name",
    "expected_slots",
    "harvest",
    "leaks_entity_ids",
    "newest_cycle_of_site",
    "refresh_corpus",
    "refresh_payload",
    "situation_of",
    "slim_payload",
    "write_actuals_days",
]

#: New cycles committed per run.  Growth should stay reviewable.
DEFAULT_MAX_NEW = 10

#: Total committed cycles.  Every one is replayed several times per test run,
#: so the cap bounds CI time; raise it deliberately.
DEFAULT_MAX_CORPUS = 50

#: The sections of a dump the harness reads.  ``planner_output`` is the bulk of
#: a dump and is never read — replays recompute it.  ``site`` is the
#: installation tag a harvest adds (issue #1225).
_KEEP_KEYS = (
    "hsem_version",
    "dump_timestamp",
    "planner_input",
    "apply_result",
    SITE_KEY,
)

_SENTINELS: frozenset[str] = frozenset(m.value for m in SENTINEL_RECS)

#: A Home Assistant entity id: a known domain, a dot, an object id.
_ENTITY_ID = re.compile(
    r"\b(?:sensor|binary_sensor|input_number|input_boolean|input_select|switch|"
    r"number|select|button|climate|device_tracker|person|light|zone)\.[a-z0-9_]+\b"
)


def leaks_entity_ids(text: str) -> bool:
    """Return ``True`` when *text* contains anything that looks like an entity id.

    ``**REDACTED**`` markers are fine — they are redaction working.  A raw id is
    not: a corpus file is one home's data in a public repository.
    """
    return _ENTITY_ID.search(text) is not None


@dataclass(frozen=True, order=True)
class Situation:
    """What a planning cycle exercises, coarsely enough to deduplicate on.

    Two cycles with the same situation test the planner the same way, so only
    the first is committed.  Deliberately coarse: finer keys would admit
    near-duplicates and grow the corpus without broadening it.

    Attributes:
        interval_minutes: Slot width.
        horizon_hours: Planning horizon.
        winner: The winning candidate.
        recommendations: Every operating mode the plan uses, sorted.
        soc_band: Starting SoC quartile, 0 (below 25 %) to 3.
        ev_charging: Whether the plan schedules any EV load.
        negative_import: Whether any planned slot has a negative import price.
        negative_export: Whether any planned slot has a negative export price.
        crosses_dst: Whether the horizon spans a UTC-offset change.
    """

    interval_minutes: int
    horizon_hours: int
    winner: str
    recommendations: tuple[str, ...]
    soc_band: int
    ev_charging: bool
    negative_import: bool
    negative_export: bool
    crosses_dst: bool

    def describe(self) -> str:
        """Render a one-line summary for the harvest report."""
        flags = [
            name
            for name, on in (
                ("ev", self.ev_charging),
                ("neg-import", self.negative_import),
                ("neg-export", self.negative_export),
                ("dst", self.crosses_dst),
            )
            if on
        ]
        return (
            f"{self.winner}, soc band {self.soc_band}, "
            f"{'+'.join(flags) or 'no flags'}: {', '.join(self.recommendations)}"
        )


def situation_of(inp: PlannerInput, out: PlannerOutput) -> Situation:
    """Classify one replayed cycle.

    Args:
        inp: The cycle's input.
        out: The plan the current code produces for it.

    Returns:
        The cycle's :class:`Situation`.
    """
    planned = [s for s in out.slots if s.recommendation not in _SENTINELS]
    return Situation(
        interval_minutes=inp.interval_minutes,
        horizon_hours=inp.interval_length_hours,
        winner=out.winner_name,
        recommendations=tuple(
            sorted({s.recommendation for s in planned if s.recommendation})
        ),
        soc_band=min(max(int(inp.battery_soc_pct // 25), 0), 3),
        ev_charging=any(s.ev_total_planned_load_kwh > 1e-6 for s in out.slots),
        negative_import=any(s.price.import_price < -1e-9 for s in planned),
        negative_export=any(s.price.export_price < -1e-9 for s in planned),
        crosses_dst=len({s.start.utcoffset() for s in out.slots}) > 1,
    )


def slim_payload(
    payload: dict[str, Any], time_zone: str | None, site: str | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Reduce a dump to what the harness reads, filling a known site zone.

    ``time_zone`` (#1169) is the one field a harvest may fill: dumps from before
    it lack the key, and the site's zone is known for certain.  Any other gap is
    left for the fidelity check to reject rather than invented.

    Args:
        payload: A full diagnostics payload.
        time_zone: The site's IANA zone, used only when the dump lacks the key.
        site: The tag of the installation the dump was recorded on (issue
            #1225).  A live dump carries none, so the harvest's tag is written;
            a tag the payload already carries is kept.

    Returns:
        The slim payload and the names of any fields filled in.
    """
    slim = {key: copy.deepcopy(payload[key]) for key in _KEEP_KEYS if key in payload}
    if site is not None and SITE_KEY not in slim:
        slim[SITE_KEY] = site
    filled: list[str] = []
    planner_input = slim.get("planner_input")
    if time_zone and isinstance(planner_input, dict):
        if "time_zone" not in planner_input:
            planner_input["time_zone"] = time_zone
            filled.append("time_zone")
    return slim, filled


def cycle_file_name(inp: PlannerInput) -> str:
    """Return the corpus file name for a cycle, from its planning moment."""
    moment = datetime.fromisoformat(inp.now_iso)
    return f"cycle-{moment:%Y-%m-%d-%H%M}.json"


def _horizon_days(out: PlannerOutput) -> set[date]:
    """Return the local calendar days a plan's slots cover."""
    return {slot.start.date() for slot in out.slots}


@dataclass
class HarvestResult:
    """What one harvest run did.

    Attributes:
        checked: Live cycles replayed and checked against the spec.
        violations: ``(cycle, report)`` for every cycle that broke an invariant.
        added: ``(file name, situation)`` for every cycle committed.
        skipped: Why eligible-looking cycles were not committed, with counts.
        capped: New situations left out because a cap was reached.
        latest: Timestamp of the newest cycle seen, for the next run to resume.
        horizon_days_by_site: Per installation tag, the calendar days covered
            by its committed cycles, old and new — the days worth committing
            that installation's actuals for (issue #1225).
    """

    checked: int = 0
    violations: list[tuple[str, str]] = field(default_factory=list)
    added: list[tuple[str, Situation]] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    capped: int = 0
    latest: str | None = None
    horizon_days_by_site: dict[str | None, set[date]] = field(default_factory=dict)

    def horizon_days(self, site: str | None) -> set[date]:
        """Return the days covered by committed cycles of installation *site*."""
        return set(self.horizon_days_by_site.get(site, ()))

    def cover(self, site: str | None, days: set[date]) -> None:
        """Record that a committed cycle of installation *site* covers *days*."""
        self.horizon_days_by_site.setdefault(site, set()).update(days)

    def describe(self) -> str:
        """Render the run summary."""
        lines = [f"replayed {self.checked} new cycle(s) against the planner spec"]
        if self.violations:
            lines.append(f"  {len(self.violations)} violated an invariant:")
            lines.extend(f"    {name}\n{report}" for name, report in self.violations)
        else:
            lines.append("  none violated an invariant")
        lines.append(f"added {len(self.added)} cycle(s) covering a new situation")
        lines.extend(f"  {name}  {sit.describe()}" for name, sit in self.added)
        for reason, count in self.skipped.most_common():
            lines.append(f"  not added: {reason} ({count})")
        if self.capped:
            lines.append(
                f"  {self.capped} new situation(s) left out: a cap was reached "
                f"(--max-new / --max-corpus)"
            )
        return "\n".join(lines)


def _after(timestamp: str | None, since: datetime | None) -> bool:
    """Return whether a dump timestamp is newer than the resume point."""
    if since is None:
        return True
    if not timestamp:
        return False
    return datetime.fromisoformat(timestamp) > since


def harvest(
    live_files: Iterable[Path],
    corpus_dir: Path,
    *,
    time_zone: str | None,
    site: str | None = None,
    since: datetime | None = None,
    max_new: int = DEFAULT_MAX_NEW,
    max_corpus: int = DEFAULT_MAX_CORPUS,
    quarantine_dir: Path | None = None,
    dry_run: bool = False,
) -> HarvestResult:
    """Replay new live cycles and commit those that cover a new situation.

    Args:
        live_files: Live corpus files (``.json`` or ``.jsonl``).
        corpus_dir: The committed corpus directory.
        time_zone: The site's IANA zone, filled into dumps that predate #1169.
        site: The tag of the installation the live corpus was recorded on
            (issue #1225).  Without one every cycle is still replayed and
            checked, but none is committed.
        since: Only cycles dumped after this moment are processed.
        max_new: Cycles to commit in this run at most.
        max_corpus: Committed cycles in total at most.
        quarantine_dir: Where cycles that violate an invariant are copied.
        dry_run: Report what would happen, writing nothing.

    Returns:
        A :class:`HarvestResult`.
    """
    with generous_solver_limit():
        return _harvest(
            live_files,
            corpus_dir,
            time_zone=time_zone,
            site=validate_site(site),
            since=since,
            max_new=max_new,
            max_corpus=max_corpus,
            quarantine_dir=quarantine_dir,
            dry_run=dry_run,
        )


def _harvest(
    live_files: Iterable[Path],
    corpus_dir: Path,
    *,
    time_zone: str | None,
    site: str | None,
    since: datetime | None,
    max_new: int,
    max_corpus: int,
    quarantine_dir: Path | None,
    dry_run: bool,
) -> HarvestResult:
    """Do the work of :func:`harvest` with the solver limit already lifted."""
    result = HarvestResult()
    committed = sorted(corpus_dir.glob("*.json"))
    known: set[Situation] = set()
    for path in committed:
        inp, _report = load_planner_input(path)
        out = run_planner(inp)
        known.add(situation_of(inp, out))
        result.cover(site_of(load_dump(path)), _horizon_days(out))

    for live in live_files:
        for payload in iter_dumps(live):
            stamp = payload.get("dump_timestamp")
            if not _after(stamp, since):
                continue
            if stamp and (result.latest is None or stamp > result.latest):
                result.latest = stamp

            slim, _filled = slim_payload(payload, time_zone, site)
            inp, report = planner_input_from_dict(slim)
            out = run_planner(inp)
            result.checked += 1
            text = json.dumps(slim, indent=2, sort_keys=True) + "\n"

            violations = check_invariants(inp, out)
            if violations:
                name = cycle_file_name(inp)
                result.violations.append((name, format_violations(violations)))
                if quarantine_dir is not None and not dry_run:
                    quarantine_dir.mkdir(parents=True, exist_ok=True)
                    (quarantine_dir / name).write_text(text, encoding="utf-8")
                continue

            if not report.is_faithful:
                fields = ", ".join(report.missing + report.dropped) or "datetimes"
                result.skipped[f"does not round-trip ({fields})"] += 1
                continue
            if leaks_entity_ids(text):
                result.skipped["contains an entity id"] += 1
                continue
            cycle_site = site_of(slim)
            if cycle_site is None:
                result.skipped["no site tag (set HSEM_BACKTEST_SITE)"] += 1
                continue
            situation = situation_of(inp, out)
            if situation in known:
                result.skipped["situation already covered"] += 1
                continue
            if (
                len(result.added) >= max_new
                or len(committed) + len(result.added) >= max_corpus
            ):
                result.capped += 1
                continue
            name = cycle_file_name(inp)
            target = corpus_dir / name
            if target.exists():
                result.skipped["file already exists"] += 1
                continue
            if not dry_run:
                target.write_text(text, encoding="utf-8")
            known.add(situation)
            result.added.append((name, situation))
            result.cover(cycle_site, _horizon_days(out))
    return result


def expected_slots(day: date, zone: ZoneInfo, slot_minutes: int) -> int:
    """Return how many slots a local calendar day has — 92 or 100 on DST days."""
    start = datetime.combine(day, time(0), zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0), zone).astimezone(UTC)
    return int((end - start) / timedelta(minutes=slot_minutes))


def write_actuals_days(
    actuals: dict[str, Any],
    out_dir: Path,
    days: Iterable[date],
    zone: ZoneInfo,
    *,
    dry_run: bool = False,
) -> tuple[list[date], list[date]]:
    """Commit one actuals file per wanted day that the export covers completely.

    Args:
        actuals: An ``hsem-actuals-1`` payload, as ``build_actuals.py`` writes.
        out_dir: The committed actuals directory.
        days: Days worth committing — those covered by the horizon of a
            committed cycle **of the same installation** as *actuals*
            (:meth:`HarvestResult.horizon_days` with the payload's tag).
        zone: The site's zone, which defines a calendar day.
        dry_run: Report what would happen, writing nothing.

    Returns:
        ``(written, incomplete)``.  A day already committed is in neither;
        a day with any energy series short of a full day is ``incomplete``.

    Raises:
        ValueError: If *actuals* carries no site tag (issue #1225): a
            committed day must say which installation it is from.
    """
    site = site_of(actuals)
    if site is None:
        raise ValueError(
            f"the actuals carry {describe_site(site)}: set HSEM_BACKTEST_SITE and "
            f"rebuild them before committing a day"
        )
    slot_minutes = int(actuals["slot_minutes"])
    by_day: dict[date, dict[str, dict[str, list[Any]]]] = {}
    for bucket in ("slot_energy_kwh", "slot_values"):
        for series, pairs in actuals.get(bucket, {}).items():
            for stamp, value in pairs:
                day = datetime.fromisoformat(stamp).astimezone(zone).date()
                buckets = by_day.setdefault(
                    day, {"slot_energy_kwh": {}, "slot_values": {}}
                )
                buckets[bucket].setdefault(series, []).append([stamp, value])

    written: list[date] = []
    incomplete: list[date] = []
    for day in sorted(set(days)):
        target = out_dir / f"actuals-{day:%Y-%m-%d}.json"
        if target.exists():
            continue
        day_buckets = by_day.get(day)
        need = expected_slots(day, zone, slot_minutes)
        if day_buckets is None or any(
            len(day_buckets["slot_energy_kwh"].get(series, [])) < need
            for series in ENERGY_SERIES
        ):
            if day_buckets is not None:
                incomplete.append(day)
            continue
        document = {
            "schema": ACTUALS_SCHEMA,
            "slot_minutes": slot_minutes,
            "day": day.isoformat(),
            "time_zone": zone.key,
            SITE_KEY: site,
            **day_buckets,
        }
        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n",
                encoding="utf-8",
            )
        written.append(day)
    return written, incomplete


def committed_actuals(paths: Sequence[Path]) -> dict[date, Path]:
    """Map each committed actuals file to its day."""
    return {date.fromisoformat(p.stem.removeprefix("actuals-")): p for p in paths}


def newest_cycle_of_site(corpus_dir: Path, site: str | None) -> Path | None:
    """Return the newest committed cycle recorded on installation *site*.

    Scoring reads the battery and grid limits from a recorded planner input,
    and they must be those of the installation the actuals are from (issue
    #1225).

    Args:
        corpus_dir: The committed corpus directory.
        site: The site tag of the actuals being scored.

    Returns:
        The cycle's path, or ``None`` when no committed cycle carries the tag.
    """
    cycles = [
        path
        for path in sorted(corpus_dir.glob("cycle-*.json"))
        if same_site(site_of(load_dump(path)), site)
    ]
    return cycles[-1] if cycles else None


# ---------------------------------------------------------------------------
# Keeping the committed corpus in step with PlannerInput
# ---------------------------------------------------------------------------


@dataclass
class RefreshResult:
    """What a corpus refresh changed.

    Attributes:
        unchanged: Cycles that already round-trip.
        updated: ``(file, filled, removed)`` for every cycle rewritten —
            ``filled`` maps each added field to the default it received.
        unfixable: ``(file, reason)`` for cycles a refresh cannot repair.
        violations: ``(file, report)`` for cycles that round-trip after the
            refresh but break an invariant on the current code.
    """

    unchanged: int = 0
    updated: list[tuple[str, dict[str, Any], list[str]]] = field(default_factory=list)
    unfixable: list[tuple[str, str]] = field(default_factory=list)
    violations: list[tuple[str, str]] = field(default_factory=list)

    def describe(self) -> str:
        """Render the refresh summary, naming every default that was filled in."""
        lines = [
            f"{self.unchanged} cycle(s) already round-trip, {len(self.updated)} updated"
        ]
        filled: dict[str, Any] = {}
        removed: set[str] = set()
        for _name, file_filled, file_removed in self.updated:
            filled.update(file_filled)
            removed.update(file_removed)
        if filled:
            lines.append(
                "  filled with the PlannerInput default — check each describes "
                "a site recorded before the field existed:"
            )
            lines.extend(
                f"    {name} = {value!r}" for name, value in sorted(filled.items())
            )
        if removed:
            lines.append(f"  removed, no longer on PlannerInput: {sorted(removed)}")
        for name, reason in self.unfixable:
            lines.append(f"  cannot repair {name}: {reason}")
        for name, report in self.violations:
            lines.append(f"  {name} now violates an invariant:\n{report}")
        return "\n".join(lines)


def refresh_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str]] | str | None:
    """Bring one payload back in step with the current ``PlannerInput``, in place.

    Fields the dump predates are filled with their ``PlannerInput`` default
    and fields that no longer exist are dropped, as :func:`refresh_corpus`
    describes.

    Args:
        payload: A dump payload carrying ``planner_input``; modified in place.

    Returns:
        ``None`` when the payload already round-trips; the reason when it
        cannot be repaired; otherwise ``(filled, removed)``: the defaults
        filled in by field name, and the names removed.
    """
    _inp, report = planner_input_from_dict(payload)
    if report.is_faithful:
        return None
    if report.malformed_datetimes:
        return f"unparseable {sorted(report.malformed_datetimes)}"

    defaults = _planner_input_to_dict(PlannerInput())
    planner_input = payload["planner_input"]
    filled = {name: defaults[name] for name in report.missing}
    planner_input.update(copy.deepcopy(filled))
    for name in report.dropped:
        del planner_input[name]
    for list_name, keys in report.dropped_nested.items():
        for row in planner_input[list_name]:
            for key in keys:
                row.pop(key, None)

    _inp, after = planner_input_from_dict(payload)
    if not after.is_faithful:
        return after.describe()
    return filled, [*report.dropped, *(f"{k}[]" for k in report.dropped_nested)]


def refresh_corpus(corpus_dir: Path, *, dry_run: bool = False) -> RefreshResult:
    """Bring every committed cycle back in step with the current ``PlannerInput``.

    Adding a field to ``PlannerInput`` makes every committed cycle stop
    round-tripping, and the corpus tests fail — deliberately.  For a dump
    recorded before the field existed, the dataclass default is normally the
    truthful value (the feature was off), so a refresh fills it in; a field
    removed from ``PlannerInput`` is dropped.  Every filled default is listed
    so it can be checked: when the default does *not* describe the recorded
    site — as with ``time_zone`` (#1169) — set the real value by hand instead.

    A refresh never hides a regression: each rewritten cycle is replayed, and
    one that now breaks an invariant is reported.

    Args:
        corpus_dir: The committed corpus directory.
        dry_run: Report what would change, writing nothing.

    Returns:
        A :class:`RefreshResult`.
    """
    result = RefreshResult()
    with generous_solver_limit():
        for path in sorted(corpus_dir.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            payload = document.get("data", document)
            outcome = refresh_payload(payload)
            if outcome is None:
                result.unchanged += 1
                continue
            if isinstance(outcome, str):
                result.unfixable.append((path.name, outcome))
                continue
            filled, removed = outcome
            result.updated.append((path.name, filled, removed))
            if not dry_run:
                path.write_text(
                    json.dumps(document, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            inp, _report = planner_input_from_dict(payload)
            violations = check_invariants(inp, run_planner(inp))
            if violations:
                result.violations.append((path.name, format_violations(violations)))
    return result

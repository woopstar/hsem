"""Per-slot actuals and hindsight scores for the performance sensors (issue #1182).

The tracker keeps three things and persists them together:

- **slots** — what actually happened in each slot of the last two days, built
  up from consecutive coordinator samples;
- **window** — the latest hindsight score of the trailing 24 hours;
- **days** — one score per completed local day, which the 7- and 30-day
  figures are sums over.

Scoring itself is not done here.  :meth:`PerformanceTracker.request` hands out
a detached copy of what an evaluation needs, the coordinator runs it in the
executor (``utils/performance_scoring.py``), and
:meth:`PerformanceTracker.apply` takes the result back.

A period figure is always a ratio of sums, never a mean of daily percentages:
a day with almost nothing to gain would otherwise weigh as much as a day with
a lot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

from custom_components.hsem.models.performance_record import (
    REASON_NO_SITE_LIMITS,
    REASON_ORACLE_UNSOLVED,
    REASON_SCIPY_UNAVAILABLE,
    PerformanceSample,
    PerformanceScore,
    PerformanceSlot,
)
from custom_components.hsem.planner.hindsight_scoring import SiteLimits, capture_share
from custom_components.hsem.utils.datetime_utils import (
    physical_slot_grid,
    slot_key,
    utc_key,
)
from custom_components.hsem.utils.performance_scoring import PerformanceRequest
from custom_components.hsem.utils.persistence import (
    finite_float,
    read_json_history_file,
    write_json_history_file,
)

#: Version of the history file.  A file of another version is not read.
SCHEMA_VERSION = 1

#: Length of the rolling window the headline figures cover.
WINDOW_HOURS = 24

#: How long slots are kept.  Two days and a little: a completed local day is
#: at most 25 hours long and may only be scored some hours after it ended.
RETAIN_SLOT_HOURS = 50

#: Daily scores kept.  The longest period reported is 30 days.
MAX_DAYS = 90

#: Reported periods, in completed local days before today.
PERIODS: dict[str, int] = {"last_7_days": 7, "last_30_days": 30}

#: Why the sensors have no value before the first evaluation.
REASON_NOT_EVALUATED = "not_evaluated"

#: The additive quantities of a score.  Every period figure is derived from
#: their sums, so a later KPI only has to add the quantities it needs here.
SUMMED_FIELDS: tuple[str, ...] = (
    "realized_cost",
    "oracle_cost",
    "baseline_cost",
    "potential",
)

#: Largest grid power a meter delta may imply before it is taken for a meter
#: glitch and the interval is left unobserved.
_MAX_GRID_KW = 50.0

#: Allowance on top of that for a meter that counts in coarse steps: two
#: cycles a few seconds apart can straddle a whole step.
_METER_STEP_KWH = 2.0


@dataclass(frozen=True)
class _Interval:
    """What happened between two consecutive samples."""

    seconds: float
    grid_import_kwh: float
    grid_export_kwh: float
    soc_from_pct: float
    soc_to_pct: float
    import_price: float
    export_price: float
    end_import_price: float
    end_export_price: float
    in_control: bool
    pv_kwh: float | None
    ev_kwh: float | None
    ev_connected: bool | None


def _meter_delta(
    before: float | None, after: float | None, hours: float
) -> float | None:
    """Return a cumulative meter's increase, or ``None`` when it is unusable."""
    if before is None or after is None:
        return None
    delta = after - before
    if delta < -1e-9 or delta > _MAX_GRID_KW * hours + _METER_STEP_KWH:
        return None
    return max(delta, 0.0)


def _mean_power_kwh(
    before_w: float | None, after_w: float | None, hours: float
) -> float | None:
    """Return the energy of a power reading held between two samples."""
    if before_w is None or after_w is None:
        return None
    return (before_w + after_w) / 2.0 * hours / 1000.0


def _interval(
    previous: PerformanceSample, sample: PerformanceSample
) -> _Interval | None:
    """Return what happened between two samples, or ``None`` if not observed.

    Prices are the ones in force at the earlier sample, as the financial
    tracker prices an interval; the later sample's are kept for the part of
    the interval that falls into its slot.  Nothing missing is read as zero:
    without both grid meters, the SoC and both prices the interval is left
    out.
    """
    seconds = (sample.at - previous.at).total_seconds()
    hours = seconds / 3600.0
    grid_import = _meter_delta(
        previous.grid_import_energy_kwh, sample.grid_import_energy_kwh, hours
    )
    grid_export = _meter_delta(
        previous.grid_export_energy_kwh, sample.grid_export_energy_kwh, hours
    )
    if (
        grid_import is None
        or grid_export is None
        or previous.soc_pct is None
        or sample.soc_pct is None
        or previous.import_price is None
        or previous.export_price is None
    ):
        return None
    pv = _meter_delta(previous.pv_energy_kwh, sample.pv_energy_kwh, hours)
    if pv is None:
        pv = _mean_power_kwh(previous.pv_power_w, sample.pv_power_w, hours)
    connected = [
        state
        for state in (previous.ev_connected, sample.ev_connected)
        if state is not None
    ]
    return _Interval(
        seconds=seconds,
        grid_import_kwh=grid_import,
        grid_export_kwh=grid_export,
        soc_from_pct=previous.soc_pct,
        soc_to_pct=sample.soc_pct,
        import_price=previous.import_price,
        export_price=previous.export_price,
        end_import_price=(
            previous.import_price
            if sample.import_price is None
            else sample.import_price
        ),
        end_export_price=(
            previous.export_price
            if sample.export_price is None
            else sample.export_price
        ),
        in_control=previous.in_control and sample.in_control,
        pv_kwh=pv,
        ev_kwh=_mean_power_kwh(previous.ev_power_w, sample.ev_power_w, hours),
        ev_connected=any(connected) if connected else None,
    )


def sum_scores(
    scores: Iterable[PerformanceScore], names: Sequence[str] = SUMMED_FIELDS
) -> dict[str, float]:
    """Return the sums of the named quantities over the scored runs.

    Args:
        scores: Scores of any runs; unscored ones are left out.
        names: The additive fields to sum.

    Returns:
        One sum per name, 0.0 when no run is scored.
    """
    scored = [score for score in scores if score.is_scored]
    return {
        name: sum(getattr(score, name) or 0.0 for score in scored) for name in names
    }


def _rounded(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _percent(share: float | None) -> float | None:
    return None if share is None else round(share * 100.0, 1)


def _site_from_dict(data: Any) -> SiteLimits | None:
    """Rebuild persisted site limits, or ``None`` when they are not usable."""
    if not isinstance(data, dict):
        return None
    values: dict[str, Any] = {}
    for spec in fields(SiteLimits):
        raw = data.get(spec.name)
        if spec.name == "battery_export_enabled":
            if not isinstance(raw, bool):
                return None
            values[spec.name] = raw
            continue
        number = finite_float(raw)
        if number is None and spec.name not in (
            "max_grid_import_kw",
            "max_grid_export_kw",
        ):
            return None
        values[spec.name] = number
    return SiteLimits(**values)


@dataclass
class PerformanceTracker:
    """Records per-slot actuals and holds their hindsight scores.

    Attributes:
        slots: Recorded slots by UTC start.
        days: Scores of completed local days by ISO date.
        window: Latest score of the trailing 24 hours, or ``None``.
        site: The installation's hard limits as of the latest plan.
        history_file: Path of the JSON history file.
    """

    slots: dict[datetime, PerformanceSlot] = field(default_factory=dict)
    days: dict[str, PerformanceScore] = field(default_factory=dict)
    window: PerformanceScore | None = None
    site: SiteLimits | None = None
    history_file: str = ""
    _last_sample: PerformanceSample | None = field(default=None, repr=False)
    _saved_at: datetime | None = field(default=None, repr=False)
    _unsaved_scores: bool = field(default=False, repr=False)

    # ------------------------------------------------------------------
    # Accumulation
    # ------------------------------------------------------------------

    def accumulate(
        self,
        sample: PerformanceSample,
        *,
        slot_minutes: int,
        max_gap_seconds: float,
    ) -> None:
        """Add what happened since the previous sample to the slots it spans.

        Meter deltas are split over the slots by time.  An interval longer
        than *max_gap_seconds*, or one missing a required reading, is left
        unobserved: the slots it would have touched simply gain no coverage.

        An interval that crosses a slot boundary is priced on each side with
        the price sampled on that side: prices change at slot boundaries, and
        the earlier sample's price would otherwise leak into the next slot
        for as long as a cycle takes.

        Args:
            sample: This cycle's readings.
            slot_minutes: Slot width.
            max_gap_seconds: Longest interval between two samples that is
                still attributed.
        """
        previous = self._last_sample
        self._last_sample = sample
        if previous is None:
            return
        elapsed = (sample.at - previous.at).total_seconds()
        if elapsed <= 0.0 or elapsed > max_gap_seconds:
            return
        interval = _interval(previous, sample)
        if interval is None:
            return

        step = timedelta(minutes=slot_minutes)
        cursor = utc_key(previous.at)
        end = utc_key(sample.at)
        start = slot_key(previous.at, slot_minutes)
        last = slot_key(sample.at, slot_minutes)
        soc_per_second = (interval.soc_to_pct - interval.soc_from_pct) / elapsed
        while cursor < end:
            piece_end = min(start + step, end)
            seconds = (piece_end - cursor).total_seconds()
            share = seconds / elapsed
            slot = self.slots.get(start)
            if slot is None or slot.minutes != slot_minutes:
                slot = self.slots[start] = PerformanceSlot(
                    start=start, minutes=slot_minutes
                )
            since = (cursor - utc_key(previous.at)).total_seconds()
            # Only the slot the later sample is in is known to have its price.
            later = start == last and start != slot_key(previous.at, slot_minutes)
            slot.add(
                seconds=seconds,
                grid_import_kwh=interval.grid_import_kwh * share,
                grid_export_kwh=interval.grid_export_kwh * share,
                soc_start_pct=interval.soc_from_pct + soc_per_second * since,
                soc_end_pct=interval.soc_from_pct + soc_per_second * (since + seconds),
                import_price=(
                    interval.end_import_price if later else interval.import_price
                ),
                export_price=(
                    interval.end_export_price if later else interval.export_price
                ),
                in_control=interval.in_control,
                pv_kwh=None if interval.pv_kwh is None else interval.pv_kwh * share,
                ev_kwh=None if interval.ev_kwh is None else interval.ev_kwh * share,
                ev_connected=interval.ev_connected,
            )
            cursor = piece_end
            start += step
        self._prune(end)

    def _prune(self, now: datetime) -> None:
        """Drop slots older than :data:`RETAIN_SLOT_HOURS`."""
        oldest = now - timedelta(hours=RETAIN_SLOT_HOURS)
        for start in [start for start in self.slots if start < oldest]:
            del self.slots[start]

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def needs_evaluation(self, now: datetime, slot_minutes: int) -> bool:
        """Return whether the window score is older than the last full slot.

        Args:
            now: Current time.
            slot_minutes: Slot width.
        """
        return self.window is None or self.window.end != slot_key(now, slot_minutes)

    def request(
        self,
        now: datetime,
        *,
        slot_minutes: int,
        zone: tzinfo | None,
        time_limit_s: float | None = None,
    ) -> PerformanceRequest:
        """Return what an evaluation at *now* needs, detached from the tracker.

        The window is the 24 physical hours before the slot *now* is in,
        stepped in UTC, so it has the same number of slots on a DST day as on
        any other.  A local day is asked for once it has ended and has no
        score yet.

        Args:
            now: Current time.
            slot_minutes: Slot width.
            zone: The site's time zone, which defines the local days.
            time_limit_s: Wall-clock limit for each oracle solve.

        Returns:
            A request the executor can work on while the tracker moves on.
        """
        step = timedelta(minutes=slot_minutes)
        window_end = slot_key(now, slot_minutes)
        count = int(timedelta(hours=WINDOW_HOURS) / step)
        days: list[tuple[str, tuple[datetime, ...]]] = []
        for day in sorted({start.astimezone(zone).date() for start in self.slots}):
            if day.isoformat() in self.days:
                continue
            noon = datetime.combine(day, time(12), tzinfo=zone)
            keys = tuple(
                utc_key(start)
                for start, _ in physical_slot_grid(noon, slot_minutes, 24)
            )
            if keys and keys[-1] + step <= window_end:
                days.append((day.isoformat(), keys))
        return PerformanceRequest(
            slots={start: replace(slot) for start, slot in self.slots.items()},
            slot_minutes=slot_minutes,
            window=tuple(window_end - (count - index) * step for index in range(count)),
            days=tuple(days),
            site=self.site,
            time_limit_s=time_limit_s,
        )

    def apply(
        self, window: PerformanceScore, days: Iterable[PerformanceScore] = ()
    ) -> None:
        """Take the result of an evaluation.

        A day that could not be scored only because the site's limits were
        not known yet is not kept, so it is tried again while its slots last.

        Args:
            window: Score of the trailing window.
            days: Scores of the local days that were asked for.
        """
        self.window = window
        for score in days:
            if score.day is None or score.reason == REASON_NO_SITE_LIMITS:
                continue
            self.days[score.day] = score
        for day in sorted(self.days)[:-MAX_DAYS]:
            del self.days[day]
        self._unsaved_scores = True

    # ------------------------------------------------------------------
    # Figures for the sensors
    # ------------------------------------------------------------------

    def period(self, today: date, days: int) -> dict[str, Any]:
        """Return the figures of the *days* completed local days before *today*.

        Ratios of sums over the scored days: a day that could not be scored
        is counted in ``scored_days`` falling short of ``days``, never read as
        zero cost.

        Args:
            today: The current local day, which is not part of the period.
            days: Length of the period.

        Returns:
            Actual benefit, missed opportunity, potential, savings capture in
            percent (``None`` when the potential is too small) and how many of
            the period's days are scored.
        """
        scores = [
            score
            for offset in range(1, days + 1)
            if (score := self.days.get((today - timedelta(days=offset)).isoformat()))
            is not None
            and score.is_scored
        ]
        sums = sum_scores(scores)
        regret = sums["realized_cost"] - sums["oracle_cost"]
        savings = sums["potential"] - regret
        return {
            "actual_benefit": round(savings, 3),
            "missed_opportunity": round(regret, 3),
            "potential": round(sums["potential"], 3),
            "savings_capture": (
                _percent(capture_share(savings, sums["potential"])) if scores else None
            ),
            "scored_days": len(scores),
            "days": days,
        }

    @staticmethod
    def _figures(score: PerformanceScore) -> dict[str, Any]:
        """Return a score's headline figures and cost breakdown."""
        return {
            "reason": score.capture_reason,
            "actual_benefit": _rounded(score.savings),
            "missed_opportunity": _rounded(score.regret),
            "savings_capture": _percent(score.capture),
            "potential": _rounded(score.potential),
            "realized_cost": _rounded(score.realized_cost),
            "baseline_cost": _rounded(score.baseline_cost),
            "oracle_cost": _rounded(score.oracle_cost),
            "coverage": _rounded(score.coverage),
            "control_coverage": _rounded(score.control_coverage),
        }

    def as_sensor_attributes(self, now: datetime) -> dict[str, Any]:
        """Return everything the performance sensors publish as attributes.

        Args:
            now: Current time in the site's time zone: it defines today, and
                the window bounds are published in its zone.
        """
        today = now.date()
        window = self.window
        yesterday = self.days.get((today - timedelta(days=1)).isoformat())
        attributes: dict[str, Any] = {
            "reason": REASON_NOT_EVALUATED,
            "solver_status": "not_run",
        }
        if window is not None:
            attributes = {
                **self._figures(window),
                "solver_status": _solver_status(window),
                "window_start": window.start.astimezone(now.tzinfo).isoformat(),
                "window_end": window.end.astimezone(now.tzinfo).isoformat(),
                "slot_count": window.slot_count,
                "start_stored_kwh": _rounded(window.start_kwh),
                "realized_end_stored_kwh": _rounded(window.realized_end_kwh),
                "baseline_end_stored_kwh": _rounded(window.baseline_end_kwh),
                "residual_kwh": _rounded(window.residual_kwh),
                "notes": list(window.notes),
            }
        attributes["yesterday"] = (
            None
            if yesterday is None
            else {"date": yesterday.day, **self._figures(yesterday)}
        )
        for name, days in PERIODS.items():
            attributes[name] = self.period(today, days)
        return attributes

    # ------------------------------------------------------------------
    # JSON persistence
    # ------------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return the tracker's state as a plain dict for JSON serialisation."""
        return {
            "schema": SCHEMA_VERSION,
            "slots": [self.slots[start].as_dict() for start in sorted(self.slots)],
            "days": [self.days[day].as_dict() for day in sorted(self.days)],
            "window": None if self.window is None else self.window.as_dict(),
            "site": None if self.site is None else asdict(self.site),
            "last_sample": (
                None if self._last_sample is None else self._last_sample.as_dict()
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PerformanceTracker:
        """Create a tracker from a deserialised history file.

        A file of another schema version, and any entry that does not parse,
        is left out: the tracker then starts recording afresh.
        """
        tracker = cls()
        if data.get("schema") != SCHEMA_VERSION:
            return tracker
        for raw in data.get("slots") or []:
            slot = PerformanceSlot.from_dict(raw)
            if slot is not None:
                tracker.slots[slot.start] = slot
        for raw in data.get("days") or []:
            score = PerformanceScore.from_dict(raw)
            if score is not None and score.day is not None:
                tracker.days[score.day] = score
        tracker.window = PerformanceScore.from_dict(data.get("window"))
        tracker.site = _site_from_dict(data.get("site"))
        tracker._last_sample = PerformanceSample.from_dict(data.get("last_sample"))
        return tracker

    def restore_from(self, loaded: PerformanceTracker) -> None:
        """Replace this tracker's state with a reconstructed tracker's."""
        history_file = self.history_file
        self.__dict__.update(loaded.__dict__)
        self.history_file = history_file

    def save_due(self, interval_seconds: float) -> bool:
        """Return whether enough has happened since the last save to save again.

        Args:
            interval_seconds: Longest stretch of samples that may be unsaved.
        """
        if self._unsaved_scores:
            return True
        if self._last_sample is None:
            return False
        if self._saved_at is None:
            return True
        unsaved = (self._last_sample.at - self._saved_at).total_seconds()
        return unsaved >= interval_seconds

    async def save_history(self) -> bool:
        """Persist the tracker to disk atomically.

        Returns:
            ``True`` if the state was written.
        """
        if not self.history_file:
            return False
        saved_at = None if self._last_sample is None else self._last_sample.at
        saved = await asyncio.to_thread(
            write_json_history_file, Path(self.history_file), self.as_dict()
        )
        if saved:
            self._saved_at = saved_at
            self._unsaved_scores = False
        return saved

    async def load_history(self) -> None:
        """Restore the tracker from its history file, if there is one."""
        if not self.history_file:
            return
        data = await asyncio.to_thread(read_json_history_file, Path(self.history_file))
        if data is not None:
            self.restore_from(PerformanceTracker.from_dict(data))


def _solver_status(score: PerformanceScore) -> str:
    """Return what became of the oracle solve behind a score."""
    if score.is_scored:
        return "optimal"
    if score.reason == REASON_SCIPY_UNAVAILABLE:
        return "unavailable"
    if score.reason == REASON_ORACLE_UNSOLVED:
        return "unsolved"
    return "not_run"

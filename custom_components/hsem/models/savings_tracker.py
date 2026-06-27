"""Savings tracker that computes how much money HSEM saves vs doing nothing.

Tracks actual savings (when the master switch is on) and missed savings
(when the master switch is off), plus a running baseline cost.  Persists
daily snapshots to a JSON file following the same pattern as
:class:`~custom_components.hsem.models.daily_plan_vs_actual_tracker.DailyPlanVsActualTracker`.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from custom_components.hsem.models.savings_day import SavingsDay


@dataclass
class SavingsTracker:
    """Tracks actual savings, missed savings, and baseline costs.

    Attributes:
        max_history_days: Rolling window size in days (default 90).
        history_file: Full path to ``hsem_savings_history.json``.
        actual_savings: Cumulative actual savings since integration start.
        missed_savings: Cumulative missed savings (switch was off).
        baseline_cost: Cumulative baseline cost since integration start.
        daily: Per-day snapshots keyed by ISO date string.
        _today: ISO-format date string for the current day.
        _switch_was_off: Whether the master switch was off this cycle.
        _last_export_rev: Snapshot of daily_tracker grid_export_rev for delta.
        _last_import_cost: Snapshot of daily_tracker grid_import_cost for delta.
    """

    max_history_days: int = 90
    history_file: str = ""

    # Running totals (cumulative, never reset).
    actual_savings: float = 0.0
    missed_savings: float = 0.0
    baseline_cost: float = 0.0

    # Daily snapshots keyed by ISO date string.
    daily: dict[str, SavingsDay] = field(default_factory=dict)

    # Per-cycle state.
    _today: str = ""
    _switch_was_off: bool = False

    # Delta tracking snapshots from the daily plan-vs-actual tracker.
    _last_export_rev: float | None = field(default=None, repr=False)
    _last_import_cost: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Set today's date if not already set."""
        if not self._today:
            self._today = date.today().isoformat()
        # Ensure today has a daily entry.
        if self._today not in self.daily:
            self.daily[self._today] = SavingsDay(date=self._today)

    # ------------------------------------------------------------------
    # Accumulation
    # ------------------------------------------------------------------

    def accumulate(
        self,
        export_revenue_delta: float,
        charge_savings_delta: float,
        baseline_cost_delta: float,
        switch_on: bool,
    ) -> None:
        """Accumulate one cycle's worth of savings data.

        Actual savings accumulate when *switch_on* is ``True``; missed
        savings accumulate when *switch_on* is ``False``.  Baseline cost
        accumulates regardless.

        Args:
            export_revenue_delta: Export revenue earned this cycle (currency).
            charge_savings_delta: Money saved by charging cheap now vs later.
            baseline_cost_delta: What passive mode would have cost this cycle.
            switch_on: ``True`` when the master switch is on (auto mode).
        """
        savings = export_revenue_delta + charge_savings_delta

        if switch_on:
            self.actual_savings += savings
        else:
            self.missed_savings += savings

        self.baseline_cost += baseline_cost_delta
        self._switch_was_off = not switch_on

        # Accumulate into today's daily entry.
        today_entry = self.daily.setdefault(self._today, SavingsDay(date=self._today))
        if switch_on:
            today_entry.actual_savings += savings
        else:
            today_entry.missed_savings += savings
        today_entry.baseline_cost += baseline_cost_delta

    # ------------------------------------------------------------------
    # Period rollup properties
    # ------------------------------------------------------------------

    @property
    def today_actual(self) -> float:
        """Return actual savings for today."""
        entry = self.daily.get(self._today)
        return entry.actual_savings if entry else 0.0

    @property
    def today_missed(self) -> float:
        """Return missed savings for today."""
        entry = self.daily.get(self._today)
        return entry.missed_savings if entry else 0.0

    @property
    def today_baseline(self) -> float:
        """Return baseline cost for today."""
        entry = self.daily.get(self._today)
        return entry.baseline_cost if entry else 0.0

    @property
    def last_7_days_actual(self) -> float:
        """Return actual savings sum over the last 7 calendar days."""
        return self._sum_period(7, "actual_savings")

    @property
    def last_7_days_missed(self) -> float:
        """Return missed savings sum over the last 7 calendar days."""
        return self._sum_period(7, "missed_savings")

    @property
    def last_30_days_actual(self) -> float:
        """Return actual savings sum over the last 30 calendar days."""
        return self._sum_period(30, "actual_savings")

    @property
    def last_30_days_missed(self) -> float:
        """Return missed savings sum over the last 30 calendar days."""
        return self._sum_period(30, "missed_savings")

    def _sum_period(self, days: int, field: str) -> float:
        """Sum *field* over the last *days* calendar days."""
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        total = 0.0
        for d_str, entry in sorted(self.daily.items()):
            if d_str >= cutoff:
                total += getattr(entry, field, 0.0)
        return total

    # ------------------------------------------------------------------
    # Day rollover
    # ------------------------------------------------------------------

    def check_day_rollover(self, today_str: str) -> SavingsDay | None:
        """Check if the calendar day has changed and handle rollover.

        When the day changes, the previous day's entry is finalised and
        a fresh entry is created for the new day.

        Args:
            today_str: ISO-format date string for the current day.

        Returns:
            The finalised :class:`SavingsDay` for the previous day, or
            ``None`` if no rollover occurred.
        """
        if today_str == self._today:
            return None

        previous_day = self.daily.get(self._today)
        self._today = today_str
        self.daily[today_str] = SavingsDay(date=today_str)
        return previous_day

    # ------------------------------------------------------------------
    # Daily access
    # ------------------------------------------------------------------

    def get_today_entry(self) -> SavingsDay:
        """Return today's :class:`SavingsDay` entry."""
        return self.daily.setdefault(self._today, SavingsDay(date=self._today))

    def get_sorted_daily(self, limit: int = 90) -> list[SavingsDay]:
        """Return the most recent *limit* daily entries sorted by date."""
        sorted_dates = sorted(self.daily.keys(), reverse=True)[:limit]
        return [self.daily[d] for d in sorted_dates]

    # ------------------------------------------------------------------
    # JSON persistence
    # ------------------------------------------------------------------

    async def load_history(self) -> None:
        """Load history from the JSON file, if it exists."""
        path = Path(self.history_file)
        if not path.exists():
            return

        data = await asyncio.to_thread(self._read_history_file, path)
        if data is None:
            return

        # Restore running totals.
        self.actual_savings = float(data.get("actual_savings", 0.0))
        self.missed_savings = float(data.get("missed_savings", 0.0))
        self.baseline_cost = float(data.get("baseline_cost", 0.0))

        # Restore daily entries.
        days = data.get("days", [])
        if isinstance(days, list):
            for d in days:
                entry = SavingsDay.from_dict(d)
                self.daily[entry.date] = entry

        self._prune_history()

    @staticmethod
    def _read_history_file(path: Path) -> dict[str, Any] | None:
        """Read and parse the history JSON file (sync, offloaded to thread)."""
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)  # type: ignore[no-any-return]
        except json.JSONDecodeError, OSError:
            return None

    async def save_history(self) -> bool:
        """Persist the full savings state to disk atomically."""
        if not self.history_file:
            return False

        self._prune_history()

        data: dict[str, Any] = {
            "updated": datetime.now().isoformat(),
            "actual_savings": self.actual_savings,
            "missed_savings": self.missed_savings,
            "baseline_cost": self.baseline_cost,
            "days": [
                e.as_dict() for e in sorted(self.daily.values(), key=lambda x: x.date)
            ],
        }

        return await asyncio.to_thread(self._write_history_file_sync, data)

    def _prune_history(self) -> None:
        """Keep only the most recent ``max_history_days`` entries."""
        if len(self.daily) <= self.max_history_days:
            return
        sorted_dates = sorted(self.daily.keys())
        to_remove = sorted_dates[: -self.max_history_days]
        for d in to_remove:
            del self.daily[d]

    def _write_history_file_sync(self, data: dict[str, Any]) -> bool:
        """Write the history file atomically (offloaded to thread)."""
        path = Path(self.history_file)
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            fd, tmp_path = tempfile.mkstemp(
                suffix=".json",
                prefix=".hsem_savings_history_",
                dir=str(path.parent),
                text=True,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, str(path))
            except Exception:
                with suppress(OSError):
                    os.unlink(tmp_path)
                raise
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Attribute export for the sensor
    # ------------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return the savings tracker state as a dict for sensor attributes."""
        sorted_daily = self.get_sorted_daily(90)
        return {
            "today_actual": round(self.today_actual, 4),
            "today_missed": round(self.today_missed, 4),
            "today_baseline": round(self.today_baseline, 4),
            "last_7_days_actual": round(self.last_7_days_actual, 4),
            "last_7_days_missed": round(self.last_7_days_missed, 4),
            "last_30_days_actual": round(self.last_30_days_actual, 4),
            "last_30_days_missed": round(self.last_30_days_missed, 4),
            "total_actual": round(self.actual_savings, 4),
            "total_missed": round(self.missed_savings, 4),
            "total_baseline": round(self.baseline_cost, 4),
            "daily": [d.as_dict() for d in sorted_daily],
            "max_history_days": self.max_history_days,
            "history_total_days": len(self.daily),
        }

"""Daily plan-vs-actual tracking model.

Provides :class:`DailyPlanVsActualTracker` — a pure-Python accumulator that
tracks cumulative actual vs planned energy/cost metrics since midnight and
manages a rolling 90-day JSON history file.

All fields are plain Python types; no Home Assistant imports are used.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass
class DailyMetrics:
    """Cumulative energy and cost metrics for one tracking category.

    All energy values are in kWh.  All cost values are in local currency.
    """

    grid_import_kwh: float = 0.0
    grid_import_cost: float = 0.0
    grid_export_kwh: float = 0.0
    grid_export_rev: float = 0.0
    battery_cycled_kwh: float = 0.0
    pv_produced_kwh: float = 0.0

    def as_dict(self) -> dict[str, float]:
        """Return metrics as a plain dict for JSON serialisation."""
        return {
            "grid_import_kwh": round(self.grid_import_kwh, 3),
            "grid_import_cost": round(self.grid_import_cost, 3),
            "grid_export_kwh": round(self.grid_export_kwh, 3),
            "grid_export_rev": round(self.grid_export_rev, 3),
            "battery_cycled_kwh": round(self.battery_cycled_kwh, 3),
            "pv_produced_kwh": round(self.pv_produced_kwh, 3),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DailyMetrics:
        """Create from a deserialised JSON dict."""
        return cls(
            grid_import_kwh=float(data.get("grid_import_kwh", 0.0)),
            grid_import_cost=float(data.get("grid_import_cost", 0.0)),
            grid_export_kwh=float(data.get("grid_export_kwh", 0.0)),
            grid_export_rev=float(data.get("grid_export_rev", 0.0)),
            battery_cycled_kwh=float(data.get("battery_cycled_kwh", 0.0)),
            pv_produced_kwh=float(data.get("pv_produced_kwh", 0.0)),
        )


@dataclass
class DailyDiff:
    """Difference metrics (actual minus plan)."""

    grid_import_kwh: float = 0.0
    grid_import_cost: float = 0.0
    grid_export_kwh: float = 0.0
    grid_export_rev: float = 0.0
    battery_cycled_kwh: float = 0.0
    pv_produced_kwh: float = 0.0
    net_cost: float = 0.0

    def as_dict(self) -> dict[str, float]:
        """Return diff as a plain dict for JSON serialisation."""
        return {
            "grid_import_kwh": round(self.grid_import_kwh, 3),
            "grid_import_cost": round(self.grid_import_cost, 3),
            "grid_export_kwh": round(self.grid_export_kwh, 3),
            "grid_export_rev": round(self.grid_export_rev, 3),
            "battery_cycled_kwh": round(self.battery_cycled_kwh, 3),
            "pv_produced_kwh": round(self.pv_produced_kwh, 3),
            "net_cost": round(self.net_cost, 3),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DailyDiff:
        """Create from a deserialised JSON dict."""
        return cls(
            grid_import_kwh=float(data.get("grid_import_kwh", 0.0)),
            grid_import_cost=float(data.get("grid_import_cost", 0.0)),
            grid_export_kwh=float(data.get("grid_export_kwh", 0.0)),
            grid_export_rev=float(data.get("grid_export_rev", 0.0)),
            battery_cycled_kwh=float(data.get("battery_cycled_kwh", 0.0)),
            pv_produced_kwh=float(data.get("pv_produced_kwh", 0.0)),
            net_cost=float(data.get("net_cost", 0.0)),
        )


@dataclass
class DailyRecord:
    """A single day's plan-vs-actual record.

    Captures all metrics for one calendar day, including the diff between
    actual and planned values.
    """

    date: str  # ISO-format date string (e.g. "2026-06-01")
    actual: DailyMetrics = field(default_factory=DailyMetrics)
    plan: DailyMetrics = field(default_factory=DailyMetrics)
    diff: DailyDiff = field(default_factory=DailyDiff)

    @property
    def net_cost_actual(self) -> float:
        """Return net cost for the day (import cost − export revenue)."""
        return self.actual.grid_import_cost - self.actual.grid_export_rev

    @property
    def net_cost_plan(self) -> float:
        """Return planned net cost for the day."""
        return self.plan.grid_import_cost - self.plan.grid_export_rev

    def compute_diff(self) -> None:
        """Compute diff from actual and plan fields."""
        self.diff.grid_import_kwh = (
            self.actual.grid_import_kwh - self.plan.grid_import_kwh
        )
        self.diff.grid_import_cost = (
            self.actual.grid_import_cost - self.plan.grid_import_cost
        )
        self.diff.grid_export_kwh = (
            self.actual.grid_export_kwh - self.plan.grid_export_kwh
        )
        self.diff.grid_export_rev = (
            self.actual.grid_export_rev - self.plan.grid_export_rev
        )
        self.diff.battery_cycled_kwh = (
            self.actual.battery_cycled_kwh - self.plan.battery_cycled_kwh
        )
        self.diff.pv_produced_kwh = (
            self.actual.pv_produced_kwh - self.plan.pv_produced_kwh
        )
        self.diff.net_cost = self.net_cost_actual - self.net_cost_plan

    def as_dict(self) -> dict[str, Any]:
        """Return record as a plain dict for JSON serialisation."""
        self.compute_diff()
        return {
            "date": self.date,
            "actual": self.actual.as_dict(),
            "plan": self.plan.as_dict(),
            "diff": self.diff.as_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DailyRecord:
        """Create from a deserialised JSON dict."""
        record = cls(
            date=str(data.get("date", "")),
            actual=DailyMetrics.from_dict(data.get("actual", {})),
            plan=DailyMetrics.from_dict(data.get("plan", {})),
            diff=DailyDiff.from_dict(data.get("diff", {})),
        )
        return record


@dataclass
class DailyPlanVsActualTracker:
    """Accumulates daily plan-vs-actual metrics and manages 90-day JSON history.

    This is a pure-Python tracker stored on the coordinator.  It accumulates
    plan and actual values each cycle, triggers midnight persistence, and
    resets counters for the new day.

    Attributes:
        history_file: Full path to ``hsem_daily_history.json``.
        max_history_days: Rolling window size in days (default 90).
        today: Today's date (set on initialisation and checked each cycle).
        actual: Cumulative actual metrics since midnight.
        plan: Cumulative planned metrics since midnight.
        last_soc_pct: Previous battery SoC reading for cycle tracking (or None).
        history: List of persisted :class:`DailyRecord` objects (up to 90).
    """

    history_file: str = ""
    max_history_days: int = 90
    today: str = ""  # ISO-format date string
    actual: DailyMetrics = field(default_factory=DailyMetrics)
    plan: DailyMetrics = field(default_factory=DailyMetrics)
    last_soc_pct: float | None = None
    history: list[DailyRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Set today's date if not already set and load existing history."""
        if not self.today:
            self.today = date.today().isoformat()
        if self.history_file:
            self.load_history()

    # ------------------------------------------------------------------
    # Midday counter reset
    # ------------------------------------------------------------------

    def check_day_rollover(self, now: datetime) -> DayRolloverResult | None:
        """Check if the calendar day has changed and handle rollover.

        When the day changes, the current day's record is finalised and saved,
        counters are reset, and history is pruned to the maximum window.

        Args:
            now: Current datetime (timezone-aware).

        Returns:
            A :class:`DayRolloverResult` with the persisted record and saved
            flag, or ``None`` if no rollover occurred.
        """
        today_str = now.date().isoformat()
        if today_str == self.today:
            return None

        # Day has changed — finalise and save yesterday's record.
        today_record = self._build_today_record()
        saved = self._save_record_to_history(today_record)

        # Reset counters for the new day.
        self.today = today_str
        self.actual = DailyMetrics()
        self.plan = DailyMetrics()
        self.last_soc_pct = None

        return DayRolloverResult(
            record=today_record,
            saved=saved,
        )

    # ------------------------------------------------------------------
    # Accumulation helpers
    # ------------------------------------------------------------------

    def accumulate_actual(
        self,
        grid_import_energy_kwh: float | None = None,
        grid_export_energy_kwh: float | None = None,
        pv_energy_kwh: float | None = None,
        soc_pct: float | None = None,
        rated_capacity_kwh: float = 0.0,
        import_price: float = 0.0,
        export_price: float = 0.0,
    ) -> None:
        """Accumulate actual energy and cost values.

        Energy values are expected to be *cumulative* since midnight (from
        the energy meter).  The delta from the previous reading is added.

        Battery cycles are tracked from SoC changes using
        ``abs(soc[t] - soc[t-1]) * rated_capacity_kwh / 100``.

        Args:
            grid_import_energy_kwh: Cumulative grid import meter reading (kWh).
            grid_export_energy_kwh: Cumulative grid export meter reading (kWh).
            pv_energy_kwh: Cumulative PV production meter reading (kWh).
            soc_pct: Current battery SoC percentage (0-100).
            rated_capacity_kwh: Rated battery capacity in kWh for cycle tracking.
            import_price: Current import price (currency/kWh).
            export_price: Current export price (currency/kWh).
        """
        if soc_pct is not None and self.last_soc_pct is not None:
            # Track battery cycles from SoC delta (pct → kWh).
            pct_change = abs(soc_pct - self.last_soc_pct)
            if rated_capacity_kwh > 0:
                self.actual.battery_cycled_kwh += (
                    pct_change * rated_capacity_kwh / 100.0
                )

        if soc_pct is not None:
            self.last_soc_pct = soc_pct

    def accumulate_plan(
        self,
        grid_import_kwh: float = 0.0,
        grid_export_kwh: float = 0.0,
        cycle_kwh: float = 0.0,
        pv_kwh: float = 0.0,
        import_price: float = 0.0,
        export_price: float = 0.0,
    ) -> None:
        """Accumulate planned energy values from a single time slot.

        Args:
            grid_import_kwh: Planned grid import for the slot (kWh).
            grid_export_kwh: Planned grid export for the slot (kWh).
            cycle_kwh: Planned battery cycle energy for the slot (kWh).
            pv_kwh: Planned PV production for the slot (kWh).
            import_price: Spot import price (currency/kWh).
            export_price: Spot export price (currency/kWh).
        """
        self.plan.grid_import_kwh += grid_import_kwh
        self.plan.grid_import_cost += grid_import_kwh * import_price
        self.plan.grid_export_kwh += grid_export_kwh
        self.plan.grid_export_rev += grid_export_kwh * export_price
        self.plan.battery_cycled_kwh += cycle_kwh
        self.plan.pv_produced_kwh += pv_kwh

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------

    def _build_today_record(self) -> DailyRecord:
        """Build a :class:`DailyRecord` from current accumulators."""
        record = DailyRecord(
            date=self.today,
            actual=self.actual,
            plan=self.plan,
        )
        record.compute_diff()
        return record

    def get_today_record(self) -> DailyRecord:
        """Return today's record with computed diff."""
        record = self._build_today_record()
        return record

    def get_yesterday_record(self) -> DailyRecord | None:
        """Return yesterday's record from history, or ``None``."""
        yesterday_str = (date.today() - timedelta(days=1)).isoformat()
        for record in reversed(self.history):
            if record.date == yesterday_str:
                return record
        return None

    # ------------------------------------------------------------------
    # JSON persistence
    # ------------------------------------------------------------------

    def load_history(self) -> None:
        """Load history from the JSON file, if it exists.

        Handles corrupted files gracefully by starting with an empty history.
        """
        path = Path(self.history_file)
        if not path.exists():
            return

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Corrupted file — start fresh.
            self.history = []
            return

        days = data.get("days", [])
        if isinstance(days, list):
            self.history = [DailyRecord.from_dict(d) for d in days]
            self._prune_history()

    def _save_record_to_history(self, record: DailyRecord) -> bool:
        """Append a record to the in-memory history, prune, and persist to disk.

        Uses atomic write (write to temp file, then rename) to protect against
        corruption from crashes during write.

        Args:
            record: The :class:`DailyRecord` to save.

        Returns:
            ``True`` if the record was successfully persisted to disk.
        """
        self.history.append(record)
        self._prune_history()

        return self._write_history_file()

    def _prune_history(self) -> None:
        """Keep only the most recent ``max_history_days`` records."""
        if len(self.history) > self.max_history_days:
            self.history = self.history[-self.max_history_days :]

    def _write_history_file(self) -> bool:
        """Write the history list to disk atomically.

        Returns:
            ``True`` on success, ``False`` on I/O error.
        """
        if not self.history_file:
            return False

        data = {
            "updated": datetime.now().isoformat(),
            "days": [r.as_dict() for r in self.history],
        }

        path = Path(self.history_file)
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Write to a temp file in the same directory, then atomically rename.
            fd, tmp_path = tempfile.mkstemp(
                suffix=".json",
                prefix=".hsem_daily_history_",
                dir=str(path.parent),
                text=True,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, str(path))
            except Exception:
                # Clean up temp file on failure.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Attribute export for the sensor
    # ------------------------------------------------------------------

    def as_sensor_attributes(self) -> dict[str, Any]:
        """Return all data needed by the sensor as a flat dict.

        The sensor state is ``net_cost_actual``.
        """
        today_record = self.get_today_record()
        yesterday_record = self.get_yesterday_record()

        attrs: dict[str, Any] = {
            "today": today_record.as_dict(),
            "yesterday": yesterday_record.as_dict() if yesterday_record else None,
            "history": [r.as_dict() for r in self.history[-30:]],
            "history_file": self.history_file,
            "history_days": self.max_history_days,
            "history_total_days": len(self.history),
        }
        return attrs


@dataclass
class DayRolloverResult:
    """Result of a day rollover check.

    Attributes:
        record: The :class:`DailyRecord` that was persisted.
        saved: Whether the record was successfully written to disk.
    """

    record: DailyRecord
    saved: bool = False

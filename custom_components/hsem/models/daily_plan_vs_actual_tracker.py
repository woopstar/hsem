"""Daily plan-vs-actual tracker that accumulates metrics and manages 90-day JSON history.

This is a pure-Python tracker stored on the coordinator.  It accumulates plan and
actual values each cycle, triggers midnight persistence, and resets counters for the
new day.
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

from custom_components.hsem.models.daily_metrics import DailyMetrics
from custom_components.hsem.models.daily_record import DailyRecord
from custom_components.hsem.models.day_rollover_result import DayRolloverResult


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

    # Last-seen cumulative meter readings for delta calculation.
    _last_import_energy_kwh: float | None = field(default=None, repr=False)
    _last_export_energy_kwh: float | None = field(default=None, repr=False)
    _last_pv_energy_kwh: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Set today's date if not already set.

        History loading is deferred — call :meth:`load_history` explicitly
        to avoid blocking I/O during dataclass construction.
        """
        if not self.today:
            self.today = date.today().isoformat()

    # ------------------------------------------------------------------
    # Midday counter reset
    # ------------------------------------------------------------------

    async def check_day_rollover(self, now: datetime) -> DayRolloverResult | None:
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
        saved = await self._save_record_to_history(today_record)

        # Reset counters for the new day.
        self.today = today_str
        self.actual = DailyMetrics()
        self.plan = DailyMetrics()
        self.last_soc_pct = None
        self._last_import_energy_kwh = None
        self._last_export_energy_kwh = None
        self._last_pv_energy_kwh = None

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
        # Grid import delta from cumulative meter (kWh).
        if grid_import_energy_kwh is not None:
            if self._last_import_energy_kwh is not None:
                delta = grid_import_energy_kwh - self._last_import_energy_kwh
                if delta > 0:
                    self.actual.grid_import_kwh += delta
                    self.actual.grid_import_cost += delta * import_price
            self._last_import_energy_kwh = grid_import_energy_kwh

        # Grid export delta from cumulative meter (kWh).
        if grid_export_energy_kwh is not None:
            if self._last_export_energy_kwh is not None:
                delta = grid_export_energy_kwh - self._last_export_energy_kwh
                if delta > 0:
                    self.actual.grid_export_kwh += delta
                    self.actual.grid_export_rev += delta * export_price
            self._last_export_energy_kwh = grid_export_energy_kwh

        # PV production delta from cumulative meter (kWh).
        if pv_energy_kwh is not None:
            if self._last_pv_energy_kwh is not None:
                delta = pv_energy_kwh - self._last_pv_energy_kwh
                if delta > 0:
                    self.actual.pv_produced_kwh += delta
            self._last_pv_energy_kwh = pv_energy_kwh

        # Battery cycle tracking from SoC delta (pct → kWh).
        if soc_pct is not None and self.last_soc_pct is not None:
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

    async def load_history(self) -> None:
        """Load history from the JSON file, if it exists.

        Handles corrupted files gracefully by starting with an empty history.
        """
        path = Path(self.history_file)
        if not path.exists():
            return

        data = await asyncio.to_thread(self._read_history_file, path)
        if data is None:
            return

        days = data.get("days", [])
        if isinstance(days, list):
            self.history = [DailyRecord.from_dict(d) for d in days]
            self._prune_history()

    @staticmethod
    def _read_history_file(path: Path) -> dict[str, Any] | None:
        """Read and parse the history JSON file (sync, offloaded to thread)."""
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)  # type: ignore[no-any-return]
        except json.JSONDecodeError, OSError:
            return None

    async def _save_record_to_history(self, record: DailyRecord) -> bool:
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

        return await self._write_history_file()

    def _prune_history(self) -> None:
        """Keep only the most recent ``max_history_days`` records."""
        if len(self.history) > self.max_history_days:
            self.history = self.history[-self.max_history_days :]

    async def _write_history_file(self) -> bool:
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

        return await asyncio.to_thread(self._write_history_file_sync, data, path)

    @staticmethod
    def _write_history_file_sync(data: dict[str, Any], path: Path) -> bool:
        """Write the history list to disk atomically (sync, offloaded to thread)."""
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
                with suppress(OSError):
                    os.unlink(tmp_path)
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

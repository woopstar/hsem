"""Financial tracker that accumulates cumulative import cost and export income.

Running totals never reset.  Daily snapshots are logged at midnight for period
rollups (today, last 7 days, last 30 days, this month, this year).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any


@dataclass
class FinancialDayEntry:
    """A single day's import cost and export income totals.

    Attributes:
        date: ISO-format date string (e.g. ``"2026-06-26"``).
        import_cost: Grid import cost for the day.
        export_income: Grid export income for the day.
    """

    date: str
    import_cost: float = 0.0
    export_income: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Return entry as a plain dict for JSON serialisation."""
        return {
            "date": self.date,
            "import_cost": round(self.import_cost, 3),
            "export_income": round(self.export_income, 3),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FinancialDayEntry:
        """Create from a deserialised JSON dict."""
        return cls(
            date=str(data.get("date", "")),
            import_cost=float(data.get("import_cost", 0.0)),
            export_income=float(data.get("export_income", 0.0)),
        )


@dataclass
class FinancialTracker:
    """Tracks cumulative import cost and export income with daily snapshots.

    Running totals (``import_cost_total`` / ``export_income_total``) grow
    monotonically and are **never** reset.  Daily snapshots are recorded
    at midnight so per-period rollups are cheap look-ups without scanning
    individual cycle records.

    Attributes:
        import_cost_total: Cumulative grid import cost (never reset).
        export_income_total: Cumulative export income (never reset).
        import_cost_today: Today's import cost (total − start-of-day).
        export_income_today: Today's export income (total − start-of-day).
        daily_log: Dict mapping ISO date string → :class:`FinancialDayEntry`.
        today: ISO-format date string for the current tracking day.
        history_file: Path to the JSON persistence file on disk.
    """

    import_cost_total: float = 0.0
    export_income_total: float = 0.0

    # Start-of-day running totals (set at midnight rollover).
    _today_start_import_cost: float = field(default=0.0, repr=False)
    _today_start_export_income: float = field(default=0.0, repr=False)

    # Last-seen cumulative meter readings for delta computation.
    _last_import_energy_kwh: float | None = field(default=None, repr=False)
    _last_export_energy_kwh: float | None = field(default=None, repr=False)

    daily_log: dict[str, FinancialDayEntry] = field(default_factory=dict)
    today: str = ""
    history_file: str = ""

    def __post_init__(self) -> None:
        """Set today's date if not already set."""
        if not self.today:
            self.today = date.today().isoformat()

    # ------------------------------------------------------------------
    # Period rollup properties
    # ------------------------------------------------------------------

    @property
    def import_cost_today(self) -> float:
        """Import cost accumulated today (total − start-of-day baseline)."""
        return self.import_cost_total - self._today_start_import_cost

    @property
    def export_income_today(self) -> float:
        """Export income accumulated today (total − start-of-day baseline)."""
        return self.export_income_total - self._today_start_export_income

    @property
    def net_balance_today(self) -> float:
        """Net grid balance today (export income − import cost)."""
        return self.export_income_today - self.import_cost_today

    def _sum_period(self, days: int) -> dict[str, float]:
        """Sum daily entries for the last *days* calendar days.

        Args:
            days: Number of calendar days to sum (including today).

        Returns:
            Dict with keys ``import_cost``, ``export_income``, and
            ``net_balance``.
        """
        today_date = date.today()
        total_import = 0.0
        total_export = 0.0
        for offset in range(days):
            d = today_date - timedelta(days=offset)
            entry = self.daily_log.get(d.isoformat())
            if entry is not None:
                total_import += entry.import_cost
                total_export += entry.export_income
        return {
            "import_cost": round(total_import, 3),
            "export_income": round(total_export, 3),
            "net_balance": round(total_export - total_import, 3),
        }

    def _sum_month(self) -> dict[str, float]:
        """Sum daily entries for the current month."""
        today_date = date.today()
        month_key = today_date.strftime("%Y-%m")
        total_import = 0.0
        total_export = 0.0
        for entry in self.daily_log.values():
            if entry.date.startswith(month_key):
                total_import += entry.import_cost
                total_export += entry.export_income
        return {
            "import_cost": round(total_import, 3),
            "export_income": round(total_export, 3),
            "net_balance": round(total_export - total_import, 3),
        }

    def _sum_year(self) -> dict[str, float]:
        """Sum daily entries for the current year."""
        today_date = date.today()
        year_key = today_date.strftime("%Y")
        total_import = 0.0
        total_export = 0.0
        for entry in self.daily_log.values():
            if entry.date.startswith(year_key):
                total_import += entry.import_cost
                total_export += entry.export_income
        return {
            "import_cost": round(total_import, 3),
            "export_income": round(total_export, 3),
            "net_balance": round(total_export - total_import, 3),
        }

    # ------------------------------------------------------------------
    # Accumulation
    # ------------------------------------------------------------------

    def accumulate(
        self,
        grid_import_energy_kwh: float | None = None,
        grid_export_energy_kwh: float | None = None,
        import_price: float = 0.0,
        export_price: float = 0.0,
    ) -> None:
        """Accumulate import cost and export income from live meter readings.

        Computes the delta between consecutive cumulative meter readings,
        multiplies by the applicable price, and adds to the running totals.

        Args:
            grid_import_energy_kwh: Cumulative grid import meter reading (kWh).
            grid_export_energy_kwh: Cumulative grid export meter reading (kWh).
            import_price: Current import spot price (currency/kWh).
            export_price: Current export spot price (currency/kWh).
        """
        # Grid import cost delta.
        if grid_import_energy_kwh is not None:
            if self._last_import_energy_kwh is not None:
                delta = grid_import_energy_kwh - self._last_import_energy_kwh
                if delta > 1e-9:
                    self.import_cost_total += delta * import_price
            self._last_import_energy_kwh = grid_import_energy_kwh

        # Grid export income delta.
        if grid_export_energy_kwh is not None:
            if self._last_export_energy_kwh is not None:
                delta = grid_export_energy_kwh - self._last_export_energy_kwh
                if delta > 1e-9:
                    self.export_income_total += delta * export_price
            self._last_export_energy_kwh = grid_export_energy_kwh

    # ------------------------------------------------------------------
    # Day rollover
    # ------------------------------------------------------------------

    def check_day_rollover(self, now: datetime) -> None:
        """Snapshot yesterday's totals into the daily log and reset baselines.

        Called at the start of each coordinator cycle.  When the calendar
        day has changed, the previous day's cumulative totals are recorded
        as a :class:`FinancialDayEntry` and the start-of-day baselines are
        updated to the current running totals.

        Args:
            now: Current datetime (timezone-aware).
        """
        today_str = now.date().isoformat()
        if today_str == self.today:
            return

        # Snapshot yesterday's totals (delta since last midnight).
        yesterday_import = self.import_cost_total - self._today_start_import_cost
        yesterday_export = self.export_income_total - self._today_start_export_income

        entry = FinancialDayEntry(
            date=self.today,
            import_cost=yesterday_import,
            export_income=yesterday_export,
        )
        self.daily_log[self.today] = entry

        # Rotate to new day.
        self.today = today_str
        self._today_start_import_cost = self.import_cost_total
        self._today_start_export_income = self.export_income_total

    # ------------------------------------------------------------------
    # Attribute export for sensors
    # ------------------------------------------------------------------

    def _daily_list(self) -> list[dict[str, Any]]:
        """Return the daily log as a sorted list of ``{date, value}`` records.

        Each entry contains ``date``, ``import_cost``, ``export_income``,
        and ``net_balance`` (export_income − import_cost).
        """
        result: list[dict[str, Any]] = []
        for entry in sorted(self.daily_log.values(), key=lambda e: e.date):
            result.append(
                {
                    "date": entry.date,
                    "import_cost": round(entry.import_cost, 3),
                    "export_income": round(entry.export_income, 3),
                    "net_balance": round(entry.export_income - entry.import_cost, 3),
                }
            )
        return result

    def as_sensor_attributes(self) -> dict[str, Any]:
        """Return all data needed by the financial sensors as a flat dict.

        Includes period rollups and the daily record list.
        """
        return {
            "today": {
                "import_cost": round(self.import_cost_today, 3),
                "export_income": round(self.export_income_today, 3),
                "net_balance": round(self.net_balance_today, 3),
            },
            "last_7_days": self._sum_period(7),
            "last_30_days": self._sum_period(30),
            "this_month": self._sum_month(),
            "this_year": self._sum_year(),
            "daily": self._daily_list(),
        }

    # ------------------------------------------------------------------
    # JSON persistence
    # ------------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return tracker state as a plain dict for JSON serialisation."""
        return {
            "import_cost_total": round(self.import_cost_total, 3),
            "export_income_total": round(self.export_income_total, 3),
            "_today_start_import_cost": round(self._today_start_import_cost, 3),
            "_today_start_export_income": round(self._today_start_export_income, 3),
            "_last_import_energy_kwh": (
                round(self._last_import_energy_kwh, 3)
                if self._last_import_energy_kwh is not None
                else None
            ),
            "_last_export_energy_kwh": (
                round(self._last_export_energy_kwh, 3)
                if self._last_export_energy_kwh is not None
                else None
            ),
            "today": self.today,
            "daily_log": [
                e.as_dict()
                for e in sorted(self.daily_log.values(), key=lambda e: e.date)
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FinancialTracker:
        """Create from a deserialised JSON dict."""
        tracker = cls(
            import_cost_total=float(data.get("import_cost_total", 0.0)),
            export_income_total=float(data.get("export_income_total", 0.0)),
            _today_start_import_cost=float(data.get("_today_start_import_cost", 0.0)),
            _today_start_export_income=float(
                data.get("_today_start_export_income", 0.0)
            ),
            today=str(data.get("today", "")),
        )
        last_import = data.get("_last_import_energy_kwh")
        if last_import is not None:
            tracker._last_import_energy_kwh = float(last_import)
        last_export = data.get("_last_export_energy_kwh")
        if last_export is not None:
            tracker._last_export_energy_kwh = float(last_export)

        daily_list = data.get("daily_log", [])
        if isinstance(daily_list, list):
            for entry_data in daily_list:
                entry = FinancialDayEntry.from_dict(entry_data)
                if entry.date:
                    tracker.daily_log[entry.date] = entry

        return tracker

    @staticmethod
    def _read_history_file(path: Any) -> dict[str, Any] | None:
        """Read and parse the history JSON file (sync, offloaded to thread)."""
        import json

        try:
            with open(str(path), encoding="utf-8") as f:
                return json.load(f)  # type: ignore[no-any-return]
        except json.JSONDecodeError, OSError:
            return None

    @staticmethod
    def _write_history_file(data: dict[str, Any], path: Any) -> bool:
        """Write the history data to disk atomically (sync, offloaded to thread)."""
        import json
        import os
        import tempfile
        from contextlib import suppress
        from pathlib import Path as _Path

        path_obj = _Path(str(path))
        try:
            fd, tmp_path = tempfile.mkstemp(
                suffix=".json",
                prefix=".hsem_financial_history_",
                dir=str(path_obj.parent),
                text=True,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, str(path_obj))
            except Exception:
                with suppress(OSError):
                    os.unlink(tmp_path)
                raise
            return True
        except OSError:
            return False

"""Dataclass for a single day's plan-vs-actual record."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from custom_components.hsem.models.daily_diff import DailyDiff
from custom_components.hsem.models.daily_metrics import DailyMetrics


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

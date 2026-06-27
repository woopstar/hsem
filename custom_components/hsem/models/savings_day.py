"""Dataclass for a single day's savings tracking record."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SavingsDay:
    """A single day's savings metrics.

    Attributes:
        date: ISO-format date string (e.g. ``"2026-06-26"``).
        actual_savings: Cumulative actual savings for the day (currency).
        missed_savings: Cumulative missed savings while switch was off.
        baseline_cost: Cumulative baseline cost for the day.
    """

    date: str
    actual_savings: float = 0.0
    missed_savings: float = 0.0
    baseline_cost: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Return the day as a JSON-serialisable dict."""
        return {
            "date": self.date,
            "actual_savings": round(self.actual_savings, 4),
            "missed_savings": round(self.missed_savings, 4),
            "baseline_cost": round(self.baseline_cost, 4),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SavingsDay:
        """Create a SavingsDay from a deserialised JSON dict."""
        return cls(
            date=str(data.get("date", "")),
            actual_savings=float(data.get("actual_savings", 0.0)),
            missed_savings=float(data.get("missed_savings", 0.0)),
            baseline_cost=float(data.get("baseline_cost", 0.0)),
        )

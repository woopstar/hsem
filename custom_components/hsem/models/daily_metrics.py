"""Dataclass for cumulative energy and cost metrics for one tracking category.

All energy values are in kWh.  All cost values are in local currency.
"""

from __future__ import annotations

from dataclasses import dataclass
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

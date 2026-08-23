"""Aggregated forecast-error summary.

Extracted from ``forecast_tracker.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: no behaviour change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Aggregated error summary
# ---------------------------------------------------------------------------


@dataclass
class ForecastErrorSummary:
    """Rolling-window summary of forecast accuracy.

    Attributes:
        window_slots:
            Number of slots in the rolling window.
        mae_pv_kwh:
            Mean absolute error for PV across all finalised slots (kWh).
        mae_load_kwh:
            Mean absolute error for load across all finalised slots (kWh).
        bias_pv_kwh:
            Mean signed bias for PV across all finalised slots (kWh).
            Positive = systematic over-forecast.
        bias_load_kwh:
            Mean signed bias for load across all finalised slots (kWh).
        rmse_pv_kwh:
            Root mean squared error for PV (kWh).
        rmse_load_kwh:
            Root mean squared error for load (kWh).
        finalised_count:
            How many slots have been finalised and contribute to the metrics.
        mape_pv_pct:
            Mean absolute percentage error for PV (%).  ``None`` when no
            actual PV data exists (all zeros) to avoid division by zero.
        mape_load_pct:
            Mean absolute percentage error for load (%).  ``None`` when no
            actual load data exists.
    """

    window_slots: int = 0
    mae_pv_kwh: float = 0.0
    mae_load_kwh: float = 0.0
    bias_pv_kwh: float = 0.0
    bias_load_kwh: float = 0.0
    rmse_pv_kwh: float = 0.0
    rmse_load_kwh: float = 0.0
    finalised_count: int = 0
    mape_pv_pct: float | None = None
    mape_load_pct: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dictionary for sensor attributes."""
        return {
            "window_slots": self.window_slots,
            "finalised_slots": self.finalised_count,
            "mae_pv_kwh": round(self.mae_pv_kwh, 4),
            "mae_load_kwh": round(self.mae_load_kwh, 4),
            "bias_pv_kwh": round(self.bias_pv_kwh, 4),
            "bias_load_kwh": round(self.bias_load_kwh, 4),
            "rmse_pv_kwh": round(self.rmse_pv_kwh, 4),
            "rmse_load_kwh": round(self.rmse_load_kwh, 4),
            "mape_pv_pct": (
                round(self.mape_pv_pct, 2) if self.mape_pv_pct is not None else None
            ),
            "mape_load_pct": (
                round(self.mape_load_pct, 2) if self.mape_load_pct is not None else None
            ),
        }

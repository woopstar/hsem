"""PredictionRecord dataclass for the prediction accuracy tracker.

Holds one slot's predicted-vs-actual values for SoC, PV, load, and action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class PredictionRecord:
    """One slot's prediction-vs-actual comparison."""

    slot_start: datetime
    predicted_soc_pct: float
    actual_soc_pct: float
    predicted_pv_kwh: float
    actual_pv_kwh: float
    predicted_load_kwh: float
    actual_load_kwh: float
    action: str

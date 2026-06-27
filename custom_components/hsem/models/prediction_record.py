"""Dataclass for a single prediction-vs-actual accuracy record."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class PredictionRecord:
    """One slot's predicted and actual values for accuracy tracking.

    Attributes:
        slot_start:
            Timezone-aware start of the slot.
        predicted_soc_pct:
            Planner-predicted battery SoC (%) at end of slot.
        actual_soc_pct:
            Actual battery SoC (%) at end of slot (from live state).
        predicted_pv_kwh:
            Forecast PV production for this slot (kWh).
        actual_pv_kwh:
            Actual PV production during this slot (kWh, accumulated).
        predicted_load_kwh:
            Predicted house load for this slot (kWh).
        actual_load_kwh:
            Actual house load during this slot (kWh, accumulated).
        action:
            Recommendation action for this slot
            (``"charge"``, ``"discharge"``, or ``"idle"``).
    """

    slot_start: datetime
    predicted_soc_pct: float
    actual_soc_pct: float
    predicted_pv_kwh: float
    actual_pv_kwh: float
    predicted_load_kwh: float
    actual_load_kwh: float
    action: str

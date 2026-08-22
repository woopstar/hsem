"""Dataclass for a single prediction-vs-actual accuracy record."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from custom_components.hsem.utils.persistence import (
    aware_datetime_from_iso,
    finite_float,
)


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

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {
            "slot_start": self.slot_start.isoformat(),
            "predicted_soc_pct": self.predicted_soc_pct,
            "actual_soc_pct": self.actual_soc_pct,
            "predicted_pv_kwh": self.predicted_pv_kwh,
            "actual_pv_kwh": self.actual_pv_kwh,
            "predicted_load_kwh": self.predicted_load_kwh,
            "actual_load_kwh": self.actual_load_kwh,
            "action": self.action,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PredictionRecord | None:
        """Restore one validated record, or return None for malformed data."""
        slot_start = aware_datetime_from_iso(data.get("slot_start"))
        values = (
            finite_float(data.get("predicted_soc_pct"), minimum=0.0, maximum=100.0),
            finite_float(data.get("actual_soc_pct"), minimum=0.0, maximum=100.0),
            finite_float(data.get("predicted_pv_kwh"), minimum=0.0),
            finite_float(data.get("actual_pv_kwh"), minimum=0.0),
            finite_float(data.get("predicted_load_kwh"), minimum=0.0),
            finite_float(data.get("actual_load_kwh"), minimum=0.0),
        )
        action = data.get("action")
        if slot_start is None or any(value is None for value in values):
            return None
        if action not in {"charge", "discharge", "idle"}:
            return None
        return cls(
            slot_start=slot_start,
            predicted_soc_pct=values[0],  # type: ignore[arg-type]
            actual_soc_pct=values[1],  # type: ignore[arg-type]
            predicted_pv_kwh=values[2],  # type: ignore[arg-type]
            actual_pv_kwh=values[3],  # type: ignore[arg-type]
            predicted_load_kwh=values[4],  # type: ignore[arg-type]
            actual_load_kwh=values[5],  # type: ignore[arg-type]
            action=action,
        )

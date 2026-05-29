"""Forecast-vs-actual tracking for PV and load predictions.

The :class:`ForecastTracker` maintains a rolling ring-buffer of recent slots
with their forecasted and actual PV / load values.  Each coordinator cycle
accumulates actual energy from instantaneous power readings, and once a
slot's end time has passed the forecast error is finalised and available for
diagnostic display.

This module has **no** Home Assistant dependencies and is fully testable with
plain ``pytest``.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Slot record stored in the tracker
# ---------------------------------------------------------------------------


@dataclass
class ForecastSlotRecord:
    """One slot's forecast and accumulated actual values.

    Attributes:
        start:
            Timezone-aware start of the slot.
        end:
            Timezone-aware end of the slot.
        forecast_pv_kwh:
            Predicted PV production for this slot (kWh).
        forecast_load_kwh:
            Predicted house load for this slot (kWh).
        actual_pv_kwh:
            Accumulated actual PV production during this slot (kWh).
        actual_load_kwh:
            Accumulated actual house load during this slot (kWh).
        finalised:
            ``True`` once the slot's end time has passed and error metrics
            have been computed and frozen.
        mae_pv:
            Mean absolute error for PV (kWh).  ``None`` if not yet finalised.
        mae_load:
            Mean absolute error for load (kWh).  ``None`` if not yet finalised.
        bias_pv:
            Signed bias for PV (kWh).  Positive = over-forecast (predicted >
            actual).  ``None`` if not yet finalised.
        bias_load:
            Signed bias for load (kWh).  Positive = over-forecast.
            ``None`` if not yet finalised.
    """

    start: datetime
    end: datetime
    forecast_pv_kwh: float = 0.0
    forecast_load_kwh: float = 0.0
    actual_pv_kwh: float = 0.0
    actual_load_kwh: float = 0.0
    finalised: bool = False
    mae_pv: float | None = None
    mae_load: float | None = None
    bias_pv: float | None = None
    bias_load: float | None = None

    def accumulate_pv(self, energy_kwh: float) -> None:
        """Add *energy_kwh* of measured PV to the slot accumulator.

        Must not be called after the record is finalised.

        Args:
            energy_kwh: PV energy in kWh measured over one accumulation
                interval.
        """
        self.actual_pv_kwh += energy_kwh

    def accumulate_load(self, energy_kwh: float) -> None:
        """Add *energy_kwh* of measured house load to the slot accumulator.

        Must not be called after the record is finalised.

        Args:
            energy_kwh: Load energy in kWh measured over one accumulation
                interval.
        """
        self.actual_load_kwh += energy_kwh

    def finalise(self) -> None:
        """Freeze the slot and compute error metrics.

        After calling this, ``accumulate_pv`` and ``accumulate_load`` must
        no longer be called.  This method is idempotent.
        """
        if self.finalised:
            return

        self.mae_pv = abs(self.forecast_pv_kwh - self.actual_pv_kwh)
        self.mae_load = abs(self.forecast_load_kwh - self.actual_load_kwh)
        self.bias_pv = self.forecast_pv_kwh - self.actual_pv_kwh
        self.bias_load = self.forecast_load_kwh - self.actual_load_kwh
        self.finalised = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize the record to a JSON-safe dictionary.

        Returns:
            A dictionary with ISO-format timestamps and plain numeric values.
        """
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "forecast_pv_kwh": self.forecast_pv_kwh,
            "forecast_load_kwh": self.forecast_load_kwh,
            "actual_pv_kwh": self.actual_pv_kwh,
            "actual_load_kwh": self.actual_load_kwh,
            "finalised": self.finalised,
            "mae_pv": self.mae_pv,
            "mae_load": self.mae_load,
            "bias_pv": self.bias_pv,
            "bias_load": self.bias_load,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ForecastSlotRecord:
        """Deserialize a record from a dictionary produced by :meth:`to_dict`.

        Args:
            data: A dictionary previously produced by :meth:`to_dict`.

        Returns:
            A reconstructed :class:`ForecastSlotRecord`.
        """
        from datetime import datetime as dt

        return ForecastSlotRecord(
            start=dt.fromisoformat(data["start"]),
            end=dt.fromisoformat(data["end"]),
            forecast_pv_kwh=data.get("forecast_pv_kwh", 0.0),
            forecast_load_kwh=data.get("forecast_load_kwh", 0.0),
            actual_pv_kwh=data.get("actual_pv_kwh", 0.0),
            actual_load_kwh=data.get("actual_load_kwh", 0.0),
            finalised=data.get("finalised", False),
            mae_pv=data.get("mae_pv"),
            mae_load=data.get("mae_load"),
            bias_pv=data.get("bias_pv"),
            bias_load=data.get("bias_load"),
        )


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


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class ForecastTracker:
    """Rolling ring-buffer that tracks forecast-vs-actual accuracy.

    Usage
    -----
    1. Each coordinator cycle, call :meth:`get_or_create_record` for the
       current slot, then :meth:`accumulate_actuals` with the instantaneous
       power readings and elapsed time since the last cycle.
    2. After a slot's end time has passed, call :meth:`finalise_record` to
       lock the comparison and compute error metrics.
    3. Read :attr:`summary` for the aggregated error snapshot.
    """

    def __init__(self, max_slots: int = 96) -> None:
        """Initialise the tracker.

        Args:
            max_slots: Maximum number of slot records to retain.  Older
                records are discarded.  Default 96 covers 24 h of 15-min
                slots.
        """
        self._max_slots = max_slots
        # Sorted by start time, oldest first.
        self._records: list[ForecastSlotRecord] = []

    @property
    def records(self) -> list[ForecastSlotRecord]:
        """Return a copy of all tracked records, oldest first."""
        return list(self._records)

    @property
    def summary(self) -> ForecastErrorSummary:
        """Compute and return a summary of all finalised records."""
        finalised = [r for r in self._records if r.finalised]
        if not finalised:
            return ForecastErrorSummary(window_slots=len(self._records))

        mae_pv: float = statistics.mean(
            r.mae_pv for r in finalised if r.mae_pv is not None
        )  # type: ignore[misc]
        mae_load: float = statistics.mean(
            r.mae_load for r in finalised if r.mae_load is not None
        )  # type: ignore[misc]
        bias_pv: float = statistics.mean(
            r.bias_pv for r in finalised if r.bias_pv is not None
        )  # type: ignore[misc]
        bias_load: float = statistics.mean(
            r.bias_load for r in finalised if r.bias_load is not None
        )  # type: ignore[misc]

        # RMSE
        rmse_pv = statistics.sqrt(
            statistics.mean(
                (r.forecast_pv_kwh - r.actual_pv_kwh) ** 2 for r in finalised
            )
        )
        rmse_load = statistics.sqrt(
            statistics.mean(
                (r.forecast_load_kwh - r.actual_load_kwh) ** 2 for r in finalised
            )
        )

        # MAPE — avoid division by zero
        actual_pv_values = [r.actual_pv_kwh for r in finalised]
        actual_load_values = [r.actual_load_kwh for r in finalised]

        mape_pv: float | None = None
        if any(abs(v) > 1e-9 for v in actual_pv_values):
            pv_ape = [
                abs(r.forecast_pv_kwh - r.actual_pv_kwh) / abs(r.actual_pv_kwh)
                for r in finalised
                if abs(r.actual_pv_kwh) > 1e-9
            ]
            if pv_ape:
                mape_pv = statistics.mean(pv_ape) * 100.0

        mape_load: float | None = None
        if any(abs(v) > 1e-9 for v in actual_load_values):
            load_ape = [
                abs(r.forecast_load_kwh - r.actual_load_kwh) / abs(r.actual_load_kwh)
                for r in finalised
                if abs(r.actual_load_kwh) > 1e-9
            ]
            if load_ape:
                mape_load = statistics.mean(load_ape) * 100.0

        return ForecastErrorSummary(
            window_slots=len(self._records),
            mae_pv_kwh=mae_pv,
            mae_load_kwh=mae_load,
            bias_pv_kwh=bias_pv,
            bias_load_kwh=bias_load,
            rmse_pv_kwh=rmse_pv,
            rmse_load_kwh=rmse_load,
            finalised_count=len(finalised),
            mape_pv_pct=mape_pv,
            mape_load_pct=mape_load,
        )

    # ------------------------------------------------------------------
    # Record lifecycle
    # ------------------------------------------------------------------

    def get_or_create_record(
        self, start: datetime, end: datetime
    ) -> ForecastSlotRecord:
        """Return the record matching *start*, creating one if needed.

        Args:
            start: Slot start time (must be timezone-aware).
            end: Slot end time (must be timezone-aware).

        Returns:
            The matching :class:`ForecastSlotRecord`.
        """
        for rec in self._records:
            if _same_slot(rec.start, start):
                return rec

        rec = ForecastSlotRecord(start=start, end=end)
        self._records.append(rec)
        self._prune()
        return rec

    def find_record(self, start: datetime) -> ForecastSlotRecord | None:
        """Return the record with the given *start*, or ``None``.

        Args:
            start: Slot start time to look up.

        Returns:
            The matching record, or ``None``.
        """
        for rec in self._records:
            if _same_slot(rec.start, start):
                return rec
        return None

    def finalise_record(self, start: datetime) -> bool:
        """Finalise the record at *start* if it exists and is not yet finalised.

        Args:
            start: Slot start time.

        Returns:
            ``True`` if the record was found and finalised (or was already
            finalised).  ``False`` if no matching record exists.
        """
        rec = self.find_record(start)
        if rec is None:
            return False
        rec.finalise()
        return True

    def finalise_past_records(self, now: datetime) -> int:
        """Finalise all records whose end time is before *now*.

        Idempotent — already-finalised records are skipped.

        Args:
            now: The current time (timezone-aware).

        Returns:
            Number of records newly finalised by this call.
        """
        count = 0
        for rec in self._records:
            if not rec.finalised and rec.end <= now:
                rec.finalise()
                count += 1
        return count

    def set_forecasts(
        self,
        start: datetime,
        pv_kwh: float,
        load_kwh: float,
    ) -> bool:
        """Set the forecast values for the slot at *start*.

        Only sets forecast if the record has not been finalised yet.

        Args:
            start: Slot start time.
            pv_kwh: Forecast PV energy (kWh).
            load_kwh: Forecast load energy (kWh).

        Returns:
            ``True`` if the forecast was set, ``False`` if no matching
            record exists or it is already finalised.
        """
        rec = self.find_record(start)
        if rec is None or rec.finalised:
            return False
        rec.forecast_pv_kwh = pv_kwh
        rec.forecast_load_kwh = load_kwh
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prune(self) -> None:
        """Remove the oldest records when the buffer exceeds the max size."""
        while len(self._records) > self._max_slots:
            self._records.pop(0)

    # ------------------------------------------------------------------
    # Serialization (reboot persistence)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize all tracker records to a JSON-safe dictionary.

        Returns:
            A dictionary with the full record list suitable for storage
            in a Home Assistant sensor's ``extra_state_attributes``.
        """
        return {
            "records": [r.to_dict() for r in self._records],
        }

    def load_from_dict(self, data: dict[str, Any]) -> None:
        """Restore tracker records from a dictionary previously produced
        by :meth:`to_dict`.

        This replaces the current record list.  Call once on startup
        after deserializing from HA storage.

        Args:
            data: A dictionary previously produced by :meth:`to_dict`.
        """
        raw_records = data.get("records", [])
        self._records = [ForecastSlotRecord.from_dict(r) for r in raw_records]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _same_slot(a: datetime, b: datetime) -> bool:
    """Return ``True`` when *a* and *b* represent the same slot start."""
    return a == b


def compute_accumulated_energy(power_w: float, elapsed_seconds: float) -> float:
    """Convert instantaneous power and elapsed time to energy in kWh.

    Args:
        power_w: Instantaneous power in Watts.
        elapsed_seconds: Elapsed time in seconds.

    Returns:
        Energy in kWh.
    """
    return power_w * (elapsed_seconds / 3600.0) / 1000.0

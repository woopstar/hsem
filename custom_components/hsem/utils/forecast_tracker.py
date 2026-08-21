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

import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.hsem.utils.persistence import (
    aware_datetime_from_iso,
    finite_float,
)

_MAX_RESTORED_ENERGY_KWH = 1_000_000.0

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
            Corrected PV prediction for this slot (kWh).
        raw_forecast_pv_kwh:
            Raw, pre-correction PV prediction frozen before the slot starts.
        forecast_load_kwh:
            Predicted house load for this slot (kWh).
        forecast_soc_pct:
            Predicted end-of-slot battery SoC frozen before the slot starts.
        forecast_action:
            Predicted action label frozen before the slot starts.
        actual_pv_kwh:
            Accumulated actual PV production during this slot (kWh).
        actual_load_kwh:
            Accumulated actual house load during this slot (kWh).
        forecast_frozen:
            Whether the pre-slot baseline has been frozen.
        actual_coverage_seconds:
            Seconds of the physical slot covered by trusted power samples.
            ``None`` retains compatibility with records created by older
            versions and direct callers.
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
    raw_forecast_pv_kwh: float | None = None
    forecast_soc_pct: float | None = None
    forecast_action: str | None = None
    forecast_frozen: bool = False
    actual_coverage_seconds: float | None = None

    @property
    def accuracy_eligible(self) -> bool:
        """Return whether this record has an uncontaminated full-slot baseline."""
        if not self.forecast_frozen or self.raw_forecast_pv_kwh is None:
            return False
        if self.actual_coverage_seconds is None:
            return True
        duration_seconds = (_utc_key(self.end) - _utc_key(self.start)).total_seconds()
        return (
            duration_seconds > 0
            and self.actual_coverage_seconds >= duration_seconds - 1.0
        )

    @property
    def prediction_eligible(self) -> bool:
        """Return whether all frozen PredictionTracker inputs are available."""
        return (
            self.accuracy_eligible
            and self.forecast_soc_pct is not None
            and self.forecast_action is not None
        )

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
            "raw_forecast_pv_kwh": self.raw_forecast_pv_kwh,
            "forecast_load_kwh": self.forecast_load_kwh,
            "forecast_soc_pct": self.forecast_soc_pct,
            "forecast_action": self.forecast_action,
            "actual_pv_kwh": self.actual_pv_kwh,
            "actual_load_kwh": self.actual_load_kwh,
            "forecast_frozen": self.forecast_frozen,
            "actual_coverage_seconds": self.actual_coverage_seconds,
            "finalised": self.finalised,
            "mae_pv": self.mae_pv,
            "mae_load": self.mae_load,
            "bias_pv": self.bias_pv,
            "bias_load": self.bias_load,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> ForecastSlotRecord:
        """Deserialize a record from a dictionary produced by :meth:`to_dict`.

        Args:
            data: A dictionary previously produced by :meth:`to_dict`.

        Returns:
            A reconstructed :class:`ForecastSlotRecord`.
        """
        start = aware_datetime_from_iso(data.get("start"))
        end = aware_datetime_from_iso(data.get("end"))
        if start is None or end is None:
            raise ValueError("forecast record timestamps must be timezone-aware")
        duration_seconds = (_utc_key(end) - _utc_key(start)).total_seconds()
        if (
            duration_seconds <= 0
            or duration_seconds > timedelta(hours=1).total_seconds()
        ):
            raise ValueError("forecast record timestamps are not an ordered slot")

        forecast_pv_kwh = _required_finite(
            data.get("forecast_pv_kwh", 0.0),
            minimum=0.0,
            maximum=_MAX_RESTORED_ENERGY_KWH,
        )
        forecast_load_kwh = _required_finite(
            data.get("forecast_load_kwh", 0.0),
            minimum=0.0,
            maximum=_MAX_RESTORED_ENERGY_KWH,
        )
        actual_pv_kwh = _required_finite(
            data.get("actual_pv_kwh", 0.0),
            minimum=0.0,
            maximum=_MAX_RESTORED_ENERGY_KWH,
        )
        actual_load_kwh = _required_finite(
            data.get("actual_load_kwh", 0.0),
            minimum=0.0,
            maximum=_MAX_RESTORED_ENERGY_KWH,
        )
        raw_forecast_pv_kwh = _optional_finite(
            data.get("raw_forecast_pv_kwh"),
            minimum=0.0,
            maximum=_MAX_RESTORED_ENERGY_KWH,
        )
        forecast_soc_pct = _optional_finite(
            data.get("forecast_soc_pct"), minimum=0.0, maximum=100.0
        )
        actual_coverage_seconds = _optional_finite(
            data.get("actual_coverage_seconds"),
            minimum=0.0,
            maximum=duration_seconds + 1.0,
        )

        forecast_action = data.get("forecast_action")
        if forecast_action is not None and (
            not isinstance(forecast_action, str) or len(forecast_action) > 128
        ):
            raise ValueError("forecast action must be a bounded string")

        forecast_frozen = data.get("forecast_frozen", False)
        finalised = data.get("finalised", False)
        if not isinstance(forecast_frozen, bool) or not isinstance(finalised, bool):
            raise TypeError("forecast lifecycle flags must be booleans")

        record = ForecastSlotRecord(
            start=start,
            end=end,
            forecast_pv_kwh=forecast_pv_kwh,
            raw_forecast_pv_kwh=raw_forecast_pv_kwh,
            forecast_load_kwh=forecast_load_kwh,
            forecast_soc_pct=forecast_soc_pct,
            forecast_action=forecast_action,
            actual_pv_kwh=actual_pv_kwh,
            actual_load_kwh=actual_load_kwh,
            forecast_frozen=forecast_frozen,
            actual_coverage_seconds=actual_coverage_seconds,
        )
        # Error fields are derived state. Recompute them instead of trusting
        # persisted MAE/bias values that could be stale or non-finite.
        if finalised:
            record.finalise()
        return record


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
        self._restored_unfinalised_keys: set[datetime] = set()

    @property
    def records(self) -> list[ForecastSlotRecord]:
        """Return a copy of all tracked records, oldest first."""
        return list(self._records)

    @property
    def restored_unfinalised_keys(self) -> set[datetime]:
        """Return every restored slot whose lifecycle was not finalised."""
        return set(self._restored_unfinalised_keys)

    @property
    def summary(self) -> ForecastErrorSummary:
        """Compute and return a summary of all finalised records."""
        finalised = [r for r in self._records if r.finalised and r.accuracy_eligible]
        if not finalised:
            return ForecastErrorSummary(window_slots=len(self._records))

        mae_pv: float = statistics.mean(
            r.mae_pv for r in finalised if r.mae_pv is not None
        )  # type: ignore[misc]  # statistics.mean rejects generator of optional float
        mae_load: float = statistics.mean(
            r.mae_load for r in finalised if r.mae_load is not None
        )  # type: ignore[misc]  # statistics.mean rejects generator of optional float
        bias_pv: float = statistics.mean(
            r.bias_pv for r in finalised if r.bias_pv is not None
        )  # type: ignore[misc]  # statistics.mean rejects generator of optional float
        bias_load: float = statistics.mean(
            r.bias_load for r in finalised if r.bias_load is not None
        )  # type: ignore[misc]  # statistics.mean rejects generator of optional float

        # RMSE
        rmse_pv = math.sqrt(
            statistics.mean(
                (r.forecast_pv_kwh - r.actual_pv_kwh) ** 2 for r in finalised
            )
        )
        rmse_load = math.sqrt(
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
        self._records.sort(key=lambda record: _utc_key(record.start))
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

    def reconcile_unfinalised_layout(
        self,
        expected_slots: Iterable[tuple[datetime, datetime]],
        *,
        now: datetime,
    ) -> bool:
        """Discard an incompatible live layout while keeping finalised history.

        Returns ``True`` when active/future records did not match the current
        recommendation starts and ends.  Callers use that signal to reset
        instantaneous-power endpoints so no interval bridges the change.
        """
        expected = {_utc_key(start): _utc_key(end) for start, end in expected_slots}
        now_key = _utc_key(now)
        incompatible = any(
            expected.get(_utc_key(record.start)) != _utc_key(record.end)
            for record in self._records
            if not record.finalised and _utc_key(record.end) > now_key
        )
        if not incompatible:
            return False

        self._records = [record for record in self._records if record.finalised]
        retained = {_utc_key(record.start) for record in self._records}
        self._restored_unfinalised_keys.intersection_update(retained)
        return True

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
            if not rec.finalised and _utc_key(rec.end) <= _utc_key(now):
                rec.finalise()
                count += 1
        return count

    def freeze_forecasts(self, now: datetime) -> int:
        """Freeze baselines for slots that have physically started."""
        count = 0
        now_key = _utc_key(now)
        for rec in self._records:
            if (
                not rec.finalised
                and not rec.forecast_frozen
                and rec.raw_forecast_pv_kwh is not None
                and _utc_key(rec.start) <= now_key
            ):
                rec.forecast_frozen = True
                count += 1
        return count

    def set_forecasts(
        self,
        start: datetime,
        pv_kwh: float,
        load_kwh: float,
        *,
        raw_pv_kwh: float | None = None,
        forecast_soc_pct: float | None = None,
        forecast_action: str | None = None,
        observed_at: datetime | None = None,
    ) -> bool:
        """Set an eligible pre-slot forecast baseline.

        Production callers pass ``observed_at`` and may update a future slot
        until its physical start.  Direct callers that omit it retain the
        legacy one-shot behaviour and freeze the baseline immediately.

        Args:
            start: Slot start time.
            pv_kwh: Corrected forecast PV energy (kWh).
            load_kwh: Forecast load energy (kWh).
            raw_pv_kwh: Raw PV forecast before correction.
            forecast_soc_pct: Predicted end-of-slot battery SoC (%).
            forecast_action: Predicted action label.
            observed_at: Time at which this forecast was observed.

        Returns:
            ``True`` if the forecast was set, ``False`` if no matching
            record exists, the baseline is frozen, or the slot has started.
        """
        rec = self.find_record(start)
        if rec is None or rec.finalised or rec.forecast_frozen:
            return False
        if observed_at is not None and _utc_key(observed_at) >= _utc_key(rec.start):
            return False
        rec.forecast_pv_kwh = pv_kwh
        rec.raw_forecast_pv_kwh = pv_kwh if raw_pv_kwh is None else raw_pv_kwh
        rec.forecast_load_kwh = load_kwh
        rec.forecast_soc_pct = forecast_soc_pct
        rec.forecast_action = forecast_action
        if observed_at is None:
            rec.forecast_frozen = True
        elif rec.actual_coverage_seconds is None:
            rec.actual_coverage_seconds = 0.0
        return True

    def accumulate_power_interval(
        self,
        start: datetime,
        end: datetime,
        *,
        pv_power_w: float,
        load_power_w: float,
        max_gap_seconds: float,
    ) -> float:
        """Allocate prior-sample power over ``[start, end)`` by UTC overlap.

        Long gaps are rejected rather than treating a stale power sample as
        representative.  The return value is the allocated number of seconds.
        """
        start_key = _utc_key(start)
        end_key = _utc_key(end)
        elapsed_seconds = (end_key - start_key).total_seconds()
        if (
            elapsed_seconds <= 0
            or max_gap_seconds <= 0
            or elapsed_seconds > max_gap_seconds
        ):
            return 0.0

        assigned_seconds = 0.0
        for rec in self._records:
            if rec.finalised or not rec.forecast_frozen:
                continue
            overlap_start = max(start_key, _utc_key(rec.start))
            overlap_end = min(end_key, _utc_key(rec.end))
            overlap_seconds = (overlap_end - overlap_start).total_seconds()
            if overlap_seconds <= 0:
                continue
            rec.accumulate_pv(compute_accumulated_energy(pv_power_w, overlap_seconds))
            rec.accumulate_load(
                compute_accumulated_energy(load_power_w, overlap_seconds)
            )
            if rec.actual_coverage_seconds is not None:
                rec.actual_coverage_seconds += overlap_seconds
            assigned_seconds += overlap_seconds
        return assigned_seconds

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

    def to_persistence_dict(
        self,
        *,
        now: datetime,
        max_records: int = 24,
    ) -> dict[str, Any]:
        """Serialize the bounded records that matter across a restart.

        The active/frozen slot is retained first, followed by the newest
        eligible finalised records and then the nearest future baselines.
        This prevents a long pre-created planning horizon from filling HA's
        attribute budget with only the farthest-future slots.
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("persistence reference time must be timezone-aware")
        if max_records <= 0:
            return {"records": []}

        now_key = _utc_key(now)
        active = sorted(
            (
                record
                for record in self._records
                if not record.finalised
                and _utc_key(record.start) <= now_key < _utc_key(record.end)
            ),
            key=lambda record: _utc_key(record.start),
            reverse=True,
        )
        frozen = sorted(
            (
                record
                for record in self._records
                if not record.finalised
                and record.forecast_frozen
                and record not in active
            ),
            key=lambda record: _utc_key(record.start),
            reverse=True,
        )
        finalised = sorted(
            (
                record
                for record in self._records
                if record.finalised and record.accuracy_eligible
            ),
            key=lambda record: _utc_key(record.start),
            reverse=True,
        )
        future = sorted(
            (
                record
                for record in self._records
                if not record.finalised
                and not record.forecast_frozen
                and record.raw_forecast_pv_kwh is not None
                and _utc_key(record.start) > now_key
            ),
            key=lambda record: _utc_key(record.start),
        )

        selected: list[ForecastSlotRecord] = []
        selected_keys: set[datetime] = set()
        for category in (active, frozen, finalised, future):
            for record in category:
                key = _utc_key(record.start)
                if key in selected_keys:
                    continue
                selected.append(record)
                selected_keys.add(key)
                if len(selected) >= max_records:
                    break
            if len(selected) >= max_records:
                break

        selected.sort(key=lambda record: _utc_key(record.start))
        return {"records": [record.to_dict() for record in selected]}

    def load_from_dict(
        self,
        data: Any,
    ) -> None:
        """Restore valid tracker records without trusting persisted values.

        Invalid records are skipped. The retained records are UTC-sorted,
        de-duplicated by physical start, and bounded by ``max_slots``.
        """
        self._records = []
        self._restored_unfinalised_keys.clear()
        if not isinstance(data, Mapping):
            return
        raw_records = data.get("records", [])
        if not isinstance(raw_records, list):
            return

        parsed_records: list[ForecastSlotRecord] = []
        for raw_record in raw_records:
            if not isinstance(raw_record, Mapping):
                continue
            try:
                record = ForecastSlotRecord.from_dict(raw_record)
            except TypeError, ValueError, OverflowError:
                continue
            parsed_records.append(record)

        parsed_records.sort(key=lambda record: _utc_key(record.start))
        seen: set[datetime] = set()
        for record in parsed_records:
            key = _utc_key(record.start)
            if key in seen:
                continue
            seen.add(key)
            self._records.append(record)
            if not record.finalised:
                self._restored_unfinalised_keys.add(key)

        if len(self._records) > self._max_slots:
            self._records = self._records[-self._max_slots :]
            retained = {_utc_key(record.start) for record in self._records}
            self._restored_unfinalised_keys.intersection_update(retained)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _same_slot(a: datetime, b: datetime) -> bool:
    """Return ``True`` when *a* and *b* represent the same slot start."""
    return _utc_key(a) == _utc_key(b)


def _utc_key(value: datetime) -> datetime:
    """Return a pure-Python UTC identity without importing HA utilities."""
    return value.astimezone(UTC).replace(microsecond=0)


def _required_finite(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Return a finite restored value or raise for the caller to skip it."""
    parsed = finite_float(value, minimum=minimum, maximum=maximum)
    if parsed is None:
        raise ValueError("restored numeric value is invalid")
    return parsed


def _optional_finite(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    """Return a finite optional restored value or reject invalid state."""
    if value is None:
        return None
    return _required_finite(value, minimum=minimum, maximum=maximum)


def compute_accumulated_energy(power_w: float, elapsed_seconds: float) -> float:
    """Convert instantaneous power and elapsed time to energy in kWh.

    Args:
        power_w: Instantaneous power in Watts.
        elapsed_seconds: Elapsed time in seconds.

    Returns:
        Energy in kWh.
    """
    return power_w * (elapsed_seconds / 3600.0) / 1000.0

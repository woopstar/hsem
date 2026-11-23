"""History of house-load forecast errors per slot (issue #1361).

The planner sizes the pre-peak reserve from how far the load forecast has
been off over a whole window, not over one slot: evening load errors are
correlated, so the per-slot error times ``sqrt(n)`` understates the spread of
an ``n``-slot window by more than half on a real installation.

This profile holds ``actual - forecast`` per slot and answers one question:
over the same local hours on earlier days, how far was the summed forecast
off?  The forecast of a slot is the one that stood before the slot began; the
live measurement the planner puts into a running slot never enters.

Pure Python, no Home Assistant imports.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Protocol

from custom_components.hsem.models.prediction_record import PredictionRecord
from custom_components.hsem.utils.persistence import finite_float

#: Earlier days a window is compared with.  The prediction history holds 30
#: days of 15-minute slots.
LOAD_ERROR_MAX_DAYS = 30

_SCHEMA_VERSION = 1
#: Largest summed-error magnitude accepted from a serialised profile (kWh).
_MAX_ERROR_KWH = 1_000.0


class SlotLoadRecord(Protocol):
    """A finished slot with its load forecast and measured load."""

    start: datetime
    end: datetime
    forecast_load_kwh: float
    actual_load_kwh: float


def _minute_key(value: datetime) -> int:
    """Return the UTC epoch minute of an aware datetime."""
    return int(value.astimezone(UTC).timestamp() // 60)


def window_rmse_kwh(samples: Sequence[float]) -> float:
    """Return the root mean square of window error sums, ``0.0`` when empty.

    Args:
        samples: One summed forecast error per window, in kWh.
    """
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def contiguous_window_rmse_kwh(
    errors: Mapping[int, float], n_slots: int, step_minutes: int
) -> float:
    """Return the RMSE of errors summed over every contiguous ``n``-slot window.

    Args:
        errors: Forecast error per slot, keyed by the UTC epoch minute of the
            slot start.
        n_slots: Window length in slots.
        step_minutes: Slot width in minutes.

    Returns:
        The RMSE in kWh, or ``0.0`` when no ``n_slots`` consecutive slots
        exist.
    """
    if n_slots <= 0 or step_minutes <= 0:
        return 0.0
    samples: list[float] = []
    for key in errors:
        total = _run_sum(errors, key, n_slots, step_minutes)
        if total is not None:
            samples.append(total)
    return window_rmse_kwh(samples)


def slot_records_window_rmse_kwh(
    records: Sequence[SlotLoadRecord], n_slots: int
) -> float:
    """Return the RMSE of load errors summed over contiguous ``n``-slot windows.

    Args:
        records: Finished slots of one slot width.
        n_slots: Window length in slots.

    Returns:
        The RMSE in kWh, or ``0.0`` when fewer than ``n_slots`` consecutive
        records exist.
    """
    if not records:
        return 0.0
    step = (_minute_key(records[0].end) - _minute_key(records[0].start)) or 1
    errors = {
        _minute_key(record.start): record.actual_load_kwh - record.forecast_load_kwh
        for record in records
    }
    return contiguous_window_rmse_kwh(errors, n_slots, step)


def _run_sum(
    errors: Mapping[int, float], first_key: int, n_slots: int, step_minutes: int
) -> float | None:
    """Return the summed error of ``n_slots`` slots from *first_key*, or ``None``."""
    total = 0.0
    for offset in range(n_slots):
        error = errors.get(first_key + offset * step_minutes)
        if error is None:
            return None
        total += error
    return total


@dataclass(frozen=True)
class LoadErrorProfile:
    """Load forecast error (``actual - forecast``, kWh) per finished slot.

    Attributes:
        errors: Error per slot, keyed by the UTC epoch minute of the slot
            start.  Positive: the house used more than forecast.
    """

    errors: Mapping[int, float] = field(default_factory=dict)

    @classmethod
    def from_records(cls, records: Iterable[PredictionRecord]) -> LoadErrorProfile:
        """Build the profile from prediction history records.

        Records without a pre-start load forecast are left out.

        Args:
            records: Prediction-vs-actual records of finished slots.
        """
        errors: dict[int, float] = {}
        for record in records:
            if record.forecast_load_kwh is not None:
                errors[_minute_key(record.slot_start)] = (
                    record.actual_load_kwh - record.forecast_load_kwh
                )
        return cls(errors=errors)

    def window_samples(
        self, slot_starts: Sequence[datetime], zone: tzinfo | None = None
    ) -> list[float]:
        """Return the summed error of the same local hours on earlier days.

        A day counts only when every slot of the window has an error.

        Args:
            slot_starts: Aware start of every slot of the window.
            zone: Time zone whose wall clock defines "the same hours", so a
                DST change does not move the window.  ``None`` steps back in
                whole 24-hour days.

        Returns:
            One summed error per earlier day, newest first.
        """
        samples: list[float] = []
        if not slot_starts or not self.errors:
            return samples
        local = [s.astimezone(zone) if zone is not None else s for s in slot_starts]
        for days_back in range(1, LOAD_ERROR_MAX_DAYS + 1):
            total = self._day_sum(local, timedelta(days=days_back))
            if total is not None:
                samples.append(total)
        return samples

    def _day_sum(
        self, slot_starts: Sequence[datetime], back: timedelta
    ) -> float | None:
        """Return the summed error of the window moved *back*, or ``None``."""
        total = 0.0
        for start in slot_starts:
            error = self.errors.get(_minute_key(start - back))
            if error is None:
                return None
            total += error
        return total

    def as_dict(self) -> dict[str, Any]:
        """Return a compact JSON-safe form: one string per run of slots."""
        runs: list[dict[str, Any]] = []
        values: list[str] = []
        previous: int | None = None
        step: int | None = None
        for key in sorted(self.errors):
            gap = None if previous is None else key - previous
            if previous is not None and (step is None or gap == step):
                step = gap
            elif previous is not None:
                runs[-1].update(step=step or 0, values=",".join(values))
                values, step, previous = [], None, None
            if previous is None:
                runs.append({"start": key})
            values.append(f"{self.errors[key]:.3f}")
            previous = key
        if runs:
            runs[-1].update(step=step or 0, values=",".join(values))
        return {"version": _SCHEMA_VERSION, "runs": runs}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LoadErrorProfile:
        """Rebuild a profile from :meth:`as_dict`, skipping invalid entries."""
        errors: dict[int, float] = {}
        raw_runs = data.get("runs") if data.get("version") == _SCHEMA_VERSION else None
        for run in raw_runs if isinstance(raw_runs, list) else []:
            if not isinstance(run, Mapping):
                continue
            start, step, values = run.get("start"), run.get("step"), run.get("values")
            if not (
                isinstance(start, int)
                and isinstance(step, int)
                and step >= 0
                and isinstance(values, str)
            ):
                continue
            for offset, raw in enumerate(values.split(",")):
                error = finite_float(
                    raw, minimum=-_MAX_ERROR_KWH, maximum=_MAX_ERROR_KWH
                )
                if error is not None:
                    errors[start + offset * step] = error
        return cls(errors=errors)

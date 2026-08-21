"""Per-hour solar forecast accuracy auto-correction.

The :class:`SolarForecastCorrector` maintains learned per-hour accuracy factors
and intra-hour residual corrections, inspired by Solar AI's approach.  It
corrects PV forecasts before they enter the planner engine.

This module has **no** Home Assistant dependencies and is fully testable with
plain ``pytest``.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.persistence import (
    aware_datetime_from_iso,
    finite_float,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Bounds for the per-hour accuracy factor (actual / forecast ratio).
FACTOR_MIN: float = 0.3
FACTOR_MAX: float = 1.5

# Four eligible samples are retained per wall-clock hour.
MAX_HISTORY_PER_HOUR: int = 4

# Maximum number of recent closed-slot residuals retained.
MAX_RESIDUALS: int = 4

# Number of slots over which the intra-hour residual correction decays to 1.0.
RESIDUAL_DECAY_SLOTS: int = 8

CONFIDENCE_DEFAULT: float = 0.50

# Versions before 3 did not persist the exact rolling buffers or replay
# watermark, so restoring their factors could not preserve learning semantics.
SOLAR_CORRECTOR_STATE_VERSION: int = 3

# Threshold below which forecast kWh is treated as zero to avoid division by zero.
_FORECAST_EPS: float = 1e-9

# A deliberately generous per-slot ceiling keeps restored arithmetic finite.
_MAX_ENERGY_KWH: float = 1_000_000.0


@dataclass
class SolarForecastCorrector:
    """Learns and applies per-hour PV forecast accuracy corrections.

    Two-layer correction:
    1. **Per-hour accuracy factor**: Rolling mean of (actual PV / forecast PV)
       over the last four eligible samples for that wall-clock
       hour, clamped to [0.3, 1.5].
    2. **Intra-hour residual correction**: Mean of (actual / forecast) over the
       last four eligible closed physical slots, linearly decayed to 1.0 over
       the next eight physical slots (two hours at 15-minute cadence).

    Attributes:
        hour_factors: Per-hour accuracy factors keyed by hour (0-23).
            Defaults to 1.0 for all hours.
        confidence: Internal correction strength (0.10-0.90).  At 0.50 the
            learned factor is used as-is; lower values reduce the correction
            toward the raw forecast.
    """

    # Per-hour accuracy factor [0.3, 1.5], keyed by hour (0-23).
    hour_factors: dict[int, float] = field(default_factory=dict)

    # Rolling buffer of (forecast, actual) pairs per hour for factor computation.
    _hour_history: dict[int, list[tuple[float, float]]] = field(
        default_factory=dict, repr=False
    )

    # Intra-hour recent residuals: list of (forecast, actual) for last N closed
    # slots.  Most recent entry is last.
    _recent_residuals: list[tuple[float, float]] = field(
        default_factory=list, repr=False
    )

    # Internal correction strength (0.10-0.90, default 0.50).
    # At 0.50 the learned factor is applied fully; lower values push toward 1.0
    # (toward the uncorrected raw forecast).
    confidence: float = CONFIDENCE_DEFAULT

    # Physical planning instant used to derive residual distance.  Ephemeral:
    # it is refreshed each coordinator cycle and deliberately not persisted.
    _reference_time: datetime | None = field(default=None, repr=False)

    # Latest UTC slot start consumed by the rolling buffers. Persisted so a
    # restored finalised tracker record cannot be learned a second time.
    _processed_through: datetime | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_reference_time(self, now: datetime) -> None:
        """Set the physical instant from which future-slot distance is measured."""
        if now.tzinfo is None:
            raise ValueError("reference time must be timezone-aware")
        self._reference_time = now

    @property
    def processed_through(self) -> datetime | None:
        """Return the latest physical slot consumed by the rolling buffers."""
        return self._processed_through

    def mark_processed(self, slot_start: datetime) -> None:
        """Advance the replay watermark to an aware physical slot start."""
        if slot_start.tzinfo is None or slot_start.utcoffset() is None:
            raise ValueError("processed slot start must be timezone-aware")
        key = slot_start.astimezone(UTC).replace(microsecond=0)
        if self._processed_through is None or key > self._processed_through:
            self._processed_through = key

    def was_processed(self, slot_start: datetime) -> bool:
        """Return whether a physical slot is at or before the replay watermark."""
        if slot_start.tzinfo is None or slot_start.utcoffset() is None:
            return False
        return (
            self._processed_through is not None
            and slot_start.astimezone(UTC).replace(microsecond=0)
            <= self._processed_through
        )

    def slots_ahead_for(
        self,
        slot_start: datetime,
        interval_minutes: int,
        *,
        fallback: int,
    ) -> int:
        """Return a UTC-based physical slot distance from the current slot.

        The ceiling keeps an in-progress slot at ``0`` while mapping the next
        whole-hour boundaries correctly (for example +4 and +8 at 15-minute
        granularity).  UTC arithmetic preserves both identities in a DST fold.
        """
        if self._reference_time is None:
            return max(0, fallback)
        if slot_start.tzinfo is None:
            raise ValueError("slot start must be timezone-aware")
        if interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")

        delta_seconds = (
            slot_start.astimezone(UTC) - self._reference_time.astimezone(UTC)
        ).total_seconds()
        return max(0, math.ceil(delta_seconds / (interval_minutes * 60.0)))

    def update_hour(self, hour: int, forecast_kwh: float, actual_kwh: float) -> None:
        """Update the per-hour accuracy factor for a given hour.

        Stores the (forecast, actual) pair in a rolling buffer and recomputes
        the factor as ``mean(actual / forecast)``, clamped to
        [``FACTOR_MIN``, ``FACTOR_MAX``].

        Skips samples where ``forecast_kwh`` is effectively zero (to avoid
        division by zero).

        Args:
            hour: Hour of day (0-23).
            forecast_kwh: Forecast PV energy in kWh.
            actual_kwh: Actual PV energy in kWh.
        """
        if hour < 0 or hour > 23:
            log_planner(
                "warning",
                "[solar_corrector] update_hour called with invalid hour %d — ignoring",
                hour,
            )
            return

        sample = _valid_energy_pair(forecast_kwh, actual_kwh)
        if sample is None or sample[0] < _FORECAST_EPS:
            log_planner(
                "debug",
                "[solar_corrector] update_hour(h=%d) skipped — invalid or "
                "near-zero sample",
                hour,
            )
            return
        forecast_kwh, actual_kwh = sample

        if hour not in self._hour_history:
            self._hour_history[hour] = []

        history = self._hour_history[hour]
        history.append((forecast_kwh, actual_kwh))

        # Keep only the most recent N samples per hour.
        while len(history) > MAX_HISTORY_PER_HOUR:
            history.pop(0)

        # Recompute factor as mean(actual / forecast), clamped.
        ratios = [
            actual / fcast for fcast, actual in history if abs(fcast) >= _FORECAST_EPS
        ]
        if ratios:
            mean_ratio = statistics.mean(ratios)
            self.hour_factors[hour] = max(FACTOR_MIN, min(FACTOR_MAX, mean_ratio))
        else:
            self.hour_factors[hour] = 1.0

        log_planner(
            "debug",
            "[solar_corrector] update_hour(h=%d) factor=%.4f  samples=%d",
            hour,
            self.hour_factors.get(hour, 1.0),
            len(history),
        )

    def update_residual(self, forecast_kwh: float, actual_kwh: float) -> None:
        """Add a closed-slot (forecast, actual) pair to the recent residuals buffer.

        Keeps at most ``MAX_RESIDUALS`` entries (oldest dropped first).

        Args:
            forecast_kwh: Forecast PV energy for the slot in kWh.
            actual_kwh: Actual PV energy for the slot in kWh.
        """
        sample = _valid_energy_pair(forecast_kwh, actual_kwh)
        if sample is None or sample[0] < _FORECAST_EPS:
            return

        self._recent_residuals.append(sample)
        while len(self._recent_residuals) > MAX_RESIDUALS:
            self._recent_residuals.pop(0)

        log_planner(
            "debug",
            "[solar_corrector] update_residual  residual_count=%d",
            len(self._recent_residuals),
        )

    def get_corrected_pv(
        self, hour: int, forecast_kwh: float, slots_ahead: int = 0
    ) -> float:
        """Return the corrected PV estimate for a slot.

        Applies:
        1. Per-hour accuracy factor (scaled by correction strength).
        2. Intra-hour residual factor (decayed by ``slots_ahead``).

        The two corrections multiply::

            corrected = forecast_kwh × hour_factor × residual_factor

        Args:
            hour: Hour of day (0-23) of the slot.
            forecast_kwh: Raw forecast PV energy in kWh.
            slots_ahead: Number of slots into the future from now (0 = current).
                Used to decay the intra-hour residual correction.

        Returns:
            Corrected PV estimate in kWh (never negative for zero forecast).
        """
        if abs(forecast_kwh) < _FORECAST_EPS:
            return 0.0

        # 1. Per-hour accuracy factor with confidence scaling.
        raw_hour_factor = self.hour_factors.get(hour, 1.0)
        hour_factor = self._apply_confidence(raw_hour_factor)

        # 2. Intra-hour residual factor, decayed by slots_ahead.
        residual_factor = self._compute_residual_factor(slots_ahead)

        corrected = forecast_kwh * hour_factor * residual_factor

        log_planner(
            "debug",
            "[solar_corrector] get_corrected_pv(h=%d, forecast=%.4f, ahead=%d)"
            " → hour_factor=%.4f  residual_factor=%.4f  corrected=%.4f",
            hour,
            forecast_kwh,
            slots_ahead,
            hour_factor,
            residual_factor,
            corrected,
        )

        return round(corrected, 4)

    # ------------------------------------------------------------------
    # Serialization (reboot persistence)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize the corrector state to a JSON-safe dictionary.

        Returns:
            A versioned dictionary with the exact bounded rolling state.
        """
        return {
            "schema_version": SOLAR_CORRECTOR_STATE_VERSION,
            "hour_factors": {
                str(hour): factor for hour, factor in sorted(self.hour_factors.items())
            },
            "hour_history": {
                str(hour): [list(sample) for sample in history]
                for hour, history in sorted(self._hour_history.items())
            },
            "recent_residuals": [list(sample) for sample in self._recent_residuals],
            "confidence": self.confidence,
            "processed_through": (
                self._processed_through.isoformat()
                if self._processed_through is not None
                else None
            ),
        }

    def load_from_dict(
        self,
        data: Any,
        *,
        restored_at: datetime | None = None,
    ) -> None:
        """Restore a validated corrector state or intentionally cold-reset.

        Versions before 3 lack the exact rolling buffers and replay watermark,
        so their learned state cannot be resumed faithfully. Malformed current
        payloads are rejected atomically. A valid bounded confidence control is
        retained across either kind of cold reset.

        Args:
            data: A value previously produced by :meth:`to_dict`.
        """
        safe_confidence = (
            finite_float(self.confidence, minimum=0.10, maximum=0.90)
            or CONFIDENCE_DEFAULT
        )
        if isinstance(data, Mapping):
            restored_confidence = finite_float(
                data.get("confidence"), minimum=0.10, maximum=0.90
            )
            if restored_confidence is not None:
                safe_confidence = restored_confidence

        self.hour_factors.clear()
        self._hour_history.clear()
        self._recent_residuals.clear()
        self._processed_through = None
        self.confidence = safe_confidence

        if (
            not isinstance(data, Mapping)
            or type(data.get("schema_version")) is not int
            or data.get("schema_version") != SOLAR_CORRECTOR_STATE_VERSION
        ):
            log_planner(
                "debug",
                "[solar_corrector] discarded pre-v3 learned state; rebuilding "
                "from frozen forecast baselines",
            )
            return

        restore_reference = restored_at or datetime.now(UTC)
        if restore_reference.tzinfo is None or restore_reference.utcoffset() is None:
            log_planner(
                "warning",
                "[solar_corrector] discarded v3 state without an aware "
                "restore reference",
            )
            return
        try:
            restore_reference_utc = restore_reference.astimezone(UTC)
        except OverflowError, ValueError:
            return

        restored = _parse_current_state(
            data,
            restored_at=restore_reference_utc,
        )
        if restored is None:
            log_planner(
                "warning",
                "[solar_corrector] discarded malformed v3 persisted state",
            )
            return

        (
            self.hour_factors,
            self._hour_history,
            self._recent_residuals,
            self.confidence,
            self._processed_through,
        ) = restored

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_confidence(self, raw_factor: float) -> float:
        """Scale the per-hour factor by the correction strength.

        At ``confidence = 0.50`` the raw factor is applied at full strength.
        Below 0.50 the correction is dampened toward 1.0 (toward the raw
        forecast). Above 0.50 the correction is still applied at full strength
        but never amplified beyond the raw factor.

        Args:
            raw_factor: The raw learned factor for the hour.

        Returns:
            Confidence-adjusted factor.
        """
        # confidence_scale: 0.0 at confidence=0, 1.0 at confidence=0.5, capped at 1.0
        confidence_scale = min(1.0, self.confidence / 0.5)
        return 1.0 + (raw_factor - 1.0) * confidence_scale

    def _compute_residual_factor(self, slots_ahead: int) -> float:
        """Compute the intra-hour residual correction factor.

        Calculates the mean (actual / forecast) over the recent residuals
        buffer and linearly decays it to 1.0 over ``RESIDUAL_DECAY_SLOTS``.

        Args:
            slots_ahead: Number of slots into the future (0 = now).

        Returns:
            Residual correction factor, where 1.0 means no correction.
        """
        if not self._recent_residuals:
            return 1.0

        # Compute mean residual ratio.
        ratios = [
            actual / fcast
            for fcast, actual in self._recent_residuals
            if abs(fcast) >= _FORECAST_EPS
        ]
        if not ratios:
            return 1.0

        mean_residual = statistics.mean(ratios)
        # Clamp the mean residual to the same bounds as the hourly factor.
        mean_residual = max(FACTOR_MIN, min(FACTOR_MAX, mean_residual))

        # Linear decay: at slots_ahead=0, full correction; at slots_ahead≥DECAY_SLOTS, no correction.
        if slots_ahead >= RESIDUAL_DECAY_SLOTS:
            return 1.0

        decay = 1.0 - (slots_ahead / RESIDUAL_DECAY_SLOTS)
        return 1.0 + (mean_residual - 1.0) * decay


def _valid_energy_pair(
    forecast_kwh: Any,
    actual_kwh: Any,
) -> tuple[float, float] | None:
    """Return a finite, non-negative, bounded energy pair."""
    forecast = finite_float(
        forecast_kwh,
        minimum=0.0,
        maximum=_MAX_ENERGY_KWH,
    )
    actual = finite_float(
        actual_kwh,
        minimum=0.0,
        maximum=_MAX_ENERGY_KWH,
    )
    if forecast is None or actual is None:
        return None
    return forecast, actual


def _parse_hour_key(value: Any) -> int | None:
    """Parse one canonical JSON hour key."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        hour = value
    elif isinstance(value, str) and value.isdigit() and str(int(value)) == value:
        hour = int(value)
    else:
        return None
    return hour if 0 <= hour <= 23 else None


def _parse_energy_history(
    value: Any,
    *,
    maximum_length: int,
) -> list[tuple[float, float]] | None:
    """Parse a bounded ordered list of valid forecast/actual pairs."""
    if not isinstance(value, list) or len(value) > maximum_length:
        return None
    parsed: list[tuple[float, float]] = []
    for raw_pair in value:
        if not isinstance(raw_pair, (list, tuple)) or len(raw_pair) != 2:
            return None
        pair = _valid_energy_pair(raw_pair[0], raw_pair[1])
        if pair is None or pair[0] < _FORECAST_EPS:
            return None
        parsed.append(pair)
    return parsed


def _factor_from_history(history: list[tuple[float, float]]) -> float:
    """Recompute the exact bounded factor represented by a history buffer."""
    mean_ratio = statistics.mean(actual / forecast for forecast, actual in history)
    return max(FACTOR_MIN, min(FACTOR_MAX, mean_ratio))


def _parse_current_state(
    data: Mapping[str, Any],
    *,
    restored_at: datetime,
) -> (
    tuple[
        dict[int, float],
        dict[int, list[tuple[float, float]]],
        list[tuple[float, float]],
        float,
        datetime | None,
    ]
    | None
):
    """Parse v3 state atomically, rejecting partial or poisoned payloads."""
    required_keys = {
        "hour_factors",
        "hour_history",
        "recent_residuals",
        "confidence",
        "processed_through",
    }
    if not required_keys.issubset(data):
        return None

    raw_factors = data.get("hour_factors")
    raw_history = data.get("hour_history")
    raw_residuals = data.get("recent_residuals")
    if not isinstance(raw_factors, Mapping) or not isinstance(raw_history, Mapping):
        return None

    factors: dict[int, float] = {}
    for raw_hour, raw_factor in raw_factors.items():
        hour = _parse_hour_key(raw_hour)
        factor = finite_float(raw_factor, minimum=FACTOR_MIN, maximum=FACTOR_MAX)
        if hour is None or hour in factors or factor is None:
            return None
        factors[hour] = factor

    history_by_hour: dict[int, list[tuple[float, float]]] = {}
    for raw_hour, raw_samples in raw_history.items():
        hour = _parse_hour_key(raw_hour)
        samples = _parse_energy_history(
            raw_samples,
            maximum_length=MAX_HISTORY_PER_HOUR,
        )
        if hour is None or hour in history_by_hour or samples is None or not samples:
            return None
        history_by_hour[hour] = samples

    if factors.keys() != history_by_hour.keys():
        return None
    if any(
        not math.isclose(
            factors[hour],
            _factor_from_history(samples),
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
        for hour, samples in history_by_hour.items()
    ):
        return None

    residuals = _parse_energy_history(
        raw_residuals,
        maximum_length=MAX_RESIDUALS,
    )
    confidence = finite_float(
        data.get("confidence"),
        minimum=0.10,
        maximum=0.90,
    )
    if residuals is None or confidence is None:
        return None

    raw_processed_through = data.get("processed_through")
    if raw_processed_through is None:
        processed_through = None
    else:
        parsed_timestamp = aware_datetime_from_iso(raw_processed_through)
        if parsed_timestamp is None:
            return None
        processed_through = parsed_timestamp.astimezone(UTC).replace(microsecond=0)
        if processed_through > restored_at:
            return None

    return factors, history_by_hour, residuals, confidence, processed_through

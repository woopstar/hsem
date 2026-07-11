"""Per-hour solar forecast accuracy auto-correction.

The :class:`SolarForecastCorrector` maintains learned per-hour accuracy factors
and intra-hour residual corrections, inspired by Solar AI's approach.  It
corrects PV forecasts before they enter the planner engine.

This module has **no** Home Assistant dependencies and is fully testable with
plain ``pytest``.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from custom_components.hsem.utils.logger import log_planner

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Bounds for the per-hour accuracy factor (actual / forecast ratio).
FACTOR_MIN: float = 0.3
FACTOR_MAX: float = 1.5

# Maximum number of (forecast, actual) samples retained per hour.
MAX_HISTORY_PER_HOUR: int = 4

# Maximum number of recent closed-slot residuals retained.
MAX_RESIDUALS: int = 4

# Number of slots over which the intra-hour residual correction decays to 1.0.
RESIDUAL_DECAY_SLOTS: int = 8

# Confidence range.
CONFIDENCE_MIN: float = 0.10
CONFIDENCE_MAX: float = 0.90
CONFIDENCE_DEFAULT: float = 0.50

# Threshold below which forecast kWh is treated as zero to avoid division by zero.
_FORECAST_EPS: float = 1e-9


@dataclass
class SolarForecastCorrector:
    """Learns and applies per-hour PV forecast accuracy corrections.

    Two-layer correction:
    1. **Per-hour accuracy factor**: Rolling mean of (actual PV / forecast PV)
       over the last ~4 days per hour-of-day, clamped to [0.3, 1.5].
    2. **Intra-hour residual correction**: Mean of (actual / forecast) over the
       last 4 closed 15-min slots, linearly decayed to 1.0 over the next 2 hours
       (8 slots).

    Attributes:
        hour_factors: Per-hour accuracy factors keyed by hour (0-23).
            Defaults to 1.0 for all hours.
        confidence: Confidence percentile (0.10-0.90).  At 0.50 the learned
            factor is used as-is; lower values reduce the correction toward
            1.0 (more conservative PV estimate).
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

    # Confidence percentile (0.10-0.90, default 0.50).
    # At 0.50 the learned factor is applied fully; lower values push toward 1.0
    # (more conservative — less PV expected).
    confidence: float = CONFIDENCE_DEFAULT

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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

        if abs(forecast_kwh) < _FORECAST_EPS:
            log_planner(
                "debug",
                "[solar_corrector] update_hour(h=%d) skipped — forecast_kwh=%.4f near zero",
                hour,
                forecast_kwh,
            )
            return

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
        self._recent_residuals.append((forecast_kwh, actual_kwh))
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
        1. Per-hour accuracy factor (scaled by confidence percentile).
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

    def to_dict(self) -> dict:
        """Serialize the corrector state to a JSON-safe dictionary.

        Returns:
            A dictionary with hour_factors and confidence.
        """
        return {
            "hour_factors": dict(self.hour_factors),
            "confidence": self.confidence,
        }

    def load_from_dict(self, data: dict) -> None:
        """Restore corrector state from a previously-serialized dictionary.

        Args:
            data: A dictionary previously produced by :meth:`to_dict`.
        """
        if "hour_factors" in data:
            self.hour_factors = {
                int(k): float(v) for k, v in data["hour_factors"].items()
            }
        if "confidence" in data:
            self.confidence = float(data["confidence"])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_confidence(self, raw_factor: float) -> float:
        """Scale the per-hour factor by the confidence percentile.

        At ``confidence = 0.50`` the raw factor is applied at full strength.
        Below 0.50 the correction is dampened toward 1.0 (more conservative
        — less PV expected).  Above 0.50 the correction is still applied at
        full strength but never amplified beyond the raw factor.

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

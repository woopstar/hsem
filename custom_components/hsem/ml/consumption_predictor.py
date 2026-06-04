"""Consumption predictor — ridge regression with time-decay weights.

Predicts per-slot house consumption using NumPy-powered weighted ridge
regression on mixed categorical (DOW, slot) and continuous (day-of-year,
temperature) features.  L2 regularization naturally handles data sparsity.

Features (index order):
  0 .. 6*S-1    one-hot (DOW, slot)     — 672 for 15-min
  6*S, 6*S+1    sin/cos day-of-year      — seasonality
  6*S+2         temperature (optional)   — weather-driven load
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import override

import numpy as np


class ConsumptionPredictor:
    """Weighted ridge regression predictor for per-slot consumption.

    Fits coefficients for each (DOW, slot) pair plus continuous features
    for day-of-year seasonality and optional outdoor temperature.

    The temperature feature expects **outdoor (ambient) temperature in °C**.
    This helps the model predict weather-driven load:
    - Cold outdoor temps → more heating → higher consumption
    - Hot outdoor temps → more cooling → higher consumption
    Use an outdoor sensor (e.g. a weather station), not an indoor thermostat.

    Args:
        decay_days: Exponential time-decay half-life in days.
        alpha: L2 regularization strength.
        slots_per_day: Number of time slots per 24h day.
        retrain_min_new_samples: Minimum new samples before refitting.
        use_temperature: Whether to include temperature as a feature.
    """

    def __init__(
        self,
        decay_days: float = 7.0,
        alpha: float = 1.0,
        slots_per_day: int = 96,
        retrain_min_new_samples: int = 4,
        use_temperature: bool = False,
    ) -> None:
        self._decay_days = decay_days
        self._alpha = alpha
        self._slots_per_day = slots_per_day
        self._retrain_min_new = retrain_min_new_samples
        self._use_temperature = use_temperature

        # Feature layout:
        #   0 .. 6*S-1  = one-hot DOW×slot
        #   6*S, 6*S+1  = sin/cos day-of-year
        #   6*S+2       = temperature (if use_temperature)
        self._n_onehot = 7 * slots_per_day
        self._doy_offset = self._n_onehot
        self._temp_offset = self._n_onehot + 2
        self._n_features = self._temp_offset + (1 if use_temperature else 0)

        self._coef: np.ndarray | None = None
        self._intercept: float = 0.0

        self._X: np.ndarray | None = None
        self._y: np.ndarray | None = None
        self._w: np.ndarray | None = None

        # Raw per-group data for uncertainty estimation.
        # Maps (dow, slot) → list[(age_days, energy_kwh), ...]
        self._raw_groups: dict[tuple[int, int], list[tuple[float, float]]] = {}

        self._last_fit_samples: int = 0
        self._last_fit_time: datetime | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def train(
        self,
        history: list[tuple[datetime, int, float]],
        reference_time: datetime | None = None,
        temperatures: dict[datetime, float] | None = None,
    ) -> None:
        """Fit ridge regression on historical per-slot data.

        Args:
            history: List of ``(timestamp, slot_index, energy_kwh)``.
            reference_time: The "now" time for computing sample ages.
            temperatures: Optional dict mapping slot-start timestamps to
                temperature (°C) values.  Ignored when use_temperature=False.
        """
        if reference_time is None:
            reference_time = datetime.now().astimezone()

        n = len(history)
        if n < 2:
            self._coef = None
            return

        k = self._n_features
        X = np.zeros((n, k), dtype=np.float64)
        y = np.zeros(n, dtype=np.float64)
        w = np.zeros(n, dtype=np.float64)

        temps = temperatures or {}
        self._raw_groups.clear()

        valid = 0
        for ts, slot, energy in history:
            if slot < 0 or slot >= self._slots_per_day:
                continue
            if energy <= 0:
                continue

            ts_aware = ts if ts.tzinfo is not None else ts.astimezone()
            age_days = (reference_time - ts_aware).total_seconds() / 86400.0
            if age_days < 0:
                continue

            dow = ts_aware.weekday()
            doy = ts_aware.timetuple().tm_yday

            # Store raw data for uncertainty estimation.
            self._raw_groups.setdefault((dow, slot), []).append((age_days, energy))

            # One-hot (DOW, slot) feature.
            X[valid, dow * self._slots_per_day + slot] = 1.0

            # Day-of-year seasonality features.
            X[valid, self._doy_offset] = math.sin(2 * math.pi * doy / 365.0)
            X[valid, self._doy_offset + 1] = math.cos(2 * math.pi * doy / 365.0)

            # Temperature feature.
            if self._use_temperature:
                # Match temperature by slot-start timestamp (nearest).
                slot_start = ts_aware.replace(
                    minute=(ts_aware.minute // (1440 // self._slots_per_day))
                    * (1440 // self._slots_per_day),
                    second=0,
                    microsecond=0,
                )
                temp_val = self._lookup_temperature(temps, slot_start)
                X[valid, self._temp_offset] = temp_val

            y[valid] = energy
            w[valid] = math.exp(-age_days / max(self._decay_days, 0.5))
            valid += 1

        if valid < 2:
            self._coef = None
            return

        # Retrain gate.
        new_samples = valid - self._last_fit_samples
        if (
            self._coef is not None
            and self._last_fit_samples > 0
            and new_samples < self._retrain_min_new
        ):
            self._X = X[:valid]
            self._y = y[:valid]
            self._w = w[:valid]
            return

        X = X[:valid]
        y = y[:valid]
        w = w[:valid]

        self._X = X
        self._y = y
        self._w = w
        self._fit(X, y, w)

    def predict(
        self,
        slot: int,
        day_offset: int = 0,
        reference_time: datetime | None = None,
        temperature: float | None = None,
    ) -> float:
        """Predict consumption for a specific slot."""
        if self._coef is None:
            return 0.0

        if reference_time is None:
            reference_time = datetime.now().astimezone()

        target_dt = reference_time + timedelta(days=day_offset)
        target_dt = target_dt.replace(
            minute=(slot * (1440 // self._slots_per_day)) % 60,
            second=0,
            microsecond=0,
        )
        # Fix hour after minute wrap.
        hour = (slot * (1440 // self._slots_per_day)) // 60
        target_dt = target_dt.replace(hour=hour)

        return float(self._predict_from_features(target_dt, slot, temperature))

    def predict_with_std(
        self,
        slot: int,
        day_offset: int = 0,
        reference_time: datetime | None = None,
        temperature: float | None = None,
    ) -> tuple[float, float]:
        """Predict consumption with uncertainty.

        Returns:
            ``(mean_kwh, std_kwh)`` tuple.  ``std`` is the time-decay
            weighted standard deviation of the (DOW, slot) group.
            When the group has only 1 sample, std defaults to 20% of mean.
        """
        mean = self.predict(slot, day_offset, reference_time, temperature)
        if mean <= 0:
            return 0.0, 0.0

        if reference_time is None:
            reference_time = datetime.now().astimezone()

        target_date = reference_time.date() + timedelta(days=day_offset)
        dow = target_date.weekday()
        group = self._raw_groups.get((dow, slot), [])

        if len(group) < 2:
            return mean, mean * 0.2

        std = self._weighted_std(group)
        return mean, min(std, mean * 0.5)  # Cap std at 50% of mean

    def predict_all_slots(
        self,
        day_offset: int = 0,
        reference_time: datetime | None = None,
        temperatures: dict[int, float] | None = None,
    ) -> dict[int, float]:
        """Predict consumption for all slots of a given day."""
        if self._coef is None:
            return {}

        if reference_time is None:
            reference_time = datetime.now().astimezone()

        target_date = reference_time.date() + timedelta(days=day_offset)
        temps = temperatures or {}

        result: dict[int, float] = {}
        for s in range(self._slots_per_day):
            slot_dt = datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                tzinfo=reference_time.tzinfo,
            ) + timedelta(minutes=s * (1440 // self._slots_per_day))
            temp_val = temps.get(s)
            result[s] = float(self._predict_from_features(slot_dt, s, temp_val))
        return result

    # ------------------------------------------------------------------
    # Prediction helpers
    # ------------------------------------------------------------------

    def _predict_from_features(
        self,
        dt: datetime,
        slot: int,
        temperature: float | None,
    ) -> float:
        """Compute prediction from feature vector."""
        assert self._coef is not None, "_predict_from_features called before fit"
        dow = dt.weekday()
        doy = dt.timetuple().tm_yday

        pred = float(self._intercept)
        pred += float(self._coef[dow * self._slots_per_day + slot])
        pred += float(self._coef[self._doy_offset]) * math.sin(
            2 * math.pi * doy / 365.0
        )
        pred += float(self._coef[self._doy_offset + 1]) * math.cos(
            2 * math.pi * doy / 365.0
        )

        if self._use_temperature and temperature is not None:
            pred += float(self._coef[self._temp_offset]) * temperature

        return max(pred, 0.001)

    # ------------------------------------------------------------------
    # Fitting
    # ------------------------------------------------------------------

    def _fit(self, X: np.ndarray, y: np.ndarray, w: np.ndarray) -> None:
        """Solve weighted ridge regression: β = (XᵀWX + αI)⁻¹ XᵀWy."""
        k = X.shape[1]
        sqrt_w = np.sqrt(w)
        xw = X * sqrt_w[:, np.newaxis]
        yw = y * sqrt_w

        xtwx = xw.T @ xw
        ridge = xtwx + self._alpha * np.eye(k, dtype=np.float64)
        xtwy = xw.T @ yw

        try:
            coef = np.linalg.solve(ridge, xtwy)
        except np.linalg.LinAlgError:
            ridge += self._alpha * np.eye(k, dtype=np.float64)
            coef = np.linalg.solve(ridge, xtwy)

        self._intercept = 0.0
        self._coef = coef

        self._last_fit_samples = X.shape[0]
        self._last_fit_time = datetime.now().astimezone()

        # Clip negative one-hot coefficients; leave continuous features unclipped.
        floor = 0.001
        self._coef[: self._n_onehot] = np.maximum(self._coef[: self._n_onehot], floor)

    def _weighted_std(self, samples: list[tuple[float, float]]) -> float:
        """Compute time-decay weighted standard deviation."""
        if len(samples) < 2:
            return 0.0

        decay = max(self._decay_days, 0.5)
        weights = np.array([math.exp(-age / decay) for age, _ in samples])
        values = np.array([v for _, v in samples])
        w_sum = weights.sum()
        if w_sum <= 0:
            return 0.0

        w_mean = np.average(values, weights=weights)
        w_var = np.average((values - w_mean) ** 2, weights=weights)
        return float(np.sqrt(w_var))

    @staticmethod
    def _lookup_temperature(
        temperatures: dict[datetime, float],
        target: datetime,
    ) -> float:
        """Find the temperature closest to the target timestamp."""
        if not temperatures:
            return 0.0
        best = min(
            temperatures.keys(),
            key=lambda t: abs((t - target).total_seconds()),
            default=target,
        )
        return temperatures.get(best, 0.0)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def trained(self) -> bool:
        return self._coef is not None

    @property
    def group_count(self) -> int:
        if self._X is None:
            return 0
        return int(np.sum(np.max(self._X[:, : self._n_onehot], axis=0) > 0))

    @property
    def slots_per_day(self) -> int:
        return self._slots_per_day

    @property
    def last_fit_time(self) -> datetime | None:
        return self._last_fit_time

    @property
    def last_fit_samples(self) -> int:
        return self._last_fit_samples

    @property
    def alpha(self) -> float:
        return self._alpha

    @override
    def __repr__(self) -> str:
        return (
            f"ConsumptionPredictor(slots_per_day={self._slots_per_day}, "
            f"decay={self._decay_days}d, α={self._alpha}, "
            f"n_features={self._n_features}, trained={self._coef is not None})"
        )

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
        use_sequential: bool = False,
    ) -> None:
        self._decay_days = decay_days
        self._alpha = alpha
        self._slots_per_day = slots_per_day
        self._retrain_min_new = retrain_min_new_samples
        self._use_temperature = use_temperature
        self._use_sequential = use_sequential

        # Feature layout:
        #   0 .. 6*S-1  = one-hot DOW×slot
        #   6*S, 6*S+1  = sin/cos day-of-year
        #   6*S+2       = temperature (if use_temperature)
        #   6*S+3       = lag feature (prev slot energy, if use_sequential)
        self._n_onehot = 7 * slots_per_day
        self._doy_offset = self._n_onehot
        self._temp_offset = self._n_onehot + 2
        self._lag_offset = self._temp_offset + (1 if use_temperature else 0)
        self._n_features = self._lag_offset + (1 if use_sequential else 0)

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
        #: Actual calendar days spanned by the input history (set by populator).
        self.actual_history_days: float = 0.0

    @property
    def days_of_history(self) -> float:
        """Return the number of calendar days spanned by historical data.

        Computed from the oldest sample across all (DOW, slot) groups.
        Returns 0.0 when no history has been collected.
        """
        oldest_age = 0.0
        for entries in self._raw_groups.values():
            for age_days, _energy in entries:
                if age_days > oldest_age:
                    oldest_age = age_days
        return round(oldest_age, 1)

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

        # For sequential mode: track previous slot's energy as lag feature.
        prev_energy = 0.0

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

            # Lag feature: previous slot's energy.
            if self._use_sequential:
                X[valid, self._lag_offset] = prev_energy

            y[valid] = energy
            w[valid] = math.exp(-age_days / max(self._decay_days, 0.5))
            prev_energy = energy
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

    def predict_sequential(
        self,
        day_offset: int = 0,
        reference_time: datetime | None = None,
        temperatures: dict[int, float] | None = None,
    ) -> dict[int, float]:
        """Predict all slots sequentially, feeding each prediction as lag input.

        Slot 0 is predicted with prev_energy=0.  Slot 1 uses slot 0's
        prediction as its lag feature, and so on.  This captures intra-day
        momentum that independent per-slot predictions miss.
        """
        if self._coef is None:
            return {}

        if reference_time is None:
            reference_time = datetime.now().astimezone()

        target_date = reference_time.date() + timedelta(days=day_offset)
        temps = temperatures or {}
        prev = 0.0

        result: dict[int, float] = {}
        for s in range(self._slots_per_day):
            slot_dt = datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                tzinfo=reference_time.tzinfo,
            ) + timedelta(minutes=s * (1440 // self._slots_per_day))
            temp_val = temps.get(s)
            pred = float(self._predict_from_features(slot_dt, s, temp_val, prev))
            result[s] = pred
            prev = pred
        return result

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
        prev_energy: float = 0.0,
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

        if self._use_sequential:
            pred += float(self._coef[self._lag_offset]) * prev_energy

        return max(pred, 0.001)

    # ------------------------------------------------------------------
    # Fitting
    # ------------------------------------------------------------------

    def _fit(self, X: np.ndarray, y: np.ndarray, w: np.ndarray) -> None:
        """Two-stage (backfitting) weighted ridge regression.

        A joint ridge over 674 features with only a handful of samples is
        hopelessly under-determined: the day-of-year sin/cos columns soak
        up the variance and the one-hot (DOW, slot) coefficients collapse
        to the floor, destroying the per-slot signal (the whole point of
        the model).  We therefore fit in two stages:

        1. **Group means** — each (DOW, slot) one-hot coefficient is the
           time-decay weighted mean of its samples, shrunk toward the
           **slot-level weighted mean** (same slot across all weekdays)
           by ``alpha``.  The slot-level prior is the architecturally
           correct fallback: it preserves the per-slot signal for sparse
           (DOW, slot) groups while still letting recent observations
           pull stale groups toward current behaviour.
        2. **Continuous features** — day-of-year (and optional
           temperature/lag) coefficients are fitted by weighted ridge on
           the *residual* (y minus the group mean), so they only capture
           seasonality/weather effects the group means cannot explain.
        """
        n_samples = X.shape[0]

        # --- Stage 1: one-hot group means with slot-level shrinkage ----
        # Per-(DOW, slot) and per-slot weighted sums.
        group_w: dict[int, float] = {}
        group_wy: dict[int, float] = {}
        slot_w: dict[int, float] = {}
        slot_wy: dict[int, float] = {}
        onehot_cols = X[:, : self._n_onehot]
        for i in range(n_samples):
            g = int(np.argmax(onehot_cols[i]))  # one-hot index for this sample
            slot = g % self._slots_per_day
            wi = float(w[i])
            group_w[g] = group_w.get(g, 0.0) + wi
            group_wy[g] = group_wy.get(g, 0.0) + wi * float(y[i])
            slot_w[slot] = slot_w.get(slot, 0.0) + wi
            slot_wy[slot] = slot_wy.get(slot, 0.0) + wi * float(y[i])

        # Slot-level weighted mean = shrinkage prior.
        slot_mean: dict[int, float] = {
            s: slot_wy[s] / slot_w[s] for s in slot_w if slot_w[s] > 1e-12
        }

        coef = np.zeros(self._n_features, dtype=np.float64)
        floor = 0.001
        for g in range(self._n_onehot):
            wg = group_w.get(g, 0.0)
            if wg > 1e-12:
                gbar = group_wy[g] / wg
                prior = slot_mean.get(g % self._slots_per_day, gbar)
                # Shrink the group mean toward its slot-level mean.
                shrunk = (wg * gbar + self._alpha * prior) / (wg + self._alpha)
                coef[g] = max(shrunk, floor)
            else:
                coef[g] = floor

        # --- Stage 2: continuous features on the residual --------------
        resid = y - onehot_cols @ coef[: self._n_onehot]
        cont_cols = X[:, self._n_onehot :]
        k_cont = cont_cols.shape[1]
        if k_cont > 0:
            sqrt_w = np.sqrt(w)
            xw = cont_cols * sqrt_w[:, np.newaxis]
            yw = resid * sqrt_w
            ridge = xw.T @ xw + self._alpha * np.eye(k_cont, dtype=np.float64)
            xtwy = xw.T @ yw
            try:
                coef[self._n_onehot :] = np.linalg.solve(ridge, xtwy)
            except np.linalg.LinAlgError:
                ridge += self._alpha * np.eye(k_cont, dtype=np.float64)
                coef[self._n_onehot :] = np.linalg.solve(ridge, xtwy)

        self._intercept = 0.0
        self._coef = coef

        self._last_fit_samples = X.shape[0]
        self._last_fit_time = datetime.now().astimezone()

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

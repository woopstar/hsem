"""
Adaptive decay-based power consumption prediction (Phase 1 implementation).

This module provides an improved consumption prediction model using exponential
decay weighting, applicable as a drop-in enhancement to the current weighted
average approach in avg_sensor.py.

The key insight: exponential decay models real power consumption dynamics better
than fixed time windows. This principle is proven in advanced adaptive filtering
systems across many domains.

Author: GitHub Copilot
"""

import logging
import math
from datetime import date, timedelta

_LOGGER = logging.getLogger(__name__)


class AdaptiveConsumptionPredictor:
    """
    Adaptive power consumption prediction using exponential decay weighting.

    Instead of static weights (1d: 25%, 3d: 30%, 7d: 30%, 14d: 15%),
    this model learns that recent measurements should have more influence
    and uses an exponential decay to weight historical data.

    The decay time constant (tau) controls how quickly older measurements
    lose influence. This principle applies to any system with exponential
    dynamics, where recent behavior is more predictive than distant history.

    Principle:
        weight(t) = exp(-t / tau_days)

    For tau_days=7:
        - 0 days ago: weight=1.0 (100%)
        - 1 day ago: weight=0.87 (87%)
        - 3 days ago: weight=0.64 (64%)
        - 7 days ago: weight=0.37 (37%)
        - 14 days ago: weight=0.13 (13%)

    Benefits over fixed windows:
        1. Smoother transition (no cliff at window edge)
        2. Recent data naturally weighted more
        3. Single tunable parameter (tau) instead of 4 weights
        4. Physically motivated (matches real consumption dynamics)
    """

    def __init__(
        self,
        tau_days: float = 7.0,
        min_samples: int = 3,
        max_days_lookback: int = 60,
    ):
        """
        Initialize the adaptive consumption predictor.

        Parameters:
        -----------
        tau_days : float, default=7.0
            Decay time constant in days. Controls how quickly older
            measurements lose influence.
            - Smaller tau (3-5): fast response to changes, sensitive to noise
            - Larger tau (7-14): smooth predictions, slower adaptation
            Typical range: 5-10 days for household consumption.

        min_samples : int, default=3
            Minimum number of measurements required to make a prediction.
            Avoids predictions with very few data points.

        max_days_lookback : int, default=60
            Maximum number of days to consider. Older data is ignored,
            useful for ignoring seasonal anomalies or seasonal changes.
        """
        self.tau_days = tau_days
        self.min_samples = min_samples
        self.max_days_lookback = max_days_lookback
        self._last_prediction = None
        self._prediction_confidence = 0.0

    @property
    def last_prediction(self) -> float | None:
        """Return the last computed prediction."""
        return self._last_prediction

    @property
    def prediction_confidence(self) -> float:
        """
        Return prediction confidence [0.0, 1.0].

        Higher confidence indicates the prediction is based on:
        - More data points
        - More consistent data (less variance)
        - Longer observation history

        This aligns with RoomMind's confidence metric, which is based
        on covariance convergence.
        """
        return self._prediction_confidence

    def compute_weight(self, days_ago: float) -> float:
        """
        Compute exponential decay weight for a measurement.

        Parameters:
        -----------
        days_ago : float
            Time in days between measurement and today.

        Returns:
        --------
        float
            Weight in range [0, 1], where 1 is maximum influence.
        """
        if days_ago < 0:
            return 0.0
        return math.exp(-days_ago / self.tau_days)

    def predict(
        self,
        measurements: dict[date, float],
    ) -> float | None:
        """
        Predict consumption using weighted historical measurements.

        Parameters:
        -----------
        measurements : dict[date, float]
            Dictionary mapping measurement date to consumption in kWh.
            Example: {
                date(2024, 1, 15): 45.2,
                date(2024, 1, 14): 48.7,
                date(2024, 1, 13): 42.1,
            }

        Returns:
        --------
        float | None
            Predicted consumption in kWh, or None if insufficient data.
        """
        today = date.today()

        # Filter and validate measurements
        valid_measurements = []
        for measurement_date, consumption in measurements.items():
            if measurement_date > today:
                # Skip future measurements
                continue

            days_ago = (today - measurement_date).days
            if days_ago > self.max_days_lookback:
                # Skip very old data
                continue

            if consumption < 0:
                # Skip invalid (negative) consumption
                _LOGGER.warning(
                    "Skipping negative consumption measurement: %s kWh on %s",
                    consumption,
                    measurement_date,
                )
                continue

            valid_measurements.append((measurement_date, consumption))

        # Check minimum samples
        if len(valid_measurements) < self.min_samples:
            _LOGGER.debug(
                "Insufficient samples for prediction: %d < %d",
                len(valid_measurements),
                self.min_samples,
            )
            self._last_prediction = None
            self._prediction_confidence = 0.0
            return None

        # Compute weighted average
        total_weighted = 0.0
        total_weight = 0.0
        variance_sum = 0.0

        for measurement_date, consumption in valid_measurements:
            days_ago = (today - measurement_date).days
            weight = self.compute_weight(days_ago)
            total_weighted += consumption * weight
            total_weight += weight

        if total_weight < 1e-6:
            _LOGGER.debug("Total weight too small: %f", total_weight)
            self._last_prediction = None
            self._prediction_confidence = 0.0
            return None

        prediction = total_weighted / total_weight

        # Compute confidence based on data characteristics
        # (inspired by RoomMind's confidence metric)
        mean_consumption = prediction
        for measurement_date, consumption in valid_measurements:
            days_ago = (today - measurement_date).days
            weight = self.compute_weight(days_ago)
            weighted_variance = weight * (consumption - mean_consumption) ** 2
            variance_sum += weighted_variance

        weighted_variance = variance_sum / total_weight
        std_dev = math.sqrt(weighted_variance) if weighted_variance > 0 else 0.0

        # Confidence: high when data is consistent (low std_dev)
        # and we have many samples
        # Similar to RoomMind: confidence = 1 - (std/mean)^0.5
        if prediction > 0:
            cv = std_dev / prediction  # Coefficient of variation
        else:
            cv = 0.0

        num_samples_factor = min(len(valid_measurements) / 20.0, 1.0)  # Scale 0-20
        consistency_factor = max(0.0, 1.0 - cv)  # Higher when consistent
        self._prediction_confidence = 0.7 * consistency_factor + 0.3 * num_samples_factor

        self._last_prediction = prediction
        return prediction

    def predict_with_components(
        self,
        measurements: dict[date, float],
        comparison_weights: dict[str, float] | None = None,
    ) -> dict:
        """
        Predict consumption and return detailed analysis.

        Useful for understanding how the model's prediction compares
        to traditional fixed-weight averaging.

        Parameters:
        -----------
        measurements : dict[date, float]
            Historical measurements.
        comparison_weights : dict[str, float], optional
            Traditional weights for comparison (e.g., from current HSEM config).
            Expected keys: "1d", "3d", "7d", "14d"

        Returns:
        --------
        dict
            Detailed breakdown:
            {
                'prediction': float,           # Main prediction
                'confidence': float,           # [0, 1] confidence score
                'num_samples': int,            # Number of measurements used
                'time_range_days': int,        # Range from oldest to newest
                'comparison': {                # Comparison to traditional method
                    'traditional_prediction': float,
                    'difference_pct': float,
                }
            }
        """
        today = date.today()

        # Get adaptive prediction
        adaptive_pred = self.predict(measurements)
        if adaptive_pred is None:
            return {
                'prediction': None,
                'confidence': 0.0,
                'error': 'Insufficient data',
            }

        # Compute comparison with traditional fixed weights
        comparison_result = {}
        if comparison_weights:
            traditional_pred = self._compute_traditional_weighted_average(
                measurements,
                comparison_weights,
            )
            if traditional_pred is not None:
                diff_pct = (
                    (adaptive_pred - traditional_pred) / traditional_pred * 100
                    if traditional_pred > 0
                    else 0
                )
                comparison_result = {
                    'traditional_prediction': traditional_pred,
                    'difference_pct': diff_pct,
                }

        # Compute sample statistics
        valid_measurements = [
            (measurement_date, consumption)
            for measurement_date, consumption in measurements.items()
            if measurement_date <= today
            and (today - measurement_date).days <= self.max_days_lookback
            and consumption >= 0
        ]

        if valid_measurements:
            dates = [md for md, _ in valid_measurements]
            time_range = (max(dates) - min(dates)).days if len(dates) > 1 else 0
        else:
            time_range = 0

        return {
            'prediction': adaptive_pred,
            'confidence': self._prediction_confidence,
            'num_samples': len(valid_measurements),
            'time_range_days': time_range,
            'comparison': comparison_result,
            'tau_days': self.tau_days,
        }

    @staticmethod
    def _compute_traditional_weighted_average(
        measurements: dict[date, float],
        weights: dict[str, float],
    ) -> float | None:
        """
        Compute consumption using traditional fixed-window weights.

        Parameters:
        -----------
        measurements : dict[date, float]
            Historical measurements.
        weights : dict[str, float]
            Window weights: {"1d": 0.25, "3d": 0.30, "7d": 0.30, "14d": 0.15}

        Returns:
        --------
        float | None
            Weighted average, or None if windows lack data.
        """
        today = date.today()
        windows = {
            '1d': 1,
            '3d': 3,
            '7d': 7,
            '14d': 14,
        }

        window_values = {}
        for window_name, window_days in windows.items():
            window_start = today - timedelta(days=window_days)
            window_start_excl = window_start + timedelta(days=1)

            window_measurements = [
                consumption
                for mdate, consumption in measurements.items()
                if window_start_excl <= mdate <= today and consumption >= 0
            ]

            if window_measurements:
                window_values[window_name] = sum(window_measurements) / len(
                    window_measurements
                )

        # Compute weighted average
        if not window_values:
            return None

        total_weighted = sum(
            window_values.get(window, 0) * weights.get(window, 0)
            for window in weights.keys()
        )
        total_weight = sum(
            weights.get(window, 0) for window in window_values.keys()
        )

        return total_weighted / total_weight if total_weight > 0 else None

    def compute_optimal_tau(
        self,
        historical_measurements: dict[date, float],
        actual_next_value: float,
    ) -> float:
        """
        Find optimal tau by testing multiple values.

        Use this offline to tune tau_days for your specific consumption pattern.

        Parameters:
        -----------
        historical_measurements : dict[date, float]
            Training data (exclude the actual_next_value date).
        actual_next_value : float
            Actual consumption that occurred after the history.

        Returns:
        --------
        float
            Optimal tau_days value that minimizes prediction error.
        """
        tau_candidates = [2.0, 3.0, 4.0, 5.0, 7.0, 10.0, 14.0, 21.0]
        best_tau = 7.0
        best_error = float('inf')

        for tau in tau_candidates:
            self.tau_days = tau
            prediction = self.predict(historical_measurements)
            if prediction is None:
                continue

            error = abs(prediction - actual_next_value)
            if error < best_error:
                best_error = error
                best_tau = tau

        self.tau_days = best_tau
        return best_tau


# Example usage and validation
if __name__ == '__main__':
    """
    Example: Compare adaptive decay model vs. traditional fixed weights.
    """
    import random

    # Simulate 30 days of consumption data
    random.seed(42)
    base_consumption = 45.0  # kWh
    measurements = {}
    for i in range(30):
        measurement_date = date.today() - timedelta(days=i)
        # Add realistic variation: daily changes + weekly pattern
        daily_variation = 8.0 * math.sin(i * 2 * math.pi / 7)  # 7-day cycle
        noise = random.gauss(0, 2.0)  # Gaussian noise
        consumption = base_consumption + daily_variation + noise
        measurements[measurement_date] = max(5.0, consumption)  # No negative

    # Current HSEM weights
    current_weights = {
        '1d': 0.25,
        '3d': 0.30,
        '7d': 0.30,
        '14d': 0.15,
    }

    # Test different tau values
    print("Comparison of Adaptive vs. Traditional Weighting")
    print("=" * 70)
    print(f"{'tau (days)':<12} {'Prediction':<15} {'Confidence':<15} vs Current")
    print("-" * 70)

    for tau in [3.0, 5.0, 7.0, 10.0, 14.0]:
        predictor = AdaptiveConsumptionPredictor(tau_days=tau)
        result = predictor.predict_with_components(
            measurements,
            comparison_weights=current_weights,
        )

        if result['prediction'] is not None:
            diff_pct = result['comparison'].get('difference_pct', 0)
            print(
                f"{tau:<12.1f} {result['prediction']:<15.2f} "
                f"{result['confidence']:<15.2f} {diff_pct:+.1f}%"
            )

    print("\nNote: Optimal tau typically 5-10 days for household consumption.")
    print("Tune tau based on your specific consumption pattern and variability.")

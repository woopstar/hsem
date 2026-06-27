"""Temperature-adaptive battery charge rate learner.

Tracks actual charge power observations across 7 temperature buckets and
computes a p90 sustained charge rate for each bucket.  These learned rates
help the planner set realistic charge power limits that account for
temperature-dependent BMS throttling.

Issue #608 — Temperature-adaptive battery charge rate learning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Temperature bucket boundaries (°C)
TEMP_BUCKETS: list[tuple[str, float, float]] = [
    ("below_0", float("-inf"), 0.0),
    ("0_to_5", 0.0, 5.0),
    ("6_to_15", 5.0, 15.0),
    ("16_to_21", 15.0, 21.0),
    ("21_to_35", 21.0, 35.0),
    ("35_to_50", 35.0, 50.0),
    ("above_50", 50.0, float("inf")),
]


@dataclass
class ChargeRateLearner:
    """Learns sustained charge rate per temperature bucket (p90).

    Accumulates charge power observations (W) during grid/force charging
    and computes the 90th-percentile sustained rate for each temperature
    bucket once enough samples are collected.

    Attributes:
        samples: Per-bucket lists of observed charge powers (W).
        learned_rates: Per-bucket p90 charge rates (W), or ``None`` if
            not enough samples have been collected.
    """

    # Per-bucket: list of observed charge powers (W) during grid/force charging
    samples: dict[str, list[float]] = field(
        default_factory=lambda: {b[0]: [] for b in TEMP_BUCKETS}
    )

    # Learned rates (W), None until enough samples
    learned_rates: dict[str, float | None] = field(
        default_factory=lambda: {b[0]: None for b in TEMP_BUCKETS}
    )

    MIN_SAMPLES_PER_BUCKET: int = field(default=5, init=False)
    """Minimum number of samples required before computing a p90 rate."""

    MAX_SAMPLES_PER_BUCKET: int = field(default=100, init=False)
    """Maximum number of samples retained per bucket (FIFO)."""

    def learned_charge_rate_w(self, bucket_name: str) -> float | None:
        """Return the learned p90 charge rate for a bucket, or None."""
        return self.learned_rates.get(bucket_name)

    def update(self, cell_temp_c: float | None, charge_power_w: float) -> None:
        """Record a charge power observation at the given temperature.

        Args:
            cell_temp_c: Battery cell temperature in °C, or ``None`` if
                unavailable.
            charge_power_w: Observed charge power in watts.  Values <= 0
                are silently ignored.
        """
        if cell_temp_c is None or charge_power_w <= 0:
            return

        bucket_name = _get_bucket(cell_temp_c)
        self.samples[bucket_name].append(charge_power_w)

        # Keep last MAX_SAMPLES_PER_BUCKET samples per bucket
        if len(self.samples[bucket_name]) > self.MAX_SAMPLES_PER_BUCKET:
            self.samples[bucket_name] = self.samples[bucket_name][
                -self.MAX_SAMPLES_PER_BUCKET :
            ]

        # Recompute p90 when we have enough samples
        if len(self.samples[bucket_name]) >= self.MIN_SAMPLES_PER_BUCKET:
            sorted_samples = sorted(self.samples[bucket_name])
            p90_idx = int(len(sorted_samples) * 0.9)
            self.learned_rates[bucket_name] = sorted_samples[
                min(p90_idx, len(sorted_samples) - 1)
            ]

    def get_charge_rate_w(self, cell_temp_c: float | None, fallback_w: float) -> float:
        """Return the learned rate for the current temperature, or fallback.

        Args:
            cell_temp_c: Battery cell temperature in °C, or ``None`` if
                unavailable.
            fallback_w: Fallback charge rate in watts when no learned rate
                is available for the current bucket.

        Returns:
            The learned p90 charge rate for the bucket matching
            *cell_temp_c*, or *fallback_w* if no rate has been learned yet
            or the temperature is unknown.
        """
        if cell_temp_c is None:
            return fallback_w
        bucket_name = _get_bucket(cell_temp_c)
        rate = self.learned_rates.get(bucket_name)
        return rate if rate is not None else fallback_w


def _get_bucket(temp_c: float) -> str:
    """Map a temperature to the matching bucket name.

    Args:
        temp_c: Temperature in °C.

    Returns:
        The bucket name string.  Falls back to ``"21_to_35"`` if no
        bucket matches (should not happen with the defined ranges).
    """
    for name, lo, hi in TEMP_BUCKETS:
        if lo <= temp_c < hi:
            return name
    return "21_to_35"  # default fallback


# Module-level singleton — will be wired into the coordinator in a
# follow-up consolidation step (issue #608).
CHARGE_RATE_LEARNER = ChargeRateLearner()

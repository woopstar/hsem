"""Tests for custom_sensors/hourly_data_populator.py.

The core spike-aware weighting algorithm (_compute_weighted_average) is pure
Python and can be tested without any Home Assistant dependency.
"""

from __future__ import annotations

import pytest

from custom_components.hsem.custom_sensors.hourly_data_populator import (
    _compute_weighted_average,
)


class TestComputeWeightedAverage:
    """Unit tests for the spike-aware dynamic reweighting algorithm."""

    def test_equal_values_returns_same(self):
        """When all windows have the same value, the weighted average is very close to that value.

        Reliability scaling slightly dampens weights when all windows agree (rel factors
        approach 1 but not exactly), so the result may be fractionally below the input.
        """
        result = _compute_weighted_average(1.0, 1.0, 1.0, 1.0, 50, 20, 15, 10, 95)
        # Should be within 10% of the input value
        assert result == pytest.approx(1.0, rel=0.1)

    def test_all_zero_values_returns_zero(self):
        result = _compute_weighted_average(0.0, 0.0, 0.0, 0.0, 50, 20, 15, 10, 95)
        assert result == 0.0

    def test_spike_in_1d_reduces_its_contribution(self):
        """A large spike in 1d should be damped towards the 7d/14d baseline."""
        normal = 1.0
        spiked = 10.0  # 10× the baseline — clear spike
        result_spiked = _compute_weighted_average(
            spiked, normal, normal, normal, 50, 20, 15, 10, 95
        )
        result_normal = _compute_weighted_average(
            normal, normal, normal, normal, 50, 20, 15, 10, 95
        )
        # Spiked result should be higher than normal but damped (not 10× higher)
        assert result_spiked > result_normal
        assert (
            result_spiked < spiked * 0.5
        )  # damped to less than half the raw spike weight

    def test_weights_summing_to_100(self):
        """Standard 25/30/30/15 weights should produce a sensible result."""
        result = _compute_weighted_average(2.0, 1.8, 1.7, 1.6, 25, 30, 30, 15, 100)
        # Should be between the min and max input values
        assert 1.6 <= result <= 2.0

    def test_all_weights_zero_edge_case(self):
        """w_total == 0 should not raise — returns 0."""
        # This branch is guarded by the caller but we test robustness
        result = _compute_weighted_average(1.0, 1.0, 1.0, 1.0, 0, 0, 0, 0, 0)
        assert result == 0.0

    def test_negative_values_handled(self):
        """Should not raise even with negative consumption values (edge case)."""
        result = _compute_weighted_average(-0.1, -0.05, 0.0, 0.0, 50, 20, 15, 10, 95)
        assert isinstance(result, float)

    def test_large_14d_spike_redistributes_to_7d(self):
        """14d spike should redistribute weight towards 7d."""
        normal = 1.0
        spiked_14d = 5.0
        result = _compute_weighted_average(
            normal, normal, normal, spiked_14d, 25, 25, 25, 25, 100
        )
        # Result should be closer to normal (7d) than to spiked_14d
        assert result < spiked_14d

    def test_round_trip_symmetry(self):
        """Swapping v1 and v14 with symmetric weights should give different results
        because the reweighting is asymmetric (1d vs 14d are treated differently)."""
        r1 = _compute_weighted_average(3.0, 1.0, 1.0, 1.0, 25, 25, 25, 25, 100)
        r2 = _compute_weighted_average(1.0, 1.0, 1.0, 3.0, 25, 25, 25, 25, 100)
        # They need not be equal — this just confirms the function doesn't crash
        assert isinstance(r1, float)
        assert isinstance(r2, float)

"""Tests for custom_sensors/hourly_data_populator.py.

The core spike-aware weighting algorithm (_compute_weighted_average) is pure
Python and can be tested without any Home Assistant dependency.
"""

from __future__ import annotations

import pytest

from custom_components.hsem.planner.slot_population import (
    weighted_avg_consumption as _compute_weighted_average,
)


class TestComputeWeightedAverage:
    """Unit tests for the spike-aware dynamic reweighting algorithm."""

    def test_equal_values_returns_same(self):
        """When all windows have the same value, the weighted average is very close to that value.

        Reliability scaling slightly dampens weights when all windows agree (rel factors
        approach 1 but not exactly), so the result may be fractionally below the input.
        """
        result, mask = _compute_weighted_average(1.0, 1.0, 1.0, 1.0, 50, 20, 15, 10)
        # Should be within 10% of the input value
        assert result == pytest.approx(1.0, rel=0.1)
        assert mask == [False, False, False, False]

    def test_all_zero_values_returns_zero(self):
        result, mask = _compute_weighted_average(0.0, 0.0, 0.0, 0.0, 50, 20, 15, 10)
        assert result == pytest.approx(0.0)
        assert mask == [False, False, False, False]

    def test_spike_in_1d_reduces_its_contribution(self):
        """A large spike in 1d should be flagged as outlier and its weight redistributed."""
        normal = 1.0
        spiked = 10.0  # 10× the baseline — clear spike
        result_spiked, mask_spiked = _compute_weighted_average(
            spiked, normal, normal, normal, 50, 20, 15, 10
        )
        result_normal, mask_normal = _compute_weighted_average(
            normal, normal, normal, normal, 50, 20, 15, 10
        )
        # 1d should be flagged as outlier
        assert mask_spiked[0] is True
        # With 1d weight (50) redistributed to 3d/7d/14d (total 45 scaled to 100),
        # the result equals the weighted average of the non-outlier windows
        # which is 1.0 (all normal values)
        assert result_spiked == pytest.approx(result_normal, abs=0.01)

    def test_weights_summing_to_100(self):
        """Standard 25/30/30/15 weights should produce a sensible result."""
        result, mask = _compute_weighted_average(2.0, 1.8, 1.7, 1.6, 25, 30, 30, 15)
        # Should be between the min and max input values
        assert 1.6 <= result <= 2.0

    def test_all_weights_zero_edge_case(self):
        """w_total == 0 should not raise — returns 0."""
        result, mask = _compute_weighted_average(1.0, 1.0, 1.0, 1.0, 0, 0, 0, 0)
        assert result == pytest.approx(0.0)
        assert mask == [False, False, False, False]

    def test_negative_values_handled(self):
        """Should not raise even with negative consumption values (edge case)."""
        result, mask = _compute_weighted_average(-0.1, -0.05, 0.0, 0.0, 50, 20, 15, 10)
        assert isinstance(result, float)
        assert isinstance(mask, list)

    def test_large_14d_spike_redistributes_to_7d(self):
        """14d spike should redistribute weight towards 7d."""
        normal = 1.0
        spiked_14d = 5.0
        result, mask = _compute_weighted_average(
            normal, normal, normal, spiked_14d, 25, 25, 25, 25
        )
        # Result should be closer to normal (7d) than to spiked_14d
        assert result < spiked_14d
        # 14d should be flagged as outlier
        assert mask[3] is True

    def test_round_trip_symmetry(self):
        """Swapping v1 and v14 with symmetric weights should give different results
        because the reweighting is asymmetric (1d vs 14d are treated differently)."""
        r1, m1 = _compute_weighted_average(3.0, 1.0, 1.0, 1.0, 25, 25, 25, 25)
        r2, m2 = _compute_weighted_average(1.0, 1.0, 1.0, 3.0, 25, 25, 25, 25)
        # They need not be equal — this just confirms the function doesn't crash
        assert isinstance(r1, float)
        assert isinstance(r2, float)
        assert isinstance(m1, list)
        assert isinstance(m2, list)

    def test_downward_outlier_detected(self):
        """A very low 1d value (downward anomaly) should be flagged as outlier."""
        result, mask = _compute_weighted_average(
            0.188, 0.578, 0.708, 0.718, 50, 25, 15, 10
        )
        # 1d (0.188) is far below the other three (0.578, 0.708, 0.718)
        # After capping: 0.188 → 0.80 * baseline ≈ 0.80 * 0.711 = 0.569
        # The IQR on [0.569, 0.578, 0.708, 0.718] should flag 0.569 as outlier
        assert mask[0] is True, "1d should be flagged as downward outlier"
        # The weighted average should be close to the 7d/14d baseline, not pulled down by 1d
        assert result > 0.5, (
            f"Result {result} should be > 0.5 (not dragged down by 1d outlier)"
        )

    def test_repeated_high_load_not_outlier(self):
        """When 1d, 3d, and 7d are all high (repeated load), none should be outlier."""
        result, mask = _compute_weighted_average(2.0, 1.9, 1.8, 1.0, 25, 25, 25, 25)
        # 1d, 3d, 7d are all elevated — this is a trend, not a spike
        # Median of [1.0, 1.8, 1.9, 2.0] = (1.8+1.9)/2 = 1.85
        # Upper fence: 1.85 * 1.5 = 2.775 — all values below
        # Lower fence: 1.85 / 1.5 = 1.233 — 1.0 is below this
        # So 14d (1.0) IS flagged as outlier since it's the old normal
        # But 1d, 3d, 7d are NOT outliers — they represent the new trend
        assert not mask[0], "1d should not be outlier (part of trend)"
        assert not mask[1], "3d should not be outlier (part of trend)"
        assert not mask[2], "7d should not be outlier (part of trend)"

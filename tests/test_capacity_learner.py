"""Tests for the CapacityLearner — battery usable capacity auto-detection."""

from __future__ import annotations

import pytest

from custom_components.hsem.utils.capacity_learner import CapacityLearner


class TestCapacityLearner:
    """Test the CapacityLearner sample accumulation and median logic."""

    # ------------------------------------------------------------------
    # Sample accumulation — mid-range SoC only
    # ------------------------------------------------------------------

    def test_sample_accumulation_in_mid_range(self):
        """Samples should be accumulated when SoC moves within 15-85 %."""
        learner = CapacityLearner()

        # Seed with a reading at the lower boundary.
        learner.update(3.0, 15.0)  # 3 kWh @ 15 % SoC

        # Now move to 30 % SoC with a different kWh reading.
        learner.update(6.0, 30.0)  # delta_soc=15%, delta_kwh=3

        # capacity = 3 / (15/100) = 20 kWh
        assert learner.sample_count == 1
        assert learner.samples[0] == pytest.approx(20.0, rel=1e-6)

    def test_samples_outside_mid_range_not_accumulated(self):
        """Readings at 10 % SoC and 90 % SoC should NOT trigger sample accumulation."""
        learner = CapacityLearner()

        learner.update(1.0, 10.0)  # Below MIN_SOC
        learner.update(9.0, 90.0)  # Above MAX_SOC
        # Should still have 0 samples (both readings outside mid-range).
        assert learner.sample_count == 0

        # Move further within the non-mid-range
        learner.update(1.5, 12.0)
        learner.update(8.5, 88.0)
        # Still outside mid-range → no samples.
        assert learner.sample_count == 0

    def test_samples_outside_range_stored_as_reference(self):
        """Outside readings should be stored and used when re-entering mid-range."""
        learner = CapacityLearner()

        # Start at 90 % (outside)
        learner.update(9.0, 90.0)
        assert learner.sample_count == 0

        # Now enter mid-range at 80 % with 8 kWh
        learner.update(8.0, 80.0)
        # delta_soc = |80 - 90| = 10 %, delta_kwh = |8 - 9| = 1 kWh
        # capacity = 1 / (10/100) = 10 kWh
        assert learner.sample_count == 1
        assert learner.samples[0] == pytest.approx(10.0, rel=1e-6)

    # ------------------------------------------------------------------
    # Small delta filtering
    # ------------------------------------------------------------------

    def test_very_small_soc_delta_does_not_produce_sample(self):
        """A SoC change < 0.5 % between readings should NOT accumulate a sample."""
        learner = CapacityLearner()

        learner.update(5.0, 50.0)
        learner.update(5.05, 50.4)  # delta_soc = 0.4 %  →  ignored
        assert learner.sample_count == 0

    def test_very_small_kwh_delta_does_not_produce_sample(self):
        """A kWh change < 0.01 should NOT accumulate a sample."""
        learner = CapacityLearner()

        learner.update(5.0, 50.0)
        learner.update(5.005, 60.0)  # delta_kwh = 0.005 → ignored
        assert learner.sample_count == 0

    # ------------------------------------------------------------------
    # learned_capacity before MIN_SAMPLES
    # ------------------------------------------------------------------

    def test_learned_capacity_returns_none_before_min_samples(self):
        """Should return None until at least MIN_SAMPLES are collected."""
        learner = CapacityLearner()
        for i in range(learner.MIN_SAMPLES - 1):
            learner.update(float(i + 2), 20.0 + float(i))
            learner.update(float(i + 2) + 1.0, 30.0 + float(i))
        assert learner.sample_count == learner.MIN_SAMPLES - 1
        assert learner.learned_capacity_kwh is None

    # ------------------------------------------------------------------
    # Median computation
    # ------------------------------------------------------------------

    def test_learned_capacity_returns_median(self):
        """With enough samples, learned_capacity_kwh should return the median."""
        learner = CapacityLearner()

        # Collect enough samples — each update() pair produces one sample,
        # but overlapping calls between iterations also produce samples.
        odd_sample_count = learner.MIN_SAMPLES + 1  # 21 → odd
        for i in range(odd_sample_count):
            learner.update(float(i + 1), 50.0)
            learner.update(float(i + 1) + (i + 1) * 0.5, 70.0)

        # More samples accumulate than iterations due to overlap between
        # successive update() pairs.  Verify we have at least MIN_SAMPLES.
        assert learner.sample_count >= learner.MIN_SAMPLES
        result = learner.learned_capacity_kwh
        assert result is not None
        # Median of sorted list should be middle element.
        sorted_samples = sorted(learner.samples)
        expected_median = sorted_samples[len(sorted_samples) // 2]
        assert result == pytest.approx(expected_median, rel=1e-6)

    def test_median_with_even_sample_count(self):
        """Even count of samples should return the lower-mid element."""
        learner = CapacityLearner()
        # Override MIN_SAMPLES threshold by lowering it so 4 samples suffice.
        object.__setattr__(learner, "MIN_SAMPLES", 4)
        learner.samples = [10.0, 20.0, 30.0, 40.0]  # 4 samples
        result = learner.learned_capacity_kwh
        # With 4 samples, mid = 2, sorted[2] = 30
        assert result == pytest.approx(30.0, rel=1e-6)

    # ------------------------------------------------------------------
    # Outlier clamping
    # ------------------------------------------------------------------

    def test_capacity_below_1_kwh_rejected(self):
        """Capacity samples < 1 kWh should be discarded."""
        learner = CapacityLearner()

        learner.update(0.1, 50.0)
        learner.update(0.15, 70.0)  # delta_kwh=0.05, delta_soc=20% → cap=0.25 kWh
        assert learner.sample_count == 0

    def test_capacity_above_100_kwh_rejected(self):
        """Capacity samples > 100 kWh should be discarded."""
        learner = CapacityLearner()

        learner.update(100.0, 20.0)
        learner.update(200.0, 30.0)  # delta_kwh=100, delta_soc=10% → cap=1000 kWh
        assert learner.sample_count == 0

    # ------------------------------------------------------------------
    # sample_count property
    # ------------------------------------------------------------------

    def test_sample_count(self):
        """sample_count should return the number of accumulated samples."""
        learner = CapacityLearner()
        assert learner.sample_count == 0

        learner.samples = [10.0, 20.0]
        assert learner.sample_count == 2

    # ------------------------------------------------------------------
    # FIFO retention
    # ------------------------------------------------------------------

    def test_samples_capped_at_max_samples(self):
        """Sample list should be pruned to MAX_SAMPLES (200) when exceeded."""
        learner = CapacityLearner()

        # Add 201 samples directly, then trigger the cap via update.
        learner.samples = [float(i) for i in range(201)]

        # Add one more via update to trigger the cap logic.
        learner.update(20.0, 50.0)
        learner.update(30.0, 70.0)  # delta_soc=20%, delta_kwh=10 → cap=50

        assert len(learner.samples) <= learner.MAX_SAMPLES

    # ------------------------------------------------------------------
    # None handling
    # ------------------------------------------------------------------

    def test_update_with_none_kwh_does_nothing(self):
        """None kWh should be silently ignored."""
        learner = CapacityLearner()
        learner.update(None, 50.0)
        assert learner.sample_count == 0
        assert learner._last_kwh is None
        assert learner._last_soc is None

    def test_update_with_none_soc_does_nothing(self):
        """None SoC should be silently ignored."""
        learner = CapacityLearner()
        learner.update(5.0, None)
        assert learner.sample_count == 0

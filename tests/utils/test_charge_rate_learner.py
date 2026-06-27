"""Tests for the ChargeRateLearner — temperature-bucketed p90 charge rate learning.

Issue #608 — Temperature-adaptive battery charge rate learning.
"""

from __future__ import annotations

import pytest

from custom_components.hsem.utils.charge_rate_learner import (
    ChargeRateLearner,
    _get_bucket,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_learner() -> ChargeRateLearner:
    """Return a new ChargeRateLearner with no accumulated state."""
    return ChargeRateLearner()


# ---------------------------------------------------------------------------
# Bucket selection tests
# ---------------------------------------------------------------------------


class TestBucketSelection:
    """Test that temperatures are mapped to the correct bucket."""

    def test_below_0(self) -> None:
        """Negative Celsius maps to below_0."""
        assert _get_bucket(-5.0) == "below_0"
        assert _get_bucket(-0.1) == "below_0"

    def test_boundary_0_degrees(self) -> None:
        """Exactly 0 °C falls into the 0_to_5 bucket (0 <= temp < 5)."""
        assert _get_bucket(0.0) == "0_to_5"

    def test_0_to_5(self) -> None:
        """Temperatures 0 <= t < 5 map to 0_to_5."""
        assert _get_bucket(0.0) == "0_to_5"
        assert _get_bucket(2.5) == "0_to_5"
        assert _get_bucket(4.9) == "0_to_5"

    def test_6_to_15(self) -> None:
        """Temperatures 5 <= t < 15 map to 6_to_15."""
        assert _get_bucket(5.0) == "6_to_15"
        assert _get_bucket(10.0) == "6_to_15"
        assert _get_bucket(14.9) == "6_to_15"

    def test_16_to_21(self) -> None:
        """Temperatures 15 <= t < 21 map to 16_to_21."""
        assert _get_bucket(15.0) == "16_to_21"
        assert _get_bucket(18.0) == "16_to_21"
        assert _get_bucket(20.9) == "16_to_21"

    def test_21_to_35(self) -> None:
        """Temperatures 21 <= t < 35 map to 21_to_35."""
        assert _get_bucket(21.0) == "21_to_35"
        assert _get_bucket(28.0) == "21_to_35"
        assert _get_bucket(34.9) == "21_to_35"

    def test_35_to_50(self) -> None:
        """Temperatures 35 <= t < 50 map to 35_to_50."""
        assert _get_bucket(35.0) == "35_to_50"
        assert _get_bucket(42.0) == "35_to_50"
        assert _get_bucket(49.9) == "35_to_50"

    def test_above_50(self) -> None:
        """Temperatures >= 50 map to above_50."""
        assert _get_bucket(50.0) == "above_50"
        assert _get_bucket(60.0) == "above_50"


# ---------------------------------------------------------------------------
# p90 computation tests
# ---------------------------------------------------------------------------


class TestP90Computation:
    """Test the p90 charge rate computation logic."""

    def test_p90_not_computed_before_min_samples(self) -> None:
        """p90 is None until at least MIN_SAMPLES_PER_BUCKET samples."""
        learner = _fresh_learner()
        for i in range(learner.MIN_SAMPLES_PER_BUCKET - 1):
            learner.update(25.0, 2000.0 + float(i))
        assert learner.learned_rates["21_to_35"] is None

    def test_p90_computed_at_min_samples(self) -> None:
        """p90 is set once MIN_SAMPLES_PER_BUCKET samples are collected."""
        learner = _fresh_learner()
        samples = [1000.0, 2000.0, 3000.0, 4000.0, 5000.0]
        for s in samples:
            learner.update(25.0, s)
        # 5 samples → p90_idx = int(5 * 0.9) = 4 → sorted[4] = 5000
        assert learner.learned_rates["21_to_35"] == pytest.approx(5000.0)

    def test_p90_with_more_samples(self) -> None:
        """p90 selects the 90th percentile from a larger sample set."""
        learner = _fresh_learner()
        # 10 sorted values: 100, 200, ..., 1000
        samples = [float(i * 100) for i in range(1, 11)]
        for s in samples:
            learner.update(25.0, s)
        # p90_idx = int(10 * 0.9) = 9 → sorted[9] = 1000
        assert learner.learned_rates["21_to_35"] == pytest.approx(1000.0)

    def test_p90_index_clamped_at_last(self) -> None:
        """p90 index never exceeds last valid index."""
        learner = _fresh_learner()
        # 5 samples → p90_idx = int(5 * 0.9) = 4 → sorted[4] is last element
        samples = [500.0, 1000.0, 1500.0, 2000.0, 2500.0]
        for s in samples:
            learner.update(25.0, s)
        assert learner.learned_rates["21_to_35"] == pytest.approx(2500.0)

    def test_p90_updates_with_new_samples(self) -> None:
        """p90 recalculates as more samples arrive."""
        learner = _fresh_learner()
        # First 5 samples → p90 = 5000
        for s in [1000.0, 2000.0, 3000.0, 4000.0, 5000.0]:
            learner.update(25.0, s)
        assert learner.learned_rates["21_to_35"] == pytest.approx(5000.0)

        # Add 5 more samples → some are lower
        for s in [600.0, 700.0, 800.0, 900.0, 1000.0]:
            learner.update(25.0, s)
        # 10 samples sorted: 600,700,800,900,1000,1000,2000,3000,4000,5000
        # p90_idx = int(10 * 0.9) = 9 → sorted[9] = 5000
        assert learner.learned_rates["21_to_35"] == pytest.approx(5000.0)

    def test_p90_per_bucket_independence(self) -> None:
        """Each bucket computes its own independent p90."""
        learner = _fresh_learner()
        # Cold bucket — low rates
        for _ in range(5):
            learner.update(2.0, 800.0)
        # Warm bucket — high rates
        for _ in range(5):
            learner.update(25.0, 4000.0)
        assert learner.learned_rates["0_to_5"] == pytest.approx(800.0)
        assert learner.learned_rates["21_to_35"] == pytest.approx(4000.0)


# ---------------------------------------------------------------------------
# Fallback tests
# ---------------------------------------------------------------------------


class TestFallbackBehavior:
    """Test get_charge_rate_w fallback logic."""

    def test_fallback_when_no_samples(self) -> None:
        """Return fallback_w when no rate has been learned."""
        learner = _fresh_learner()
        result = learner.get_charge_rate_w(25.0, fallback_w=3000.0)
        assert result == pytest.approx(3000.0)

    def test_fallback_when_temp_is_none(self) -> None:
        """Return fallback_w when temperature is None."""
        learner = _fresh_learner()
        # Learn a rate at 25 °C (21_to_35 bucket).
        for _ in range(5):
            learner.update(25.0, 4000.0)
        # But ask with None temp → fallback
        result = learner.get_charge_rate_w(None, fallback_w=3000.0)
        assert result == pytest.approx(3000.0)

    def test_learned_rate_overrides_fallback(self) -> None:
        """Return learned rate when available."""
        learner = _fresh_learner()
        for _ in range(5):
            learner.update(25.0, 4500.0)
        result = learner.get_charge_rate_w(25.0, fallback_w=3000.0)
        assert result == pytest.approx(4500.0)

    def test_unknown_bucket_falls_back_to_21_to_35(self) -> None:
        """When no bucket matches, fallback to the 21_to_35 bucket."""
        learner = _fresh_learner()
        # Learn a rate for the default bucket
        for _ in range(5):
            learner.update(25.0, 3500.0)
        # An extreme negative temp should still map to some bucket
        result = learner.get_charge_rate_w(-100.0, fallback_w=2000.0)
        # -100 maps to below_0 bucket which has no samples → fallback
        assert result == pytest.approx(2000.0)


# ---------------------------------------------------------------------------
# Sample capping tests
# ---------------------------------------------------------------------------


class TestSampleCapping:
    """Test that sample lists are capped at MAX_SAMPLES_PER_BUCKET."""

    def test_samples_capped_at_max_samples(self) -> None:
        """Sample list is pruned to MAX_SAMPLES_PER_BUCKET when exceeded."""
        learner = _fresh_learner()

        # Pre-load 101 samples directly (one over max).
        learner.samples["21_to_35"] = [float(i) for i in range(101)]

        # Add one more via update to trigger the cap logic (use 25 °C for
        # the 21_to_35 bucket).
        learner.update(25.0, 9999.0)

        assert len(learner.samples["21_to_35"]) <= learner.MAX_SAMPLES_PER_BUCKET
        assert len(learner.samples["21_to_35"]) == learner.MAX_SAMPLES_PER_BUCKET

    def test_samples_not_capped_below_max(self) -> None:
        """Sample list not capped when below max."""
        learner = _fresh_learner()
        for i in range(50):
            learner.update(25.0, float(1000 + i))
        assert len(learner.samples["21_to_35"]) == 50


# ---------------------------------------------------------------------------
# Update with invalid inputs
# ---------------------------------------------------------------------------


class TestInvalidInputs:
    """Test that invalid input values are handled gracefully."""

    def test_none_temp_does_nothing(self) -> None:
        """None temperature should be silently ignored."""
        learner = _fresh_learner()
        learner.update(None, 2000.0)
        assert len(learner.samples["21_to_35"]) == 0

    def test_zero_power_does_nothing(self) -> None:
        """Zero or negative charge power should be ignored."""
        learner = _fresh_learner()
        learner.update(25.0, 0.0)
        learner.update(25.0, -100.0)
        assert len(learner.samples["21_to_35"]) == 0

    def test_positive_power_accumulates(self) -> None:
        """Positive charge power should be recorded."""
        learner = _fresh_learner()
        learner.update(25.0, 1.0)
        assert len(learner.samples["21_to_35"]) == 1
        assert learner.samples["21_to_35"][0] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


class TestModuleSingleton:
    """Test the module-level CHARGE_RATE_LEARNER singleton."""

    def test_singleton_exists(self) -> None:
        """CHARGE_RATE_LEARNER is available as a module-level instance."""
        from custom_components.hsem.utils.charge_rate_learner import (
            CHARGE_RATE_LEARNER,
        )

        assert isinstance(CHARGE_RATE_LEARNER, ChargeRateLearner)

    def test_singleton_buckets_initialized(self) -> None:
        """Singleton has all 7 buckets pre-initialized."""
        import custom_components.hsem.utils.charge_rate_learner as learner_mod

        assert len(learner_mod.CHARGE_RATE_LEARNER.samples) == len(
            learner_mod.TEMP_BUCKETS
        )
        assert len(learner_mod.CHARGE_RATE_LEARNER.learned_rates) == len(
            learner_mod.TEMP_BUCKETS
        )
        for name, _, _ in learner_mod.TEMP_BUCKETS:
            assert name in learner_mod.CHARGE_RATE_LEARNER.samples
            assert name in learner_mod.CHARGE_RATE_LEARNER.learned_rates


# ---------------------------------------------------------------------------
# learned_charge_rate_w property
# ---------------------------------------------------------------------------


class TestLearnedChargeRateProperty:
    """Test the learned_charge_rate_w convenience property."""

    def test_returns_none_for_unknown_bucket(self) -> None:
        """None is returned for an unrecognized bucket name."""
        learner = _fresh_learner()
        assert learner.learned_charge_rate_w("nonexistent") is None

    def test_returns_none_before_samples(self) -> None:
        """None is returned before enough samples are collected."""
        learner = _fresh_learner()
        assert learner.learned_charge_rate_w("21_to_35") is None

    def test_returns_rate_after_samples(self) -> None:
        """Rate is returned after enough samples."""
        learner = _fresh_learner()
        for _ in range(5):
            learner.update(25.0, 3000.0)
        assert learner.learned_charge_rate_w("21_to_35") == pytest.approx(3000.0)


# ---------------------------------------------------------------------------
# Clear learned rate isolation
# ---------------------------------------------------------------------------


class TestClearRateIsolation:
    """Test that update() does not contaminate other buckets."""

    def test_other_buckets_unaffected(self) -> None:
        """Feeding 21_to_35 bucket leaves below_0 at None."""
        learner = _fresh_learner()
        for _ in range(5):
            learner.update(25.0, 3000.0)
        assert learner.learned_rates["21_to_35"] is not None
        assert learner.learned_rates["below_0"] is None
        assert learner.learned_rates["0_to_5"] is None

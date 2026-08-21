"""Tests for the ridge-regression consumption predictor.

Uses lazy imports to avoid triggering the numpy/bcrypt native module
conflict during pytest collection in CI environments.
"""

import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

NOW = datetime(2026, 6, 4, 12, 0).astimezone()


def _mk(d: int, s: int, e: float) -> tuple[datetime, int, float]:
    return (NOW - timedelta(days=d), s, e)


def _predictor(**kwargs):
    """Lazy import to avoid native module conflicts during collection."""
    try:
        from custom_components.hsem.ml.consumption_predictor import (
            ConsumptionPredictor,
        )

        return ConsumptionPredictor(**kwargs)
    except Exception as exc:
        pytest.skip(f"numpy/HA not available in test environment: {exc}")


class TestConsumptionPredictor:
    """Tests for NumPy ridge-regression ConsumptionPredictor."""

    def test_untrained_returns_zero(self) -> None:
        p = _predictor(slots_per_day=96)
        assert p.predict(0, 0) == pytest.approx(0.0)
        assert not p.trained

    def test_constant_history_converges(self) -> None:
        p = _predictor(decay_days=14.0, alpha=0.1, slots_per_day=96)
        p.train([_mk(d, 0, 1.0) for d in range(1, 15)], NOW)
        assert p.trained
        assert p.predict(0, 0, NOW) == pytest.approx(1.0, rel=0.05)

    def test_different_slots(self) -> None:
        p = _predictor(decay_days=14.0, alpha=0.1, slots_per_day=96)
        history = [_mk(d, 0, 0.5) for d in range(1, 15)]
        history += [_mk(d, 32, 3.0) for d in range(1, 15)]
        p.train(history, NOW)
        assert p.predict(0, 0, NOW) == pytest.approx(0.5, rel=0.1)
        assert p.predict(32, 0, NOW) == pytest.approx(3.0, rel=0.1)

    def test_time_decay(self) -> None:
        """A stale observation must be pulled toward fresher behaviour.

        History: slot 0 was 5.0 kWh seven days ago (DOW 3) and 1.0 kWh
        yesterday (DOW 2).  Predicting slot 0 for today (DOW 3) must NOT
        return the stale 5.0 — time-decay (weight exp(-7/2) ≈ 0.03 on the
        old sample) plus slot-level pooling must drag the prediction well
        below it toward the recent 1.0.

        With alpha=0.01 (near-zero regularization) the (DOW, slot) group
        keeps most of its own (stale) mean, so the prediction stays closer
        to 5.0 than to 1.0 — but it must still be strictly below the
        stale value, proving decay+pooling pulled it down.  A prediction
        of exactly 5.0 would mean decay had no effect.
        """
        p = _predictor(decay_days=2.0, alpha=0.01, slots_per_day=96)
        p.train([_mk(7, 0, 5.0), _mk(1, 0, 1.0)], NOW)
        prediction = p.predict(0, 0, NOW)
        assert prediction < 5.0, (
            f"Stale sample must be pulled below 5.0 by decay+pooling, got {prediction}"
        )
        # And with the production default alpha=1.0 the shrinkage is much
        # stronger — the prediction must land well below the midpoint
        # between the stale 5.0 and the recent 1.0.
        p2 = _predictor(decay_days=2.0, alpha=1.0, slots_per_day=96)
        p2.train([_mk(7, 0, 5.0), _mk(1, 0, 1.0)], NOW)
        assert p2.predict(0, 0, NOW) < 2.5

    def test_regularization_effect(self) -> None:
        history = [_mk(d, 0, 2.0) for d in range(1, 15)]
        history.append((datetime(2026, 6, 1, 0, 0).astimezone(), 0, 10.0))

        p_strong = _predictor(decay_days=14.0, alpha=10.0, slots_per_day=96)
        p_strong.train(history, NOW)

        p_weak = _predictor(decay_days=14.0, alpha=0.01, slots_per_day=96)
        p_weak.train(history, NOW)

        mon_strong = p_strong.predict(0, 4, NOW)
        mon_weak = p_weak.predict(0, 4, NOW)
        assert mon_strong < mon_weak

    def test_predict_all_slots(self) -> None:
        p = _predictor(decay_days=7.0, alpha=1.0, slots_per_day=96)
        history = [_mk(d, s, 0.5 + s * 0.005) for d in range(1, 8) for s in range(96)]
        p.train(history, NOW)
        result = p.predict_all_slots(0, NOW)
        assert len(result) == 96
        assert all(v > 0 for v in result.values())

    def test_hourly_mode(self) -> None:
        p = _predictor(decay_days=7.0, alpha=1.0, slots_per_day=24)
        history = [
            (NOW - timedelta(days=d), h, 2.0) for d in range(1, 8) for h in range(24)
        ]
        p.train(history, NOW)
        result = p.predict_all_slots(0, NOW)
        assert len(result) == 24

    def test_min_two_samples(self) -> None:
        p = _predictor(slots_per_day=96)
        p.train([_mk(1, 0, 1.0)], NOW)
        assert not p.trained

    def test_dow_separation(self) -> None:
        p = _predictor(decay_days=14.0, alpha=0.1, slots_per_day=96)
        history = []
        for offset in [3, 10, 17]:
            history.append((NOW - timedelta(days=offset), 32, 2.0))
        for offset in [5, 12, 19]:
            history.append((NOW - timedelta(days=offset), 32, 5.0))
        p.train(history, NOW)
        assert p.predict(32, 2, NOW) > p.predict(32, 4, NOW)

    def test_same_length_rolling_history_retrains_at_unseen_threshold(self) -> None:
        """A sliding window must refit even when its sample count stays constant."""
        tz = NOW.tzinfo
        assert tz is not None

        def sample(month: int, day: int, energy: float) -> tuple[datetime, int, float]:
            return datetime(2026, month, day, 12, 0, tzinfo=tz), 48, energy

        initial_reference = datetime(2026, 7, 16, 13, 0, tzinfo=tz)
        initial_history = [
            sample(6, 25, 1.0),
            sample(7, 2, 1.0),
            sample(7, 9, 1.0),
            sample(7, 16, 1.0),
        ]
        p = _predictor(
            decay_days=30.0,
            alpha=0.1,
            slots_per_day=96,
            retrain_min_new_samples=2,
        )

        p.train(initial_history, initial_reference)
        baseline_prediction = p.predict(48, 0, initial_reference)
        assert p.last_fit_time == initial_reference

        # Truly unchanged history retains the cheap gate even as time advances.
        p.train(initial_history, initial_reference + timedelta(minutes=15))
        assert p.last_fit_time == initial_reference
        assert p.predict(48, 0, initial_reference) == pytest.approx(baseline_prediction)

        # One unseen physical sample is below the configured threshold.
        one_new_reference = datetime(2026, 7, 23, 13, 0, tzinfo=tz)
        one_new_sample = [
            sample(7, 2, 1.0),
            sample(7, 9, 1.0),
            sample(7, 16, 1.0),
            sample(7, 23, 4.0),
        ]
        p.train(one_new_sample, one_new_reference)
        assert p.last_fit_time == initial_reference
        assert p.predict(48, 0, initial_reference) == pytest.approx(baseline_prediction)

        # The second unseen sample reaches the threshold.  Length is still
        # four, but the fitted coefficients and prediction must update.
        two_new_reference = datetime(2026, 7, 30, 13, 0, tzinfo=tz)
        two_new_samples = [
            sample(7, 9, 1.0),
            sample(7, 16, 1.0),
            sample(7, 23, 4.0),
            sample(7, 30, 6.0),
        ]
        p.train(two_new_samples, two_new_reference)

        assert p.last_fit_samples == len(initial_history)
        assert p.last_fit_time == two_new_reference
        assert p.predict(48, 0, initial_reference) > baseline_prediction

    def test_revised_sample_fingerprint_triggers_same_length_refit(self) -> None:
        """A corrected value at an existing physical slot counts as changed."""
        tz = NOW.tzinfo
        assert tz is not None
        reference = datetime(2026, 7, 30, 13, 0, tzinfo=tz)
        first_timestamp = datetime(2026, 7, 23, 12, 0, tzinfo=tz)
        revised_timestamp = datetime(2026, 7, 30, 12, 0, tzinfo=tz)
        history = [
            (first_timestamp, 48, 1.0),
            (revised_timestamp, 48, 1.0),
        ]
        p = _predictor(
            decay_days=30.0,
            alpha=0.1,
            slots_per_day=96,
            retrain_min_new_samples=1,
        )

        p.train(history, reference)
        baseline_prediction = p.predict(48, 0, reference)

        revised_history = [
            history[0],
            (revised_timestamp, 48, 5.0),
        ]
        revised_reference = reference + timedelta(minutes=15)
        p.train(revised_history, revised_reference)

        assert p.last_fit_samples == len(history)
        assert p.last_fit_time == revised_reference
        assert p.predict(48, 0, reference) > baseline_prediction

    def test_sequential_training_resets_lag_across_recorder_gap(self) -> None:
        """A missing physical interval must not become another sample's lag."""
        tz = ZoneInfo("Europe/Stockholm")
        reference = datetime(2026, 8, 20, 1, 0, tzinfo=tz)
        slot_zero = datetime(2026, 8, 20, 0, 0, tzinfo=tz)
        slot_one = datetime(2026, 8, 20, 0, 15, tzinfo=tz)
        slot_three = datetime(2026, 8, 20, 0, 45, tzinfo=tz)
        predictor = _predictor(
            slots_per_day=96,
            retrain_min_new_samples=1,
            use_sequential=True,
        )

        # Recorder order is deliberately scrambled; physical UTC order wins.
        predictor.train(
            [
                (slot_three, 3, 4.0),
                (slot_zero, 0, 1.0),
                (slot_one, 1, 2.0),
            ],
            reference,
        )

        assert predictor._X is not None
        assert predictor._y is not None
        assert predictor._y.tolist() == pytest.approx([1.0, 2.0, 4.0])
        assert predictor._X[:, predictor._lag_offset].tolist() == pytest.approx(
            [0.0, 1.0, 0.0]
        )

    def test_sequential_inference_skips_nonexistent_spring_slots(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The lag chain follows physical slots across the spring jump."""
        tz = ZoneInfo("Europe/Stockholm")
        predictor = _predictor(slots_per_day=96, use_sequential=True)
        predictor.train([_mk(2, 0, 1.0), _mk(1, 0, 1.0)], NOW)
        seen: list[tuple[int, int, int, float]] = []

        def fake_predict(
            timestamp: datetime,
            _slot: int,
            _temperature: float | None,
            prev_energy: float,
        ) -> float:
            seen.append((timestamp.hour, timestamp.minute, timestamp.fold, prev_energy))
            return prev_energy + 1.0

        monkeypatch.setattr(predictor, "_predict_from_features", fake_predict)
        starts = [
            datetime(2026, 3, 29, 3, 15, tzinfo=tz),
            datetime(2026, 3, 29, 1, 45, tzinfo=tz),
            datetime(2026, 3, 29, 3, 0, tzinfo=tz),
        ]

        result = predictor.predict_sequential(starts)

        assert list(result) == [
            datetime(2026, 3, 29, 0, 45, tzinfo=UTC),
            datetime(2026, 3, 29, 1, 0, tzinfo=UTC),
            datetime(2026, 3, 29, 1, 15, tzinfo=UTC),
        ]
        assert list(result.values()) == pytest.approx([1.0, 2.0, 3.0])
        assert seen == [
            (1, 45, 0, 0.0),
            (3, 0, 0, 1.0),
            (3, 15, 0, 2.0),
        ]

    def test_sequential_inference_keeps_both_autumn_folds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Repeated wall slots remain distinct members of one physical chain."""
        tz = ZoneInfo("Europe/Stockholm")
        predictor = _predictor(slots_per_day=96, use_sequential=True)
        predictor.train([_mk(2, 0, 1.0), _mk(1, 0, 1.0)], NOW)

        def fake_predict(
            _timestamp: datetime,
            _slot: int,
            _temperature: float | None,
            prev_energy: float,
        ) -> float:
            return prev_energy + 1.0

        monkeypatch.setattr(predictor, "_predict_from_features", fake_predict)
        first_fold = [
            datetime(2026, 10, 25, 2, minute, tzinfo=tz, fold=0)
            for minute in (0, 15, 30, 45)
        ]
        second_fold_start = datetime(2026, 10, 25, 2, 0, tzinfo=tz, fold=1)

        result = predictor.predict_sequential(
            [second_fold_start, *reversed(first_fold)]
        )

        assert len(result) == 5
        assert result[first_fold[0].astimezone(UTC)] == pytest.approx(1.0)
        assert result[second_fold_start.astimezone(UTC)] == pytest.approx(5.0)

    def test_nonfinite_energy_and_temperature_never_contaminate_fit(self) -> None:
        predictor = _predictor(
            slots_per_day=96,
            retrain_min_new_samples=1,
            use_temperature=True,
        )
        bad_history = [
            (_mk(2, 0, 1.0)[0], 0, float("nan")),
            (_mk(1, 1, 1.0)[0], 1, float("inf")),
        ]
        predictor.train(
            bad_history,
            NOW,
            {bad_history[0][0]: float("nan")},
        )
        assert predictor.trained is False

        good_history = [_mk(2, 0, 1.0), _mk(1, 1, 2.0)]
        predictor.train(
            good_history,
            NOW,
            {timestamp: float("-inf") for timestamp, _slot, _energy in good_history},
        )

        assert predictor.trained is True
        assert predictor.last_fit_samples == 2
        assert math.isfinite(predictor.predict(0, 0, NOW, float("nan")))

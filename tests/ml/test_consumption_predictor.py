"""Tests for the ridge-regression consumption predictor.

Uses lazy imports to avoid triggering the numpy/bcrypt native module
conflict during pytest collection in CI environments.
"""

from datetime import datetime, timedelta

import pytest

NOW = datetime(2026, 6, 4, 12, 0).astimezone()


def _mk(d: int, s: int, e: float) -> tuple[datetime, int, float]:
    return (NOW - timedelta(days=d), s, e)


def _predictor(**kwargs):
    """Lazy import to avoid native module conflicts during collection."""
    from custom_components.hsem.ml.consumption_predictor import (
        ConsumptionPredictor,
    )

    return ConsumptionPredictor(**kwargs)


class TestConsumptionPredictor:
    """Tests for NumPy ridge-regression ConsumptionPredictor."""

    def test_untrained_returns_zero(self) -> None:
        p = _predictor(slots_per_day=96)
        assert p.predict(0, 0) == 0.0
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
        p = _predictor(decay_days=2.0, alpha=0.01, slots_per_day=96)
        p.train([_mk(7, 0, 5.0), _mk(1, 0, 1.0)], NOW)
        assert p.predict(0, 0, NOW) < 2.5

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

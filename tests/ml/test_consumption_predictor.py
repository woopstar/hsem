"""Tests for the ridge-regression consumption predictor.

The ConsumptionPredictor has no Home Assistant dependencies (only numpy,
math, and datetime).  We import it directly from the filesystem to avoid
triggering the full HA import chain via custom_components/__init__.py.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# Add the repository root to sys.path so we can import the ml module
# without going through custom_components/__init__.py (which triggers
# the full Home Assistant import chain including bcrypt).
_repo_root = Path(__file__).resolve().parents[3]
_hsem_root = _repo_root / "custom_components" / "hsem"
if str(_hsem_root) not in sys.path:
    sys.path.insert(0, str(_hsem_root))

from ml.consumption_predictor import ConsumptionPredictor  # noqa: E402

NOW = datetime(2026, 6, 4, 12, 0).astimezone()  # Thursday


def _mk(d: int, s: int, e: float) -> tuple[datetime, int, float]:
    """Helper: create history entry d days ago, slot s, energy e."""
    return (NOW - timedelta(days=d), s, e)


class TestConsumptionPredictor:
    """Tests for NumPy ridge-regression ConsumptionPredictor."""

    def test_untrained_returns_zero(self) -> None:
        p = ConsumptionPredictor(slots_per_day=96)
        assert p.predict(0, 0) == 0.0
        assert not p.trained

    def test_constant_history_converges(self) -> None:
        p = ConsumptionPredictor(decay_days=14.0, alpha=0.1, slots_per_day=96)
        p.train([_mk(d, 0, 1.0) for d in range(1, 15)], NOW)
        assert p.trained
        assert p.predict(0, 0, NOW) == pytest.approx(1.0, rel=0.05)

    def test_different_slots(self) -> None:
        p = ConsumptionPredictor(decay_days=14.0, alpha=0.1, slots_per_day=96)
        history = [_mk(d, 0, 0.5) for d in range(1, 15)]
        history += [_mk(d, 32, 3.0) for d in range(1, 15)]
        p.train(history, NOW)
        assert p.predict(0, 0, NOW) == pytest.approx(0.5, rel=0.1)
        assert p.predict(32, 0, NOW) == pytest.approx(3.0, rel=0.1)

    def test_time_decay(self) -> None:
        p = ConsumptionPredictor(decay_days=2.0, alpha=0.01, slots_per_day=96)
        p.train([_mk(7, 0, 5.0), _mk(1, 0, 1.0)], NOW)
        assert p.predict(0, 0, NOW) < 2.5

    def test_regularization_effect(self) -> None:
        """Strong alpha pulls sparse groups toward mean."""
        history = [_mk(d, 0, 2.0) for d in range(1, 15)]
        history.append((datetime(2026, 6, 1, 0, 0).astimezone(), 0, 10.0))  # Monday

        p_strong = ConsumptionPredictor(decay_days=14.0, alpha=10.0, slots_per_day=96)
        p_strong.train(history, NOW)

        p_weak = ConsumptionPredictor(decay_days=14.0, alpha=0.01, slots_per_day=96)
        p_weak.train(history, NOW)

        # Monday (day_offset 4 from Thu) — strong alpha pulls 10→mean
        mon_strong = p_strong.predict(0, 4, NOW)
        mon_weak = p_weak.predict(0, 4, NOW)
        assert mon_strong < mon_weak, (
            f"Strong α should pull toward mean: {mon_strong:.3f} vs {mon_weak:.3f}"
        )

    def test_predict_all_slots(self) -> None:
        p = ConsumptionPredictor(decay_days=7.0, alpha=1.0, slots_per_day=96)
        history = [_mk(d, s, 0.5 + s * 0.005) for d in range(1, 8) for s in range(96)]
        p.train(history, NOW)
        result = p.predict_all_slots(0, NOW)
        assert len(result) == 96
        assert all(v > 0 for v in result.values())

    def test_hourly_mode(self) -> None:
        p = ConsumptionPredictor(decay_days=7.0, alpha=1.0, slots_per_day=24)
        history = [
            (NOW - timedelta(days=d), h, 2.0) for d in range(1, 8) for h in range(24)
        ]
        p.train(history, NOW)
        result = p.predict_all_slots(0, NOW)
        assert len(result) == 24

    def test_min_two_samples(self) -> None:
        p = ConsumptionPredictor(slots_per_day=96)
        p.train([_mk(1, 0, 1.0)], NOW)
        assert not p.trained

    def test_dow_separation(self) -> None:
        """Monday slot 32 = 2.0, Saturday slot 32 = 5.0."""
        p = ConsumptionPredictor(decay_days=14.0, alpha=0.1, slots_per_day=96)
        history = []
        for offset in [3, 10, 17]:  # Mondays before Thu Jun 4
            history.append((NOW - timedelta(days=offset), 32, 2.0))
        for offset in [5, 12, 19]:  # Saturdays
            history.append((NOW - timedelta(days=offset), 32, 5.0))
        p.train(history, NOW)
        # Next Monday = +4d, next Saturday = +2d from Thu
        assert p.predict(32, 2, NOW) > p.predict(32, 4, NOW)

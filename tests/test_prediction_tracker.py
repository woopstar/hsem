"""Tests for the prediction accuracy tracker.

Covers:
- Record addition and warm-up gate.
- Rolling buffer / max_records pruning.
- SoC MAE computation (7d and 30d windows).
- Solar MAPE computation with edge cases (zero forecast/actual).
- Load MAE computation.
- Action mix computation.
- Deduplication of slot starts.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.hsem.utils.prediction_tracker import (
    PredictionTracker,
    _action_label,
)

# Sentinel timestamp for tests
NEVER = datetime(2099, 1, 1, tzinfo=UTC)


def _slot_start(hour: int, minute: int = 0) -> datetime:
    """Create a timezone-aware slot start time."""
    return datetime(2024, 6, 15, hour, minute, tzinfo=UTC)


def _make_record_args(
    hour: int,
    minute: int = 0,
    *,
    predicted_soc: float = 50.0,
    actual_soc: float = 52.0,
    predicted_pv: float = 0.5,
    actual_pv: float = 0.4,
    predicted_load: float = 0.3,
    actual_load: float = 0.35,
    action: str = "idle",
) -> dict:
    """Return keyword arguments for ``add_record``."""
    return {
        "predicted_soc": predicted_soc,
        "actual_soc": actual_soc,
        "predicted_pv": predicted_pv,
        "actual_pv": actual_pv,
        "predicted_load": predicted_load,
        "actual_load": actual_load,
        "action": action,
        "slot_start": _slot_start(hour, minute),
    }


# ---------------------------------------------------------------------------
# _action_label helper
# ---------------------------------------------------------------------------


class TestActionLabel:
    """Tests for the recommendation-to-action-label helper."""

    def test_charge_grid(self) -> None:
        """Grid charge maps to 'charge'."""
        assert _action_label("batteries_charge_grid") == "charge"

    def test_charge_solar(self) -> None:
        """Solar charge maps to 'charge'."""
        assert _action_label("batteries_charge_solar") == "charge"

    def test_discharge_mode(self) -> None:
        """Discharge mode maps to 'discharge'."""
        assert _action_label("batteries_discharge_mode") == "discharge"

    def test_force_discharge(self) -> None:
        """Force discharge maps to 'discharge'."""
        assert _action_label("force_batteries_discharge") == "discharge"

    def test_none(self) -> None:
        """None maps to 'idle'."""
        assert _action_label(None) == "idle"

    def test_unknown(self) -> None:
        """Unknown recommendation maps to 'idle'."""
        assert _action_label("something_else") == "idle"

    def test_ev_smart_charging(self) -> None:
        """EV smart charging maps to 'idle'."""
        assert _action_label("ev_smart_charging") == "idle"


# ---------------------------------------------------------------------------
# Warm-up gate
# ---------------------------------------------------------------------------


class TestWarmupGate:
    """Tests for the warm-up gate that skips the first 4 slots."""

    def test_warmup_skips_first_4(self) -> None:
        """First 4 slots are skipped, 5th is recorded."""
        tracker = PredictionTracker()
        tracker._warmup_slots = 4

        for i in range(4):
            tracker.add_record(**_make_record_args(hour=0, minute=i * 15))

        assert len(tracker.records) == 0
        assert tracker.soc_mae_7d is None

        # 5th slot — should be recorded
        tracker.add_record(**_make_record_args(hour=1, minute=0))
        assert len(tracker.records) == 1
        assert tracker.soc_mae_7d is not None

    def test_reset_warmup(self) -> None:
        """Resetting the warm-up counter restarts the gate."""
        tracker = PredictionTracker()
        tracker._warmup_slots = 2

        # Feed 2 slots — both skipped
        tracker.add_record(**_make_record_args(hour=0, minute=0))
        tracker.add_record(**_make_record_args(hour=0, minute=15))
        assert len(tracker.records) == 0

        # 3rd slot recorded
        tracker.add_record(**_make_record_args(hour=0, minute=30))
        assert len(tracker.records) == 1

        # Reset and feed again
        tracker.reset_warmup()
        tracker.add_record(**_make_record_args(hour=0, minute=45))
        assert len(tracker.records) == 1  # still skipped
        tracker.add_record(**_make_record_args(hour=1, minute=0))
        assert len(tracker.records) == 1  # still skipped
        tracker.add_record(**_make_record_args(hour=1, minute=15))
        assert len(tracker.records) == 2  # now through

    def test_warmup_zero(self) -> None:
        """When warmup_slots is 0, first slot is recorded immediately."""
        tracker = PredictionTracker()
        tracker._warmup_slots = 0

        tracker.add_record(**_make_record_args(hour=0, minute=0))
        assert len(tracker.records) == 1


# ---------------------------------------------------------------------------
# Record addition and deduplication
# ---------------------------------------------------------------------------


class TestRecordAddition:
    """Tests for record addition and slot-start deduplication."""

    def test_add_single_record(self) -> None:
        """A single record is added correctly."""
        tracker = PredictionTracker(_warmup_slots=0)
        tracker.add_record(**_make_record_args(hour=0))

        assert len(tracker.records) == 1
        rec = tracker.records[0]
        assert rec.predicted_soc_pct == 50.0
        assert rec.actual_soc_pct == 52.0
        assert rec.predicted_pv_kwh == 0.5
        assert rec.actual_pv_kwh == 0.4
        assert rec.predicted_load_kwh == 0.3
        assert rec.actual_load_kwh == 0.35
        assert rec.action == "idle"

    def test_deduplication(self) -> None:
        """Duplicate slot starts are silently ignored."""
        tracker = PredictionTracker(_warmup_slots=0)
        args = _make_record_args(hour=0)

        tracker.add_record(**args)
        tracker.add_record(**args)  # duplicate
        tracker.add_record(**args)  # duplicate again

        assert len(tracker.records) == 1

    def test_multiple_slots(self) -> None:
        """Multiple distinct slots are all recorded."""
        tracker = PredictionTracker(_warmup_slots=0)

        for i in range(5):
            tracker.add_record(**_make_record_args(hour=i))

        assert len(tracker.records) == 5


# ---------------------------------------------------------------------------
# SoC MAE computation
# ---------------------------------------------------------------------------


class TestSoCMAE:
    """Tests for SoC MAE over 7-day and 30-day windows."""

    def test_soc_mae_exact_match(self) -> None:
        """SoC MAE is zero when predicted == actual."""
        tracker = PredictionTracker(_warmup_slots=0)
        tracker.add_record(
            **_make_record_args(hour=0, predicted_soc=50.0, actual_soc=50.0)
        )
        assert tracker.soc_mae_7d == pytest.approx(0.0)
        assert tracker.soc_mae_30d == pytest.approx(0.0)

    def test_soc_mae_positive_error(self) -> None:
        """SoC MAE with positive errors."""
        tracker = PredictionTracker(_warmup_slots=0)
        tracker.add_record(
            **_make_record_args(hour=0, predicted_soc=50.0, actual_soc=55.0)
        )
        tracker.add_record(
            **_make_record_args(hour=1, predicted_soc=60.0, actual_soc=58.0)
        )

        # avg of |50-55|=5 and |60-58|=2 = 3.5
        assert tracker.soc_mae_7d == pytest.approx(3.5)

    def test_soc_mae_7d_window(self) -> None:
        """7d window is capped at 672 most recent records."""
        tracker = PredictionTracker(_warmup_slots=0, max_records=10)

        # Add 12 records — first 2 should be pruned
        for i in range(12):
            tracker.add_record(
                **_make_record_args(
                    hour=i // 4,
                    minute=(i % 4) * 15,
                    predicted_soc=float(50 + i),
                    actual_soc=float(52 + i),
                )
            )

        assert len(tracker.records) == 10
        # 7d window should be the last 7 days worth (but we only have 10 total)
        # SoC error should be 2.0 for each record
        assert tracker.soc_mae_7d == pytest.approx(2.0)

    def test_soc_mae_30d_same_as_7d_with_few_records(self) -> None:
        """When fewer than 672 records exist, 7d and 30d MAE are equal."""
        tracker = PredictionTracker(_warmup_slots=0)
        for i in range(5):
            tracker.add_record(
                **_make_record_args(
                    hour=i,
                    predicted_soc=50.0,
                    actual_soc=53.0,
                )
            )
        assert tracker.soc_mae_7d == pytest.approx(3.0)
        assert tracker.soc_mae_30d == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Solar MAPE computation
# ---------------------------------------------------------------------------


class TestSolarMAPE:
    """Tests for PV forecast MAPE."""

    def test_solar_mape_exact_match(self) -> None:
        """MAPE is zero when PV predicted == actual."""
        tracker = PredictionTracker(_warmup_slots=0)
        tracker.add_record(
            **_make_record_args(
                hour=0, predicted_pv=0.5, actual_pv=0.5, predicted_load=0, actual_load=0
            )
        )
        assert tracker.solar_mape == pytest.approx(0.0)

    def test_solar_mape_over_forecast(self) -> None:
        """MAPE for PV over-forecast: predicted 1.0, actual 0.5 → 100% error."""
        tracker = PredictionTracker(_warmup_slots=0)
        tracker.add_record(
            **_make_record_args(
                hour=0,
                predicted_pv=1.0,
                actual_pv=0.5,
                predicted_load=0,
                actual_load=0,
            )
        )
        assert tracker.solar_mape == pytest.approx(100.0)

    def test_solar_mape_under_forecast(self) -> None:
        """MAPE for PV under-forecast: predicted 0.5, actual 1.0 → 50% error."""
        tracker = PredictionTracker(_warmup_slots=0)
        tracker.add_record(
            **_make_record_args(
                hour=0,
                predicted_pv=0.5,
                actual_pv=1.0,
                predicted_load=0,
                actual_load=0,
            )
        )
        assert tracker.solar_mape == pytest.approx(50.0)

    def test_solar_mape_zero_actual(self) -> None:
        """MAPE is None when all actual PV is zero (division by zero)."""
        tracker = PredictionTracker(_warmup_slots=0)
        tracker.add_record(
            **_make_record_args(
                hour=0, predicted_pv=0.5, actual_pv=0.0, predicted_load=0, actual_load=0
            )
        )
        assert tracker.solar_mape is None

    def test_solar_mape_skips_zero_actual(self) -> None:
        """MAPE only considers records with non-zero actual PV."""
        tracker = PredictionTracker(_warmup_slots=0)
        # First record: actual=0 — excluded from MAPE
        tracker.add_record(
            **_make_record_args(
                hour=0, predicted_pv=0.5, actual_pv=0.0, predicted_load=0, actual_load=0
            )
        )
        # Second record: actual=1.0, predicted=1.0 → 0% error
        tracker.add_record(
            **_make_record_args(
                hour=1, predicted_pv=1.0, actual_pv=1.0, predicted_load=0, actual_load=0
            )
        )
        assert tracker.solar_mape == pytest.approx(0.0)

    def test_solar_mape_none_no_records(self) -> None:
        """MAPE is None when no records at all."""
        tracker = PredictionTracker()
        assert tracker.solar_mape is None


# ---------------------------------------------------------------------------
# Load MAE computation
# ---------------------------------------------------------------------------


class TestLoadMAE:
    """Tests for load prediction MAE."""

    def test_load_mae_exact_match(self) -> None:
        """Load MAE is zero when predicted == actual."""
        tracker = PredictionTracker(_warmup_slots=0)
        tracker.add_record(
            **_make_record_args(hour=0, predicted_load=0.3, actual_load=0.3)
        )
        assert tracker.load_mae_kwh == pytest.approx(0.0)

    def test_load_mae_over_forecast(self) -> None:
        """Load MAE: predicted 0.5, actual 0.2 → 0.3 kWh."""
        tracker = PredictionTracker(_warmup_slots=0)
        tracker.add_record(
            **_make_record_args(hour=0, predicted_load=0.5, actual_load=0.2)
        )
        assert tracker.load_mae_kwh == pytest.approx(0.3)

    def test_load_mae_mixed(self) -> None:
        """Average MAE over multiple records."""
        tracker = PredictionTracker(_warmup_slots=0)
        tracker.add_record(
            **_make_record_args(
                hour=0,
                predicted_load=0.3,
                actual_load=0.0,  # error 0.3
            )
        )
        tracker.add_record(
            **_make_record_args(
                hour=1,
                predicted_load=0.5,
                actual_load=0.3,  # error 0.2
            )
        )
        # avg = (0.3 + 0.2) / 2 = 0.25
        assert tracker.load_mae_kwh == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Action mix computation
# ---------------------------------------------------------------------------


class TestActionMix:
    """Tests for action mix computation."""

    def test_action_mix_all_idle(self) -> None:
        """When all slots are idle, mix is {'idle': 1.0}."""
        tracker = PredictionTracker(_warmup_slots=0)
        for i in range(3):
            tracker.add_record(**_make_record_args(hour=i, action="idle"))
        assert tracker.action_mix == {"idle": 1.0}

    def test_action_mix_mixed(self) -> None:
        """Action mix reflects the proportion of each action."""
        tracker = PredictionTracker(_warmup_slots=0)
        tracker.add_record(**_make_record_args(hour=0, action="charge"))
        tracker.add_record(**_make_record_args(hour=1, action="charge"))
        tracker.add_record(**_make_record_args(hour=2, action="discharge"))
        tracker.add_record(**_make_record_args(hour=3, action="idle"))

        assert tracker.action_mix == {
            "charge": 0.5,
            "discharge": 0.25,
            "idle": 0.25,
        }

    def test_action_mix_empty(self) -> None:
        """Action mix is empty when no records exist."""
        tracker = PredictionTracker()
        assert tracker.action_mix == {}


# ---------------------------------------------------------------------------
# Max records pruning
# ---------------------------------------------------------------------------


class TestMaxRecordsPruning:
    """Tests for rolling buffer pruning."""

    def test_prune_exceeds_max(self) -> None:
        """Oldest records are removed when buffer exceeds max_records."""
        tracker = PredictionTracker(_warmup_slots=0, max_records=3)

        slots = []
        for i in range(5):
            start = _slot_start(hour=i)
            slots.append(start)
            tracker.add_record(
                predicted_soc=50.0,
                actual_soc=50.0,
                predicted_pv=0.5,
                actual_pv=0.5,
                predicted_load=0.3,
                actual_load=0.3,
                action="idle",
                slot_start=start,
            )

        assert len(tracker.records) == 3
        # First 2 records should be pruned
        assert tracker.records[0].slot_start == slots[2]
        assert tracker.records[2].slot_start == slots[4]

    def test_prune_cleans_recorded_starts(self) -> None:
        """When records are pruned, their slot starts are removed from the set."""
        tracker = PredictionTracker(_warmup_slots=0, max_records=2)

        start_0 = _slot_start(hour=0)
        start_1 = _slot_start(hour=1)
        start_2 = _slot_start(hour=2)

        # Build args dicts and override slot_start for this test.
        a0 = _make_record_args(hour=0)
        a0["slot_start"] = start_0
        a1 = _make_record_args(hour=1)
        a1["slot_start"] = start_1
        a2 = _make_record_args(hour=2)
        a2["slot_start"] = start_2

        tracker.add_record(**a0)
        tracker.add_record(**a1)
        tracker.add_record(**a2)

        # start_0 should be pruned
        assert start_0 not in tracker._recorded_starts
        assert start_1 in tracker._recorded_starts
        assert start_2 in tracker._recorded_starts

        # Adding start_0 again should work (it was pruned)
        a0_again = _make_record_args(hour=0)
        a0_again["slot_start"] = start_0
        tracker.add_record(**a0_again)
        assert len(tracker.records) == 2  # max_records=2 prunes oldest


# ---------------------------------------------------------------------------
# Empty / edge cases
# ---------------------------------------------------------------------------


class TestEmptyTracker:
    """Tests for the tracker in its initial state."""

    def test_all_metrics_none_initially(self) -> None:
        """All metrics are None when no records exist."""
        tracker = PredictionTracker()
        assert tracker.soc_mae_7d is None
        assert tracker.soc_mae_30d is None
        assert tracker.solar_mape is None
        assert tracker.load_mae_kwh is None
        assert tracker.action_mix == {}
        assert len(tracker.records) == 0

    def test_compute_metrics_on_empty_is_safe(self) -> None:
        """Calling compute_metrics on an empty tracker is safe."""
        tracker = PredictionTracker()
        tracker.compute_metrics()
        assert tracker.soc_mae_7d is None
        assert tracker.solar_mape is None

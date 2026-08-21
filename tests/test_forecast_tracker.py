"""Tests for the forecast-vs-actual tracker.

Covers:
- Exact match (perfect forecast).
- Over-forecast (predicted > actual).
- Under-forecast (predicted < actual).
- Accumulation of energy from power readings.
- Record lifecycle (create, finalise, accumulate).
- Summary computation (MAE, bias, RMSE, MAPE).
- Edge cases: empty tracker, division-by-zero in MAPE.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.utils.forecast_tracker import (
    ForecastSlotRecord,
    ForecastTracker,
    compute_accumulated_energy,
)

# Sentinel timestamp for 'nonexistent slot' tests
NEVER = datetime(2099, 1, 1, tzinfo=UTC)


def _slot_start(hour: int, minute: int = 0) -> datetime:
    """Create a timezone-aware slot start time."""
    return datetime(2024, 6, 15, hour, minute, tzinfo=UTC)


def test_repeated_hour_folds_create_distinct_forecast_records() -> None:
    copenhagen = ZoneInfo("Europe/Copenhagen")
    first = datetime(2026, 10, 25, 2, 0, tzinfo=copenhagen, fold=0)
    second = datetime(2026, 10, 25, 2, 0, tzinfo=copenhagen, fold=1)
    tracker = ForecastTracker()

    first_record = tracker.get_or_create_record(
        first,
        (first.astimezone(UTC) + timedelta(minutes=15)).astimezone(copenhagen),
    )
    second_record = tracker.get_or_create_record(
        second,
        (second.astimezone(UTC) + timedelta(minutes=15)).astimezone(copenhagen),
    )

    assert first_record is not second_record
    assert len(tracker.records) == 2


# ---------------------------------------------------------------------------
# compute_accumulated_energy
# ---------------------------------------------------------------------------


class TestComputeAccumulatedEnergy:
    """Tests for the power-to-energy conversion helper."""

    def test_1000w_for_1_hour(self) -> None:
        """1000 W for 3600 s = 1 kWh."""
        assert compute_accumulated_energy(1000.0, 3600.0) == pytest.approx(1.0)

    def test_500w_for_30_minutes(self) -> None:
        """500 W for 1800 s = 0.25 kWh."""
        assert compute_accumulated_energy(500.0, 1800.0) == pytest.approx(0.25)

    def test_zero_power(self) -> None:
        """Zero power yields zero energy regardless of elapsed time."""
        assert compute_accumulated_energy(0.0, 3600.0) == pytest.approx(0.0)

    def test_zero_elapsed(self) -> None:
        """Zero elapsed time yields zero energy regardless of power."""
        assert compute_accumulated_energy(5000.0, 0.0) == pytest.approx(0.0)

    def test_negative_power(self) -> None:
        """Negative power is handled gracefully (e.g. net metering)."""
        result = compute_accumulated_energy(-500.0, 1800.0)
        assert result == pytest.approx(-0.25)


# ---------------------------------------------------------------------------
# ForecastSlotRecord
# ---------------------------------------------------------------------------


class TestForecastSlotRecord:
    """Tests for individual slot records."""

    def test_finalise_computes_metrics(self) -> None:
        """Finalise computes MAE and bias from accumulated actuals."""
        rec = ForecastSlotRecord(
            start=_slot_start(10),
            end=_slot_start(11),
            forecast_pv_kwh=4.0,
            forecast_load_kwh=2.0,
            actual_pv_kwh=3.5,
            actual_load_kwh=2.5,
        )
        rec.finalise()
        assert rec.finalised
        assert rec.mae_pv == pytest.approx(0.5)
        assert rec.mae_load == pytest.approx(0.5)
        assert rec.bias_pv == pytest.approx(0.5)  # over-forecast
        assert rec.bias_load == pytest.approx(-0.5)  # under-forecast

    def test_finalise_exact_match(self) -> None:
        """Exact match produces zero error."""
        rec = ForecastSlotRecord(
            start=_slot_start(10),
            end=_slot_start(11),
            forecast_pv_kwh=3.0,
            forecast_load_kwh=2.0,
            actual_pv_kwh=3.0,
            actual_load_kwh=2.0,
        )
        rec.finalise()
        assert rec.mae_pv == pytest.approx(0.0)
        assert rec.mae_load == pytest.approx(0.0)
        assert rec.bias_pv == pytest.approx(0.0)
        assert rec.bias_load == pytest.approx(0.0)

    def test_accumulate_updates_actuals(self) -> None:
        """Accumulate adds energy to the slot's actuals."""
        rec = ForecastSlotRecord(
            start=_slot_start(10),
            end=_slot_start(11),
        )
        rec.accumulate_pv(1.0)
        rec.accumulate_pv(0.5)
        rec.accumulate_load(2.0)
        assert rec.actual_pv_kwh == pytest.approx(1.5)
        assert rec.actual_load_kwh == pytest.approx(2.0)

    def test_cannot_accumulate_after_finalise(self) -> None:
        """Accumulation after finalise still modifies actuals (no guard)."""
        rec = ForecastSlotRecord(
            start=_slot_start(10),
            end=_slot_start(11),
            forecast_pv_kwh=1.0,
            forecast_load_kwh=1.0,
        )
        rec.finalise()
        # Doc says "must not be called", but there's no runtime guard.
        # The test verifies it doesn't crash.
        rec.accumulate_pv(0.5)
        assert rec.actual_pv_kwh == pytest.approx(0.5)

    def test_finalise_is_idempotent(self) -> None:
        """Calling finalise twice produces the same result."""
        rec = ForecastSlotRecord(
            start=_slot_start(10),
            end=_slot_start(11),
            forecast_pv_kwh=4.0,
            forecast_load_kwh=2.0,
            actual_pv_kwh=3.5,
            actual_load_kwh=2.5,
        )
        rec.finalise()
        saved_mae = rec.mae_pv
        # Accumulate would change actuals, then finalise again
        rec.accumulate_pv(10.0)
        rec.finalise()  # no-op because already finalised
        assert rec.mae_pv == saved_mae  # unchanged


# ---------------------------------------------------------------------------
# ForecastTracker — record lifecycle
# ---------------------------------------------------------------------------


class TestForecastTrackerLifecycle:
    """Tests for creating, finding, and finalising records."""

    def test_get_or_create_record_new(self) -> None:
        """get_or_create_record creates a record when none exists."""
        tracker = ForecastTracker()
        rec = tracker.get_or_create_record(_slot_start(10), _slot_start(11))
        assert rec.start == _slot_start(10)
        assert rec.end == _slot_start(11)
        assert len(tracker.records) == 1

    def test_get_or_create_record_existing(self) -> None:
        """get_or_create_record returns the same record for the same start."""
        tracker = ForecastTracker()
        rec1 = tracker.get_or_create_record(_slot_start(10), _slot_start(11))
        rec2 = tracker.get_or_create_record(_slot_start(10), _slot_start(12))
        assert rec1 is rec2
        assert len(tracker.records) == 1

    def test_find_record_missing(self) -> None:
        """find_record returns None for a non-existent slot."""
        tracker = ForecastTracker()
        assert tracker.find_record(_slot_start(10)) is None

    def test_finalise_record(self) -> None:
        """finalise_record freezes the record at the given start."""
        tracker = ForecastTracker()
        tracker.get_or_create_record(_slot_start(10), _slot_start(11))
        rec = tracker.find_record(_slot_start(10))
        assert rec is not None
        assert not rec.finalised
        tracker.finalise_record(_slot_start(10))
        assert rec.finalised

    def test_finalise_record_missing(self) -> None:
        """finalise_record returns False for a non-existent slot."""
        tracker = ForecastTracker()
        assert not tracker.finalise_record(_slot_start(10))

    def test_finalise_past_records(self) -> None:
        """finalise_past_records finalises all records with end <= now."""
        now = _slot_start(12)
        tracker = ForecastTracker()
        # Past slot
        tracker.get_or_create_record(_slot_start(9), _slot_start(10))
        # Current slot
        tracker.get_or_create_record(_slot_start(10), _slot_start(11))
        # Future slot
        tracker.get_or_create_record(_slot_start(13), _slot_start(14))

        count = tracker.finalise_past_records(now)
        assert (
            count == 2
        )  # slots 9-10 (ends 10:00) and 10-11 (ends 11:00) ended by 12:00
        assert tracker.find_record(_slot_start(9)).finalised  # type: ignore[union-attr]  # mock attribute on union type
        assert tracker.find_record(_slot_start(10)).finalised  # type: ignore[union-attr]  # mock attribute on union type
        assert not tracker.find_record(_slot_start(13)).finalised  # type: ignore[union-attr]  # mock attribute on union type

    def test_prune_exceeds_max(self) -> None:
        """Old records are pruned when the buffer exceeds max_slots."""
        tracker = ForecastTracker(max_slots=3)
        for i in range(5):
            tracker.get_or_create_record(_slot_start(10 + i), _slot_start(11 + i))
        assert len(tracker.records) == 3
        # Oldest two were pruned
        assert tracker.find_record(_slot_start(10)) is None
        assert tracker.find_record(_slot_start(11)) is None
        assert tracker.find_record(_slot_start(12)) is not None

    def test_set_forecasts(self) -> None:
        """set_forecasts stores the PV and load forecast."""
        tracker = ForecastTracker()
        tracker.get_or_create_record(_slot_start(10), _slot_start(11))
        assert tracker.set_forecasts(_slot_start(10), 4.0, 2.0)
        rec = tracker.find_record(_slot_start(10))
        assert rec is not None
        assert rec.forecast_pv_kwh == pytest.approx(4.0)
        assert rec.forecast_load_kwh == pytest.approx(2.0)

    def test_set_forecasts_no_record(self) -> None:
        """set_forecasts returns False when the record doesn't exist."""
        tracker = ForecastTracker()
        assert not tracker.set_forecasts(_slot_start(10), 4.0, 2.0)

    def test_set_forecasts_already_finalised(self) -> None:
        """set_forecasts returns False when the record is finalised."""
        tracker = ForecastTracker()
        tracker.get_or_create_record(_slot_start(10), _slot_start(11))
        tracker.finalise_record(_slot_start(10))
        assert not tracker.set_forecasts(_slot_start(10), 4.0, 2.0)


# ---------------------------------------------------------------------------
# ForecastTracker — summary computation
# ---------------------------------------------------------------------------


class _SummaryTracker(ForecastTracker):
    """Helper that creates and finalises records with given forecast/actual pairs."""

    def add(
        self,
        hour: int,
        forecast_pv: float,
        forecast_load: float,
        actual_pv: float,
        actual_load: float,
    ) -> None:
        start = _slot_start(hour)
        end = _slot_start(hour + 1)
        rec = self.get_or_create_record(start, end)
        assert self.set_forecasts(
            start,
            forecast_pv,
            forecast_load,
            raw_pv_kwh=forecast_pv,
        )
        rec.actual_pv_kwh = actual_pv
        rec.actual_load_kwh = actual_load
        rec.finalise()


class TestForecastTrackerSummary:
    """Tests for aggregate summary computation."""

    def test_empty_tracker(self) -> None:
        """Empty tracker returns empty summary."""
        tracker = ForecastTracker()
        s = tracker.summary
        assert s.finalised_count == 0
        assert s.window_slots == 0
        assert s.mae_pv_kwh == pytest.approx(0.0)
        assert s.mae_load_kwh == pytest.approx(0.0)
        assert s.bias_pv_kwh == pytest.approx(0.0)
        assert s.bias_load_kwh == pytest.approx(0.0)

    def test_exact_match(self) -> None:
        """Perfect forecasts produce zero error."""
        tracker = _SummaryTracker()
        tracker.add(
            10, forecast_pv=3.0, forecast_load=2.0, actual_pv=3.0, actual_load=2.0
        )
        s = tracker.summary
        assert s.finalised_count == 1
        assert s.mae_pv_kwh == pytest.approx(0.0)
        assert s.mae_load_kwh == pytest.approx(0.0)
        assert s.bias_pv_kwh == pytest.approx(0.0)
        assert s.bias_load_kwh == pytest.approx(0.0)
        assert s.rmse_pv_kwh == pytest.approx(0.0)
        assert s.rmse_load_kwh == pytest.approx(0.0)
        assert s.mape_pv_pct == pytest.approx(0.0)
        assert s.mape_load_pct == pytest.approx(0.0)

    def test_over_forecast(self) -> None:
        """Over-forecast (predicted > actual) shows positive bias."""
        tracker = _SummaryTracker()
        tracker.add(
            10, forecast_pv=5.0, forecast_load=3.0, actual_pv=4.0, actual_load=2.0
        )
        s = tracker.summary
        assert s.bias_pv_kwh == pytest.approx(1.0)  # 5 - 4
        assert s.bias_load_kwh == pytest.approx(1.0)  # 3 - 2
        assert s.mae_pv_kwh == pytest.approx(1.0)
        assert s.mae_load_kwh == pytest.approx(1.0)

    def test_under_forecast(self) -> None:
        """Under-forecast (predicted < actual) shows negative bias."""
        tracker = _SummaryTracker()
        tracker.add(
            10, forecast_pv=3.0, forecast_load=1.0, actual_pv=4.0, actual_load=2.0
        )
        s = tracker.summary
        assert s.bias_pv_kwh == pytest.approx(-1.0)  # 3 - 4
        assert s.bias_load_kwh == pytest.approx(-1.0)  # 1 - 2

    def test_mixed_forecasts(self) -> None:
        """Multiple slots produce averaged metrics."""
        tracker = _SummaryTracker()
        # Slot 1: over-forecast PV
        tracker.add(
            10, forecast_pv=5.0, forecast_load=2.0, actual_pv=4.0, actual_load=2.0
        )
        # Slot 2: under-forecast PV
        tracker.add(
            11, forecast_pv=3.0, forecast_load=2.0, actual_pv=4.0, actual_load=2.0
        )

        s = tracker.summary
        assert s.finalised_count == 2
        # MAE: (|5-4| + |3-4|) / 2 = 1.0
        assert s.mae_pv_kwh == pytest.approx(1.0)
        # Bias: (1 + -1) / 2 = 0.0 (cancels out)
        assert s.bias_pv_kwh == pytest.approx(0.0)
        # RMSE: sqrt((1^2 + (-1)^2) / 2) = 1.0
        assert s.rmse_pv_kwh == pytest.approx(1.0)

    def test_mape_pv_division_by_zero(self) -> None:
        """MAPE returns None when actual PV is zero for all slots."""
        tracker = _SummaryTracker()
        tracker.add(
            10, forecast_pv=1.0, forecast_load=2.0, actual_pv=0.0, actual_load=2.0
        )
        s = tracker.summary
        assert s.mape_pv_pct is None
        assert s.mape_load_pct == pytest.approx(0.0)

    def test_mape_load_division_by_zero(self) -> None:
        """MAPE returns None when actual load is zero for all slots."""
        tracker = _SummaryTracker()
        tracker.add(
            10, forecast_pv=1.0, forecast_load=1.0, actual_pv=1.0, actual_load=0.0
        )
        s = tracker.summary
        assert s.mape_pv_pct == pytest.approx(0.0)
        assert s.mape_load_pct is None

    def test_single_slot_mape(self) -> None:
        """Single slot MAPE computation is correct."""
        tracker = _SummaryTracker()
        # PV: |5-4|/4 = 0.25 = 25%
        # Load: |3-2|/2 = 0.50 = 50%
        tracker.add(
            10, forecast_pv=5.0, forecast_load=3.0, actual_pv=4.0, actual_load=2.0
        )
        s = tracker.summary
        assert s.mape_pv_pct == pytest.approx(25.0)
        assert s.mape_load_pct == pytest.approx(50.0)

    def test_summary_as_dict(self) -> None:
        """as_dict returns a JSON-safe dictionary."""
        tracker = _SummaryTracker()
        tracker.add(
            10, forecast_pv=5.0, forecast_load=3.0, actual_pv=4.0, actual_load=2.0
        )
        d = tracker.summary.as_dict()
        assert isinstance(d, dict)
        assert d["mae_pv_kwh"] == pytest.approx(1.0)
        assert d["finalised_slots"] == 1
        assert d["window_slots"] == 1
        assert d["mape_pv_pct"] == pytest.approx(25.0)
        assert d["mape_load_pct"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Integration-style: full lifecycle
# ---------------------------------------------------------------------------


class TestForecastTrackerIntegration:
    """Full lifecycle: accumulate, finalise, compute summary."""

    def test_full_cycle_single_slot(self) -> None:
        """Simulates a single slot: register forecasts, accumulate 4x, finalise."""
        tracker = ForecastTracker()

        # Register forecasts
        tracker.get_or_create_record(_slot_start(10), _slot_start(11))
        tracker.set_forecasts(_slot_start(10), pv_kwh=4.0, load_kwh=2.0)

        # Accumulate 4x over a 15-min slot (simulating 4 coordinator cycles)
        slot_energy_pv = compute_accumulated_energy(
            4000.0, 225.0
        )  # 4000W for 225s = 0.25 kWh each
        slot_energy_load = compute_accumulated_energy(
            2000.0, 225.0
        )  # 2000W for 225s = 0.125 kWh each

        for _ in range(4):
            rec = tracker.find_record(_slot_start(10))
            assert rec is not None
            rec.accumulate_pv(slot_energy_pv)
            rec.accumulate_load(slot_energy_load)

        # Finalise
        tracker.finalise_record(_slot_start(10))

        # Check
        s = tracker.summary
        assert s.finalised_count == 1
        # Actual: 4 * 0.25 = 1.0 kWh PV, 4 * 0.125 = 0.5 kWh load
        assert s.mae_pv_kwh == pytest.approx(abs(4.0 - 1.0))
        assert s.mae_load_kwh == pytest.approx(abs(2.0 - 0.5))

    def test_two_slots_over_forecast_and_under_forecast(self) -> None:
        """Two sequential slots: one over-forecast, one under-forecast."""
        tracker = ForecastTracker()

        # Slot 10-11: over-forecast PV
        tracker.get_or_create_record(_slot_start(10), _slot_start(11))
        tracker.set_forecasts(_slot_start(10), pv_kwh=5.0, load_kwh=2.0)
        rec = tracker.find_record(_slot_start(10))
        assert rec is not None
        rec.accumulate_pv(3.0)
        rec.accumulate_load(2.0)
        tracker.finalise_record(_slot_start(10))

        # Slot 11-12: exact match load, slight under-forecast PV
        tracker.get_or_create_record(_slot_start(11), _slot_start(12))
        tracker.set_forecasts(_slot_start(11), pv_kwh=2.0, load_kwh=3.0)
        rec = tracker.find_record(_slot_start(11))
        assert rec is not None
        rec.accumulate_pv(3.0)
        rec.accumulate_load(3.0)
        tracker.finalise_record(_slot_start(11))

        s = tracker.summary
        assert s.finalised_count == 2
        # PV MAE: (|5-3| + |2-3|) / 2 = (2 + 1) / 2 = 1.5
        assert s.mae_pv_kwh == pytest.approx(1.5)
        # PV bias: (2 + -1) / 2 = 0.5  (over-forecast dominates)
        assert s.bias_pv_kwh == pytest.approx(0.5)

    def test_finalise_past_records_updates_summary(self) -> None:
        """finalise_past_records updates the summary."""
        tracker = ForecastTracker()

        now = _slot_start(12)
        # Create a past slot
        tracker.get_or_create_record(_slot_start(9), _slot_start(10))
        tracker.set_forecasts(_slot_start(9), pv_kwh=4.0, load_kwh=2.0)
        rec = tracker.find_record(_slot_start(9))
        assert rec is not None
        rec.accumulate_pv(4.0)
        rec.accumulate_load(2.0)
        # Not yet finalised

        # Create a current slot
        tracker.get_or_create_record(_slot_start(10), _slot_start(11))
        tracker.set_forecasts(_slot_start(10), pv_kwh=3.0, load_kwh=1.0)
        rec = tracker.find_record(_slot_start(10))
        assert rec is not None
        rec.accumulate_pv(3.0)
        rec.accumulate_load(1.0)

        # Before finalise: empty summary
        assert tracker.summary.finalised_count == 0

        # Finalise past
        count = tracker.finalise_past_records(now)
        assert count == 2  # both slot 9-10 and 10-11 have ended by 12:00

        # After finalise: summary has 2 entries
        assert tracker.summary.finalised_count == 2
        assert tracker.summary.mae_pv_kwh == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Serialization (reboot persistence)
# ---------------------------------------------------------------------------


class TestForecastTrackerSerialization:
    """Tests for to_dict / from_dict / load_from_dict."""

    def test_forecast_slot_record_to_dict_empty(self) -> None:
        """An empty record serializes and deserializes correctly."""
        rec = ForecastSlotRecord(start=_slot_start(10), end=_slot_start(11))
        d = rec.to_dict()
        assert d["start"] == _slot_start(10).isoformat()
        assert d["end"] == _slot_start(11).isoformat()
        assert d["forecast_pv_kwh"] == pytest.approx(0.0)
        assert d["actual_pv_kwh"] == pytest.approx(0.0)
        assert d["finalised"] is False
        assert d["mae_pv"] is None

        restored = ForecastSlotRecord.from_dict(d)
        assert restored.start == rec.start
        assert restored.end == rec.end
        assert restored.forecast_pv_kwh == pytest.approx(0.0)
        assert restored.finalised is False
        assert restored.mae_pv is None

    def test_forecast_slot_record_to_dict_finalised(self) -> None:
        """A finalised record serializes and deserializes correctly."""
        rec = ForecastSlotRecord(
            start=_slot_start(10),
            end=_slot_start(11),
            forecast_pv_kwh=5.0,
            forecast_load_kwh=2.0,
            actual_pv_kwh=4.0,
            actual_load_kwh=2.5,
        )
        rec.finalise()
        d = rec.to_dict()
        assert d["finalised"] is True
        assert d["mae_pv"] == pytest.approx(1.0)  # |5-4|
        assert d["bias_pv"] == pytest.approx(1.0)  # 5-4

        restored = ForecastSlotRecord.from_dict(d)
        assert restored.finalised is True
        assert restored.mae_pv == pytest.approx(1.0)
        assert restored.bias_pv == pytest.approx(1.0)

    def test_tracker_to_dict_empty(self) -> None:
        """An empty tracker serializes to an empty record list."""
        tracker = ForecastTracker()
        d = tracker.to_dict()
        assert d == {"records": []}

    def test_tracker_to_dict_round_trip(self) -> None:
        """Full round-trip: populate, serialize, deserialize, verify summary."""
        original = ForecastTracker()

        # Add two finalised records
        for hour, fpv, fl, apv, al in [
            (10, 5.0, 2.0, 4.0, 2.0),
            (11, 3.0, 2.0, 4.0, 2.5),
        ]:
            original.get_or_create_record(_slot_start(hour), _slot_start(hour + 1))
            original.set_forecasts(_slot_start(hour), fpv, fl)
            rec = original.find_record(_slot_start(hour))
            assert rec is not None
            rec.accumulate_pv(apv)
            rec.accumulate_load(al)
            original.finalise_record(_slot_start(hour))

        original_summary = original.summary

        # Serialize
        data = original.to_dict()
        assert len(data["records"]) == 2

        # Deserialize into a fresh tracker
        restored = ForecastTracker()
        restored.load_from_dict(data)
        assert len(restored.records) == 2

        restored_summary = restored.summary
        assert restored_summary.finalised_count == original_summary.finalised_count
        assert restored_summary.mae_pv_kwh == pytest.approx(original_summary.mae_pv_kwh)
        assert restored_summary.bias_pv_kwh == pytest.approx(
            original_summary.bias_pv_kwh
        )
        assert restored_summary.mape_pv_pct == pytest.approx(
            original_summary.mape_pv_pct
        )

    def test_tracker_to_dict_unfinalised_records(self) -> None:
        """Unfinalised serialized records restore correctly (with zero bias)."""
        tracker = ForecastTracker()
        tracker.get_or_create_record(_slot_start(10), _slot_start(11))
        tracker.set_forecasts(
            _slot_start(10),
            4.0,
            2.0,
            forecast_soc_pct=78.0,
            forecast_action="charge",
        )
        # Accumulate but do NOT finalise

        data = tracker.to_dict()
        assert len(data["records"]) == 1
        assert data["records"][0]["finalised"] is False
        assert data["records"][0]["actual_pv_kwh"] == pytest.approx(0.0)

        restored = ForecastTracker()
        restored.load_from_dict(data)
        rec = restored.find_record(_slot_start(10))
        assert rec is not None
        assert rec.finalised is False
        assert rec.forecast_pv_kwh == pytest.approx(4.0)
        assert rec.forecast_soc_pct == pytest.approx(78.0)
        assert rec.forecast_action == "charge"
        assert rec.prediction_eligible is True

    def test_legacy_payload_is_restored_but_excluded_from_accuracy(self) -> None:
        """Pre-baseline-schema records must not train or skew metrics."""
        legacy_record = {
            "start": _slot_start(10).isoformat(),
            "end": _slot_start(11).isoformat(),
            "forecast_pv_kwh": 9.0,
            "forecast_load_kwh": 7.0,
            "actual_pv_kwh": 1.0,
            "actual_load_kwh": 2.0,
            "finalised": True,
            "mae_pv": 8.0,
            "mae_load": 5.0,
            "bias_pv": 8.0,
            "bias_load": 5.0,
        }
        tracker = ForecastTracker()

        tracker.load_from_dict({"records": [legacy_record]})

        rec = tracker.records[0]
        assert rec.forecast_pv_kwh == pytest.approx(9.0)
        assert rec.actual_pv_kwh == pytest.approx(1.0)
        assert rec.raw_forecast_pv_kwh is None
        assert rec.forecast_frozen is False
        assert rec.actual_coverage_seconds is None
        assert rec.accuracy_eligible is False
        assert rec.forecast_soc_pct is None
        assert rec.forecast_action is None
        assert rec.prediction_eligible is False
        assert tracker.summary.finalised_count == 0

    def test_persistence_prioritises_lifecycle_records_over_future_tail(
        self,
    ) -> None:
        """A long horizon keeps current/history and the nearest future slots."""
        tracker = ForecastTracker(max_slots=96)
        now = datetime(2026, 8, 20, 10, 5, tzinfo=UTC)

        final_start = now - timedelta(minutes=35)
        final_end = final_start + timedelta(minutes=15)
        final = tracker.get_or_create_record(final_start, final_end)
        assert tracker.set_forecasts(
            final_start,
            1.0,
            1.0,
            raw_pv_kwh=1.2,
            observed_at=final_start - timedelta(minutes=1),
        )
        tracker.freeze_forecasts(final_start)
        final.actual_coverage_seconds = 900.0
        final.finalise()

        active_start = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
        active = tracker.get_or_create_record(
            active_start,
            active_start + timedelta(minutes=15),
        )
        assert tracker.set_forecasts(
            active_start,
            2.0,
            1.0,
            raw_pv_kwh=2.2,
            observed_at=active_start - timedelta(minutes=1),
        )
        tracker.freeze_forecasts(now)
        assert active.forecast_frozen is True

        future_starts: list[datetime] = []
        for index in range(30):
            start = active.end + timedelta(minutes=15 * index)
            future_starts.append(start)
            tracker.get_or_create_record(start, start + timedelta(minutes=15))
            assert tracker.set_forecasts(
                start,
                3.0,
                1.0,
                raw_pv_kwh=3.2,
                observed_at=now,
            )

        payload = tracker.to_persistence_dict(now=now, max_records=24)
        persisted_starts = {
            datetime.fromisoformat(record["start"]) for record in payload["records"]
        }

        assert len(payload["records"]) == 24
        assert final_start in persisted_starts
        assert active_start in persisted_starts
        assert future_starts[0] in persisted_starts
        assert future_starts[-1] not in persisted_starts

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("forecast_pv_kwh", float("nan")),
            ("forecast_load_kwh", float("inf")),
            ("actual_pv_kwh", -1.0),
            ("actual_load_kwh", 1e308),
            ("raw_forecast_pv_kwh", 1e308),
            ("forecast_soc_pct", 101.0),
            ("actual_coverage_seconds", 902.0),
        ],
    )
    def test_restore_skips_nonfinite_or_out_of_bounds_records(
        self,
        field: str,
        value: float,
    ) -> None:
        """Malformed numerics cannot poison summaries or learned state."""
        record = ForecastSlotRecord(
            start=_slot_start(10),
            end=_slot_start(10, 15),
            forecast_pv_kwh=1.0,
            raw_forecast_pv_kwh=1.0,
            forecast_load_kwh=1.0,
            forecast_soc_pct=50.0,
            actual_pv_kwh=1.0,
            actual_load_kwh=1.0,
            forecast_frozen=True,
            actual_coverage_seconds=900.0,
        ).to_dict()
        record[field] = value
        tracker = ForecastTracker()

        tracker.load_from_dict({"records": [record]})

        assert tracker.records == []
        assert tracker.summary.finalised_count == 0

    def test_restore_sorts_deduplicates_and_bounds_after_validation(self) -> None:
        """Only the newest valid unique physical starts survive the bound."""
        valid = [
            ForecastSlotRecord(
                start=_slot_start(hour),
                end=_slot_start(hour + 1),
            ).to_dict()
            for hour in (13, 10, 12, 11)
        ]
        duplicate = dict(valid[0])
        duplicate["start"] = duplicate["start"].replace("+00:00", "Z")
        invalid = dict(valid[1])
        invalid["start"] = "2024-06-15T10:00:00"
        tracker = ForecastTracker(max_slots=2)

        tracker.load_from_dict(
            {"records": [invalid, *valid, duplicate, "not-a-record"]}
        )

        assert [record.start for record in tracker.records] == [
            _slot_start(12),
            _slot_start(13),
        ]

    def test_nearer_record_inserted_after_restore_preserves_order(self) -> None:
        """A newly available gap baseline is inserted by physical start."""
        farther = ForecastSlotRecord(
            start=_slot_start(12),
            end=_slot_start(13),
        )
        tracker = ForecastTracker()
        tracker.load_from_dict({"records": [farther.to_dict()]})

        tracker.get_or_create_record(_slot_start(11), _slot_start(12))

        assert [record.start for record in tracker.records] == [
            _slot_start(11),
            _slot_start(12),
        ]

    def test_every_restored_unfinalised_start_is_excluded(self) -> None:
        """Even a near-end active slot is unsafe for post-restart SoC scoring."""
        start = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
        record = ForecastSlotRecord(
            start=start,
            end=start + timedelta(minutes=15),
            forecast_pv_kwh=1.0,
            raw_forecast_pv_kwh=1.0,
            forecast_load_kwh=1.0,
            forecast_soc_pct=70.0,
            forecast_action="idle",
            forecast_frozen=True,
            actual_coverage_seconds=899.0,
        )
        tracker = ForecastTracker()

        tracker.load_from_dict({"records": [record.to_dict()]})

        assert tracker.restored_unfinalised_keys == {start}


class TestForecastLayoutReconciliation:
    """Live cadence changes use physical UTC layout identities."""

    @pytest.mark.parametrize(
        ("old_minutes", "new_minutes"),
        [(15, 60), (60, 15)],
    )
    def test_same_start_different_end_discards_only_unfinalised(
        self,
        old_minutes: int,
        new_minutes: int,
    ) -> None:
        start = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
        tracker = ForecastTracker()
        historical = tracker.get_or_create_record(
            start - timedelta(hours=2),
            start - timedelta(hours=1),
        )
        historical.finalise()
        tracker.get_or_create_record(
            start,
            start + timedelta(minutes=old_minutes),
        )

        changed = tracker.reconcile_unfinalised_layout(
            [(start, start + timedelta(minutes=new_minutes))],
            now=start - timedelta(minutes=1),
        )

        assert changed is True
        assert tracker.records == [historical]

    def test_dst_fold_and_spring_skip_layouts_do_not_false_trigger(self) -> None:
        stockholm = ZoneInfo("Europe/Stockholm")
        fold_starts = [
            datetime(2026, 10, 25, 2, 0, tzinfo=stockholm, fold=0),
            datetime(2026, 10, 25, 2, 0, tzinfo=stockholm, fold=1),
        ]
        spring_starts = [
            datetime(2026, 3, 29, 0, 45, tzinfo=UTC).astimezone(stockholm),
            datetime(2026, 3, 29, 1, 0, tzinfo=UTC).astimezone(stockholm),
        ]

        for starts in (fold_starts, spring_starts):
            tracker = ForecastTracker()
            expected: list[tuple[datetime, datetime]] = []
            for start in starts:
                end = (start.astimezone(UTC) + timedelta(minutes=15)).astimezone(
                    stockholm
                )
                tracker.get_or_create_record(start, end)
                expected.append((start, end))

            changed = tracker.reconcile_unfinalised_layout(
                expected,
                now=(starts[0].astimezone(UTC) - timedelta(minutes=1)),
            )

            assert changed is False
            assert len(tracker.records) == 2


class TestPhysicalIntervalAccumulation:
    """Actual power is allocated by physical overlap, never cycle timestamp."""

    @staticmethod
    def _add_frozen_slot(
        tracker: ForecastTracker,
        start: datetime,
        end: datetime,
    ) -> ForecastSlotRecord:
        rec = tracker.get_or_create_record(start, end)
        observed_at = start.astimezone(UTC) - timedelta(minutes=1)
        assert tracker.set_forecasts(
            start,
            pv_kwh=1.0,
            load_kwh=1.0,
            raw_pv_kwh=1.2,
            observed_at=observed_at,
        )
        assert tracker.freeze_forecasts(start) == 1
        return rec

    def test_boundary_crossing_is_split_between_slots(self) -> None:
        tracker = ForecastTracker()
        first = self._add_frozen_slot(
            tracker,
            _slot_start(10),
            _slot_start(10, 15),
        )
        second = self._add_frozen_slot(
            tracker,
            _slot_start(10, 15),
            _slot_start(10, 30),
        )

        assigned = tracker.accumulate_power_interval(
            _slot_start(10, 14) + timedelta(seconds=55),
            _slot_start(10, 15) + timedelta(seconds=5),
            pv_power_w=3600.0,
            load_power_w=7200.0,
            max_gap_seconds=1800.0,
        )

        assert assigned == pytest.approx(10.0)
        assert first.actual_pv_kwh == pytest.approx(0.005)
        assert second.actual_pv_kwh == pytest.approx(0.005)
        assert first.actual_load_kwh == pytest.approx(0.010)
        assert second.actual_load_kwh == pytest.approx(0.010)
        assert first.actual_coverage_seconds == pytest.approx(5.0)
        assert second.actual_coverage_seconds == pytest.approx(5.0)

    def test_multi_slot_gap_is_distributed_by_overlap(self) -> None:
        tracker = ForecastTracker()
        records = [
            self._add_frozen_slot(
                tracker,
                _slot_start(10, minute),
                _slot_start(10, minute + 15),
            )
            for minute in (0, 15, 30)
        ]

        assigned = tracker.accumulate_power_interval(
            _slot_start(10, 10),
            _slot_start(10, 35),
            pv_power_w=3600.0,
            load_power_w=7200.0,
            max_gap_seconds=1800.0,
        )

        assert assigned == pytest.approx(1500.0)
        assert [rec.actual_pv_kwh for rec in records] == pytest.approx([0.3, 0.9, 0.3])
        assert [rec.actual_load_kwh for rec in records] == pytest.approx(
            [0.6, 1.8, 0.6]
        )
        assert [rec.actual_coverage_seconds for rec in records] == pytest.approx(
            [300.0, 900.0, 300.0]
        )

    def test_gap_over_twice_five_minute_cadence_is_rejected(self) -> None:
        tracker = ForecastTracker()
        rec = self._add_frozen_slot(
            tracker,
            _slot_start(10),
            _slot_start(10, 15),
        )

        assigned = tracker.accumulate_power_interval(
            _slot_start(10),
            _slot_start(10, 10) + timedelta(seconds=1),
            pv_power_w=3600.0,
            load_power_w=7200.0,
            max_gap_seconds=600.0,
        )
        tracker.finalise_past_records(_slot_start(11))

        assert assigned == pytest.approx(0.0)
        assert rec.actual_pv_kwh == pytest.approx(0.0)
        assert rec.actual_load_kwh == pytest.approx(0.0)
        assert rec.actual_coverage_seconds == pytest.approx(0.0)
        assert rec.accuracy_eligible is False
        assert tracker.summary.finalised_count == 0

    def test_replans_update_future_baseline_but_live_injection_cannot(self) -> None:
        tracker = ForecastTracker()
        start = _slot_start(10)
        tracker.get_or_create_record(start, _slot_start(10, 15))

        assert tracker.set_forecasts(
            start,
            pv_kwh=4.0,
            load_kwh=2.0,
            raw_pv_kwh=5.0,
            forecast_soc_pct=80.0,
            forecast_action="charge",
            observed_at=_slot_start(9),
        )
        assert tracker.set_forecasts(
            start,
            pv_kwh=3.0,
            load_kwh=1.5,
            raw_pv_kwh=4.5,
            forecast_soc_pct=65.0,
            forecast_action="discharge",
            observed_at=_slot_start(9, 30),
        )
        assert tracker.freeze_forecasts(start) == 1

        assert not tracker.set_forecasts(
            start,
            pv_kwh=99.0,
            load_kwh=99.0,
            raw_pv_kwh=99.0,
            forecast_soc_pct=99.0,
            forecast_action="idle",
            observed_at=_slot_start(10) + timedelta(seconds=5),
        )
        rec = tracker.find_record(start)
        assert rec is not None
        assert rec.forecast_pv_kwh == pytest.approx(3.0)
        assert rec.raw_forecast_pv_kwh == pytest.approx(4.5)
        assert rec.forecast_load_kwh == pytest.approx(1.5)
        assert rec.forecast_soc_pct == pytest.approx(65.0)
        assert rec.forecast_action == "discharge"
        assert rec.prediction_eligible is False  # actual coverage is not complete yet

    def test_autumn_fold_slots_keep_distinct_physical_energy(self) -> None:
        stockholm = ZoneInfo("Europe/Stockholm")
        first_start = datetime(
            2026,
            10,
            25,
            2,
            45,
            tzinfo=stockholm,
            fold=0,
        )
        first_end = (first_start.astimezone(UTC) + timedelta(minutes=15)).astimezone(
            stockholm
        )
        second_start = datetime(
            2026,
            10,
            25,
            2,
            0,
            tzinfo=stockholm,
            fold=1,
        )
        second_end = (second_start.astimezone(UTC) + timedelta(minutes=15)).astimezone(
            stockholm
        )
        tracker = ForecastTracker()
        first = self._add_frozen_slot(tracker, first_start, first_end)
        second = self._add_frozen_slot(tracker, second_start, second_end)

        assigned = tracker.accumulate_power_interval(
            datetime(2026, 10, 25, 0, 55, tzinfo=UTC),
            datetime(2026, 10, 25, 1, 5, tzinfo=UTC),
            pv_power_w=1000.0,
            load_power_w=2000.0,
            max_gap_seconds=1800.0,
        )

        assert assigned == pytest.approx(600.0)
        assert first is not second
        assert first.actual_pv_kwh == pytest.approx(5.0 / 60.0)
        assert second.actual_pv_kwh == pytest.approx(5.0 / 60.0)
        assert first.actual_coverage_seconds == pytest.approx(300.0)
        assert second.actual_coverage_seconds == pytest.approx(300.0)

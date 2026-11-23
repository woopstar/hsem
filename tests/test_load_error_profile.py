"""Load forecast error history behind the pre-peak reserve (issue #1361).

The reserve is sized from how far the load forecast was off over a whole
window on earlier days.  These tests pin how that history is built from the
prediction records, how a window is compared with the same local hours of
earlier days, and how the profile survives a diagnostics dump.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.load_error_profile import (
    LOAD_ERROR_MAX_DAYS,
    LoadErrorProfile,
    contiguous_window_rmse_kwh,
    slot_records_window_rmse_kwh,
    window_rmse_kwh,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.prediction_record import PredictionRecord
from custom_components.hsem.utils.diagnostics import _planner_input_to_dict
from custom_components.hsem.utils.forecast_tracker import ForecastTracker
from custom_components.hsem.utils.prediction_tracker import PredictionTracker
from tests.backtest.replay import planner_input_from_dict

_TZ = ZoneInfo("Europe/Copenhagen")
_STEP = timedelta(minutes=15)
_START = datetime(2026, 11, 3, 15, 30, tzinfo=_TZ)


def _minute(value: datetime) -> int:
    """Return the UTC epoch minute of an aware datetime."""
    return int(value.astimezone(UTC).timestamp() // 60)


def _record(
    start: datetime, actual: float, forecast: float | None, predicted: float = 9.0
) -> PredictionRecord:
    """Return a prediction record of one finished slot."""
    return PredictionRecord(
        slot_start=start,
        predicted_soc_pct=50.0,
        actual_soc_pct=50.0,
        predicted_pv_kwh=0.0,
        actual_pv_kwh=0.0,
        predicted_load_kwh=predicted,
        actual_load_kwh=actual,
        action="idle",
        forecast_load_kwh=forecast,
    )


def _window(slots: int = 4, start: datetime = _START) -> list[datetime]:
    """Return the slot starts of a window."""
    return [start + _STEP * index for index in range(slots)]


class TestFromRecords:
    """The profile holds ``actual − forecast`` of the pre-start forecast."""

    def test_the_error_is_actual_minus_the_pre_start_forecast(self) -> None:
        """The live-injected ``predicted_load_kwh`` plays no part."""
        profile = LoadErrorProfile.from_records(
            [_record(_START, actual=0.7, forecast=0.5, predicted=0.69)]
        )

        assert profile.errors == {_minute(_START): pytest.approx(0.2)}

    def test_a_record_without_a_pre_start_forecast_is_left_out(self) -> None:
        """Records written before the field existed carry ``None``."""
        profile = LoadErrorProfile.from_records(
            [_record(_START, 0.7, None), _record(_START + _STEP, 0.4, 0.5)]
        )

        assert list(profile.errors) == [_minute(_START + _STEP)]
        assert LoadErrorProfile.from_records([]).errors == {}


class TestWindowSamples:
    """A window against the same local hours of earlier days."""

    def test_each_complete_earlier_day_is_one_summed_sample(self) -> None:
        """Day 1 sums to +0.4, day 2 to −0.8; day 3 misses a slot."""
        errors: dict[int, float] = {}
        for back, value in ((1, 0.1), (2, -0.2), (3, 0.3)):
            for start in _window():
                errors[_minute(start - timedelta(days=back))] = value
        del errors[_minute(_START - timedelta(days=3))]
        profile = LoadErrorProfile(errors=errors)

        samples = profile.window_samples(_window(), _TZ)

        assert samples == pytest.approx([0.4, -0.8])
        assert window_rmse_kwh(samples) == pytest.approx(math.sqrt(0.4))

    def test_no_window_and_no_history_give_no_sample(self) -> None:
        """Nothing to compare."""
        profile = LoadErrorProfile(errors={_minute(_START - timedelta(days=1)): 0.1})

        assert profile.window_samples([], _TZ) == []
        assert LoadErrorProfile().window_samples(_window(), _TZ) == []
        assert window_rmse_kwh([]) == pytest.approx(0.0)

    def test_history_older_than_the_limit_is_not_read(self) -> None:
        """Thirty earlier days at most."""
        old = _START - timedelta(days=LOAD_ERROR_MAX_DAYS + 1)
        profile = LoadErrorProfile(errors={_minute(old): 0.5})

        assert profile.window_samples([_START], _TZ) == []

    def test_the_zone_keeps_the_local_hours_across_a_dst_change(self) -> None:
        """The day before the clocks go back, 17:00 local is one hour earlier in UTC."""
        monday = datetime(2026, 10, 26, 17, 0, tzinfo=_TZ)
        saturday = datetime(2026, 10, 24, 17, 0, tzinfo=_TZ)
        profile = LoadErrorProfile(errors={_minute(saturday): 0.25})

        assert profile.window_samples([monday], _TZ) == pytest.approx([0.25])
        # Without the zone the step back is 48 hours: 16:00 local, no record.
        fixed = monday.astimezone(UTC)
        assert profile.window_samples([fixed]) == []


class TestContiguousWindows:
    """The RMSE of errors summed over every run of ``n`` slots."""

    def test_the_rmse_of_every_contiguous_window(self) -> None:
        """Windows 0-1 and 1-2 sum to 0.3 and 0.5; the gap ends the run."""
        errors = {0: 0.1, 15: 0.2, 30: 0.3, 60: 5.0}

        assert contiguous_window_rmse_kwh(errors, 2, 15) == pytest.approx(
            math.sqrt((0.09 + 0.25) / 2)
        )
        assert contiguous_window_rmse_kwh(errors, 3, 15) == pytest.approx(0.6)

    def test_fewer_than_n_consecutive_slots_give_zero(self) -> None:
        """Four slots in a row are needed for a four-slot window."""
        errors = {0: 0.1, 15: 0.2, 30: 0.3, 60: 5.0}

        assert contiguous_window_rmse_kwh(errors, 4, 15) == pytest.approx(0.0)
        assert contiguous_window_rmse_kwh(errors, 0, 15) == pytest.approx(0.0)
        assert contiguous_window_rmse_kwh(errors, 2, 0) == pytest.approx(0.0)
        assert slot_records_window_rmse_kwh([], 2) == pytest.approx(0.0)

    def test_correlated_errors_spread_more_than_the_square_root_rule(self) -> None:
        """Four slots that are off together: 4 × the slot error, not 2 ×."""
        errors = {index * 15: 0.2 for index in range(8)}

        assert contiguous_window_rmse_kwh(errors, 4, 15) == pytest.approx(0.8)


class TestForecastTrackerWindowRmse:
    """``ForecastTracker.window_rmse_load_kwh`` over its finalised records."""

    def _tracker(self, errors: list[float | None]) -> ForecastTracker:
        """Return a tracker with one finalised slot per error; ``None`` is a gap."""
        tracker = ForecastTracker()
        for index, error in enumerate(errors):
            if error is None:
                continue
            start = _START + _STEP * index
            record = tracker.get_or_create_record(start, start + _STEP)
            assert tracker.set_forecasts(start, 0.0, 1.0, raw_pv_kwh=0.0)
            record.actual_load_kwh = 1.0 + error
            record.finalise()
        return tracker

    def test_the_rmse_of_summed_errors_over_contiguous_windows(self) -> None:
        """Sums 0.3, 0.5 and 0.7 over three windows of two slots."""
        tracker = self._tracker([0.1, 0.2, 0.3, 0.4])

        assert tracker.window_rmse_load_kwh(2) == pytest.approx(
            math.sqrt((0.09 + 0.25 + 0.49) / 3)
        )
        assert tracker.window_rmse_load_kwh(4) == pytest.approx(1.0)

    def test_fewer_than_n_consecutive_finalised_records_give_zero(self) -> None:
        """A gap, an open slot and an empty tracker."""
        tracker = self._tracker([0.1, 0.2, None, 0.4])
        start = _START + _STEP * 4
        tracker.get_or_create_record(start, start + _STEP)

        assert tracker.window_rmse_load_kwh(3) == pytest.approx(0.0)
        assert tracker.window_rmse_load_kwh(2) == pytest.approx(0.3)
        assert ForecastTracker().window_rmse_load_kwh(1) == pytest.approx(0.0)


class TestSerialisation:
    """The profile in a diagnostics dump."""

    def test_runs_of_slots_become_one_string_each(self) -> None:
        """Two runs of 15-minute slots and a single slot."""
        profile = LoadErrorProfile(
            errors={0: 0.1, 15: -0.2, 30: 0.3, 90: 0.4, 105: 0.5, 300: 0.6}
        )

        data = profile.as_dict()

        assert data == {
            "version": 1,
            "runs": [
                {"start": 0, "step": 15, "values": "0.100,-0.200,0.300"},
                {"start": 90, "step": 15, "values": "0.400,0.500"},
                {"start": 300, "step": 0, "values": "0.600"},
            ],
        }
        assert LoadErrorProfile.from_dict(data).errors == pytest.approx(profile.errors)
        assert LoadErrorProfile().as_dict() == {"version": 1, "runs": []}

    def test_invalid_content_is_skipped(self) -> None:
        """Another version, a malformed run and a non-finite value."""
        assert LoadErrorProfile.from_dict({"version": 2, "runs": [{}]}).errors == {}
        assert LoadErrorProfile.from_dict({"version": 1, "runs": "x"}).errors == {}
        restored = LoadErrorProfile.from_dict(
            {
                "version": 1,
                "runs": [
                    "not a run",
                    {"start": "0", "step": 15, "values": "0.1"},
                    {"start": 0, "step": -15, "values": "0.1"},
                    {"start": 0, "step": 15, "values": 3},
                    {"start": 60, "step": 15, "values": "0.1,nan,x,5000,0.2"},
                ],
            }
        )

        assert restored.errors == pytest.approx({60: 0.1, 120: 0.2})

    def test_the_planner_input_round_trips_through_a_dump(self) -> None:
        """The dump and the replay shim agree on the profile."""
        profile = LoadErrorProfile(errors={0: 0.125, 15: -0.25, 60: 0.5})
        original = PlannerInput(load_reserve_enabled=True, load_error_profile=profile)

        raw = _planner_input_to_dict(original)
        rebuilt, report = planner_input_from_dict(
            {"planner_input": raw}, solver_time_limit_s=None
        )

        assert raw["load_error_profile"]["runs"][0]["values"] == "0.125,-0.250"
        assert report.is_faithful, report.describe()
        assert rebuilt.load_reserve_enabled is True
        assert rebuilt.load_error_profile == profile
        assert _planner_input_to_dict(PlannerInput())["load_error_profile"] is None


class TestPredictionHistory:
    """The pre-start forecast in the persisted prediction history."""

    def test_the_tracker_stores_and_restores_the_pre_start_forecast(self) -> None:
        """One record with a forecast, one written before the field existed."""
        tracker = PredictionTracker(max_records=10)
        tracker._warmup_slots = 0
        for index, forecast in enumerate((0.5, None)):
            assert tracker.add_record(
                predicted_soc=50.0,
                actual_soc=50.0,
                predicted_pv=0.0,
                actual_pv=0.0,
                predicted_load=0.6,
                actual_load=0.7,
                action="idle",
                slot_start=_START + _STEP * index,
                forecast_load=forecast,
            )

        data = tracker.to_persistence_dict()
        restored = PredictionTracker(max_records=10)
        restored.load_from_dict(data)

        assert [r.forecast_load_kwh for r in restored.records] == [
            pytest.approx(0.5),
            None,
        ]
        assert LoadErrorProfile.from_records(restored.records).errors == {
            _minute(_START): pytest.approx(0.2)
        }

    def test_an_old_or_invalid_value_restores_as_none(self) -> None:
        """A record without the key, and one with a negative forecast."""
        base = _record(_START, 0.7, 0.5).as_dict()
        old = {k: v for k, v in base.items() if k != "forecast_load_kwh"}
        bad = {**base, "forecast_load_kwh": -1.0}

        for data in (old, bad):
            record = PredictionRecord.from_dict(data)
            assert record is not None
            assert record.forecast_load_kwh is None
            assert record.actual_load_kwh == pytest.approx(0.7)

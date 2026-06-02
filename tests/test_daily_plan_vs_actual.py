"""Tests for the Daily Plan-vs-Actual tracking model and sensor."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime

import pytest

from custom_components.hsem.models.daily_plan_vs_actual import (
    DailyDiff,
    DailyMetrics,
    DailyPlanVsActualTracker,
    DailyRecord,
    DayRolloverResult,
)


class TestDailyMetrics:
    """Tests for :class:`DailyMetrics`."""

    def test_defaults_are_zero(self) -> None:
        """All fields default to 0.0."""
        m = DailyMetrics()
        assert m.grid_import_kwh == 0.0
        assert m.grid_import_cost == 0.0
        assert m.grid_export_kwh == 0.0
        assert m.grid_export_rev == 0.0
        assert m.battery_cycled_kwh == 0.0
        assert m.pv_produced_kwh == 0.0

    def test_as_dict_rounds(self) -> None:
        """as_dict returns rounded values."""
        m = DailyMetrics(
            grid_import_kwh=1.234567,
            grid_import_cost=2.345678,
        )
        d = m.as_dict()
        assert d["grid_import_kwh"] == 1.235
        assert d["grid_import_cost"] == 2.346

    def test_from_dict(self) -> None:
        """from_dict restores values correctly."""
        d = {"grid_import_kwh": 5.0, "grid_import_cost": 10.5}
        m = DailyMetrics.from_dict(d)
        assert m.grid_import_kwh == 5.0
        assert m.grid_import_cost == 10.5
        assert m.grid_export_kwh == 0.0  # missing key → default

    def test_roundtrip(self) -> None:
        """Dict roundtrip preserves values."""
        original = DailyMetrics(
            grid_import_kwh=1.1,
            grid_import_cost=2.2,
            grid_export_kwh=3.3,
            grid_export_rev=4.4,
            battery_cycled_kwh=5.5,
            pv_produced_kwh=6.6,
        )
        restored = DailyMetrics.from_dict(original.as_dict())
        assert restored.grid_import_kwh == pytest.approx(1.1)
        assert restored.grid_import_cost == pytest.approx(2.2)
        assert restored.grid_export_kwh == pytest.approx(3.3)
        assert restored.grid_export_rev == pytest.approx(4.4)
        assert restored.battery_cycled_kwh == pytest.approx(5.5)
        assert restored.pv_produced_kwh == pytest.approx(6.6)


class TestDailyDiff:
    """Tests for :class:`DailyDiff`."""

    def test_roundtrip(self) -> None:
        """Dict roundtrip preserves values."""
        original = DailyDiff(
            grid_import_kwh=1.0,
            grid_import_cost=2.0,
            grid_export_kwh=-1.0,
            grid_export_rev=-2.0,
            battery_cycled_kwh=0.5,
            pv_produced_kwh=-3.0,
            net_cost=5.0,
        )
        restored = DailyDiff.from_dict(original.as_dict())
        assert restored.grid_import_kwh == pytest.approx(1.0)
        assert restored.grid_import_cost == pytest.approx(2.0)
        assert restored.grid_export_kwh == pytest.approx(-1.0)
        assert restored.grid_export_rev == pytest.approx(-2.0)
        assert restored.battery_cycled_kwh == pytest.approx(0.5)
        assert restored.pv_produced_kwh == pytest.approx(-3.0)
        assert restored.net_cost == pytest.approx(5.0)


class TestDailyRecord:
    """Tests for :class:`DailyRecord`."""

    def test_net_cost_actual(self) -> None:
        """Net cost actual = import cost - export revenue."""
        record = DailyRecord(
            date="2026-06-01",
            actual=DailyMetrics(grid_import_cost=50.0, grid_export_rev=20.0),
        )
        assert record.net_cost_actual == pytest.approx(30.0)

    def test_net_cost_plan(self) -> None:
        """Net cost plan = import cost - export revenue."""
        record = DailyRecord(
            date="2026-06-01",
            plan=DailyMetrics(grid_import_cost=40.0, grid_export_rev=25.0),
        )
        assert record.net_cost_plan == pytest.approx(15.0)

    def test_compute_diff(self) -> None:
        """compute_diff sets all diff fields correctly."""
        record = DailyRecord(
            date="2026-06-01",
            actual=DailyMetrics(
                grid_import_kwh=10.0,
                grid_import_cost=20.0,
                grid_export_kwh=5.0,
                grid_export_rev=10.0,
                battery_cycled_kwh=3.0,
                pv_produced_kwh=15.0,
            ),
            plan=DailyMetrics(
                grid_import_kwh=8.0,
                grid_import_cost=16.0,
                grid_export_kwh=6.0,
                grid_export_rev=12.0,
                battery_cycled_kwh=2.0,
                pv_produced_kwh=18.0,
            ),
        )
        record.compute_diff()
        assert record.diff.grid_import_kwh == pytest.approx(2.0)
        assert record.diff.grid_import_cost == pytest.approx(4.0)
        assert record.diff.grid_export_kwh == pytest.approx(-1.0)
        assert record.diff.grid_export_rev == pytest.approx(-2.0)
        assert record.diff.battery_cycled_kwh == pytest.approx(1.0)
        assert record.diff.pv_produced_kwh == pytest.approx(-3.0)
        # Net cost actual = 20 - 10 = 10; plan = 16 - 12 = 4; diff = 6
        assert record.diff.net_cost == pytest.approx(6.0)

    def test_as_dict_includes_diff(self) -> None:
        """as_dict includes the computed diff."""
        record = DailyRecord(
            date="2026-06-01",
            actual=DailyMetrics(grid_import_kwh=1.0),
            plan=DailyMetrics(grid_import_kwh=0.5),
        )
        d = record.as_dict()
        assert d["date"] == "2026-06-01"
        assert d["actual"]["grid_import_kwh"] == 1.0
        assert d["plan"]["grid_import_kwh"] == 0.5
        assert d["diff"]["grid_import_kwh"] == 0.5

    def test_roundtrip(self) -> None:
        """Dict roundtrip preserves all values."""
        record = DailyRecord(
            date="2026-06-01",
            actual=DailyMetrics(
                grid_import_kwh=10.0,
                grid_import_cost=20.0,
                grid_export_kwh=5.0,
                grid_export_rev=10.0,
                battery_cycled_kwh=3.0,
                pv_produced_kwh=15.0,
            ),
            plan=DailyMetrics(
                grid_import_kwh=8.0,
                grid_import_cost=16.0,
            ),
        )
        record.compute_diff()
        restored = DailyRecord.from_dict(record.as_dict())
        assert restored.date == "2026-06-01"
        assert restored.actual.grid_import_kwh == pytest.approx(10.0)
        assert restored.actual.grid_import_cost == pytest.approx(20.0)
        assert restored.plan.grid_import_kwh == pytest.approx(8.0)
        assert restored.plan.grid_import_cost == pytest.approx(16.0)
        assert restored.diff.grid_import_kwh == pytest.approx(2.0)
        assert restored.diff.grid_import_cost == pytest.approx(4.0)


class TestDailyPlanVsActualTracker:
    """Tests for :class:`DailyPlanVsActualTracker`."""

    def test_init_sets_today(self) -> None:
        """Tracker sets today's date on initialisation."""
        tracker = DailyPlanVsActualTracker()
        assert tracker.today == date.today().isoformat()

    def test_accumulate_plan_adds_values(self) -> None:
        """accumulate_plan correctly sums values."""
        tracker = DailyPlanVsActualTracker()
        tracker.accumulate_plan(
            grid_import_kwh=2.0,
            grid_export_kwh=1.0,
            cycle_kwh=0.5,
            pv_kwh=3.0,
            import_price=0.5,
            export_price=0.3,
        )
        assert tracker.plan.grid_import_kwh == pytest.approx(2.0)
        assert tracker.plan.grid_import_cost == pytest.approx(1.0)  # 2 * 0.5
        assert tracker.plan.grid_export_kwh == pytest.approx(1.0)
        assert tracker.plan.grid_export_rev == pytest.approx(0.3)  # 1 * 0.3
        assert tracker.plan.battery_cycled_kwh == pytest.approx(0.5)
        assert tracker.plan.pv_produced_kwh == pytest.approx(3.0)

    def test_accumulate_plan_multiple_calls(self) -> None:
        """Multiple accumulate_plan calls sum correctly."""
        tracker = DailyPlanVsActualTracker()
        tracker.accumulate_plan(grid_import_kwh=1.0, import_price=0.5)
        tracker.accumulate_plan(grid_import_kwh=2.0, import_price=1.0)
        assert tracker.plan.grid_import_kwh == pytest.approx(3.0)
        assert tracker.plan.grid_import_cost == pytest.approx(2.5)  # 0.5 + 2.0

    def test_accumulate_actual_soc_tracking(self) -> None:
        """Battery cycle tracking uses SoC delta converted to kWh."""
        tracker = DailyPlanVsActualTracker()
        tracker.accumulate_actual(soc_pct=50.0, rated_capacity_kwh=10.0)
        assert tracker.last_soc_pct == 50.0
        # No delta yet — battery_cycled unchanged.
        assert tracker.actual.battery_cycled_kwh == 0.0

        tracker.accumulate_actual(soc_pct=55.0, rated_capacity_kwh=10.0)
        assert tracker.last_soc_pct == 55.0
        # Delta = |55 - 50| = 5 pct-points → 5 * 10 / 100 = 0.5 kWh
        assert tracker.actual.battery_cycled_kwh == pytest.approx(0.5)

    def test_accumulate_actual_soc_discharge(self) -> None:
        """SoC decrease is tracked as positive cycle kWh."""
        tracker = DailyPlanVsActualTracker()
        tracker.accumulate_actual(soc_pct=50.0, rated_capacity_kwh=10.0)
        tracker.accumulate_actual(soc_pct=45.0, rated_capacity_kwh=10.0)
        # Delta = |45 - 50| = 5 pct-points → 0.5 kWh
        assert tracker.actual.battery_cycled_kwh == pytest.approx(0.5)

    def test_check_day_rollover_no_change(self) -> None:
        """No rollover when day hasn't changed."""
        tracker = DailyPlanVsActualTracker(today="2026-06-01")
        result = tracker.check_day_rollover(datetime(2026, 6, 1, 12, 0, 0))
        assert result is None

    def test_check_day_rollover_changes(self) -> None:
        """Day rollover returns a result and resets counters."""
        tracker = DailyPlanVsActualTracker(today="2026-06-01")
        tracker.accumulate_plan(grid_import_kwh=5.0, import_price=1.0)
        tracker.accumulate_actual(soc_pct=50.0)

        result = tracker.check_day_rollover(datetime(2026, 6, 2, 0, 5, 0))
        assert result is not None
        assert isinstance(result, DayRolloverResult)
        assert result.record.date == "2026-06-01"

        # Counters should be reset.
        assert tracker.today == "2026-06-02"
        assert tracker.plan.grid_import_kwh == 0.0
        assert tracker.actual.battery_cycled_kwh == 0.0
        assert tracker.last_soc_pct is None

        # History should contain the saved record.
        assert len(tracker.history) == 1
        assert tracker.history[0].date == "2026-06-01"

    def test_get_today_record(self) -> None:
        """get_today_record returns current accumulator state."""
        tracker = DailyPlanVsActualTracker()
        tracker.accumulate_plan(grid_import_kwh=3.0, import_price=2.0)
        record = tracker.get_today_record()
        assert record.date == date.today().isoformat()
        assert record.plan.grid_import_kwh == pytest.approx(3.0)
        assert record.plan.grid_import_cost == pytest.approx(6.0)

    def test_get_yesterday_record(self) -> None:
        """get_yesterday_record returns the exact yesterday record."""
        from datetime import timedelta

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        tracker = DailyPlanVsActualTracker()
        record = DailyRecord(
            date=yesterday,
            actual=DailyMetrics(grid_import_kwh=10.0),
        )
        tracker.history = [record]
        result = tracker.get_yesterday_record()
        assert result is not None
        assert result.date == yesterday

    def test_get_yesterday_record_none_when_empty(self) -> None:
        """get_yesterday_record returns None when history is empty."""
        tracker = DailyPlanVsActualTracker()
        record = tracker.get_yesterday_record()
        assert record is None

    def test_get_yesterday_record_none_when_only_today(self) -> None:
        """get_yesterday_record returns None when history only has today."""
        today_str = date.today().isoformat()
        tracker = DailyPlanVsActualTracker()
        today_record = DailyRecord(date=today_str)
        tracker.history = [today_record]
        record = tracker.get_yesterday_record()
        assert record is None

    def test_history_pruning(self) -> None:
        """History is pruned to max_history_days."""
        tracker = DailyPlanVsActualTracker(max_history_days=3)
        for i in range(5):
            tracker._save_record_to_history(DailyRecord(date=f"2026-06-{i + 1:02d}"))
        assert len(tracker.history) == 3
        # Should keep the 3 most recent (June 3, 4, 5).
        assert tracker.history[-1].date == "2026-06-05"

    def test_as_sensor_attributes(self) -> None:
        """as_sensor_attributes returns expected structure."""
        tracker = DailyPlanVsActualTracker()
        tracker.accumulate_plan(grid_import_kwh=1.0, import_price=0.5)

        attrs = tracker.as_sensor_attributes()
        assert "today" in attrs
        assert "yesterday" in attrs
        assert "history" in attrs
        assert "history_file" in attrs
        assert "history_days" in attrs
        assert "history_total_days" in attrs
        assert attrs["history_days"] == 90
        assert attrs["history_total_days"] == 0

    def test_json_persistence_roundtrip(self) -> None:
        """Save and load history through a temp JSON file."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            # Create tracker, add records, save.
            tracker = DailyPlanVsActualTracker(
                history_file=tmp_path, max_history_days=90
            )
            tracker.accumulate_plan(
                grid_import_kwh=5.0,
                grid_export_kwh=2.0,
                import_price=1.0,
                export_price=0.5,
            )
            tracker.accumulate_actual(soc_pct=60.0)

            # Simulate day rollover to save.
            tracker.today = "2026-06-01"
            tracker.check_day_rollover(datetime(2026, 6, 2, 0, 5, 0))

            # Load from the file with a new tracker.
            tracker2 = DailyPlanVsActualTracker(
                history_file=tmp_path, max_history_days=90
            )
            assert len(tracker2.history) == 1
            assert tracker2.history[0].date == "2026-06-01"
            assert tracker2.history[0].plan.grid_import_kwh == pytest.approx(5.0)
            assert tracker2.history[0].plan.grid_export_kwh == pytest.approx(2.0)
            assert tracker2.history[0].plan.grid_import_cost == pytest.approx(5.0)
            assert tracker2.history[0].plan.grid_export_rev == pytest.approx(1.0)

            # Verify file is valid JSON.
            with open(tmp_path, encoding="utf-8") as f:
                data = json.load(f)
            assert "updated" in data
            assert "days" in data
            assert len(data["days"]) == 1
        finally:
            os.unlink(tmp_path)

    def test_corrupted_file_handling(self) -> None:
        """Tracker loads gracefully from a corrupted file."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tmp:
            tmp.write("this is not valid json")
            tmp_path = tmp.name

        try:
            tracker = DailyPlanVsActualTracker(
                history_file=tmp_path, max_history_days=90
            )
            # Should have loaded empty history despite corruption.
            assert tracker.history == []
        finally:
            os.unlink(tmp_path)

    def test_load_missing_file(self) -> None:
        """Tracker handles missing history file gracefully."""
        tracker = DailyPlanVsActualTracker(
            history_file="/tmp/nonexistent_hsem_history.json",
            max_history_days=90,
        )
        assert tracker.history == []

"""Tests for the FinancialTracker model."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from custom_components.hsem.models.financial_tracker import (
    FinancialDayEntry,
    FinancialTracker,
)


class TestFinancialDayEntry:
    """Tests for :class:`FinancialDayEntry`."""

    def test_defaults_are_zero(self) -> None:
        """All cost fields default to 0.0."""
        e = FinancialDayEntry(date="2026-06-26")
        assert e.import_cost == pytest.approx(0.0)
        assert e.export_income == pytest.approx(0.0)

    def test_as_dict_rounds(self) -> None:
        """as_dict returns rounded values."""
        e = FinancialDayEntry(
            date="2026-06-26", import_cost=1.234567, export_income=2.345678
        )
        d = e.as_dict()
        assert d["date"] == "2026-06-26"
        assert d["import_cost"] == pytest.approx(1.235)
        assert d["export_income"] == pytest.approx(2.346)

    def test_from_dict(self) -> None:
        """from_dict restores values correctly."""
        d = {"date": "2026-06-26", "import_cost": 5.0, "export_income": 10.5}
        e = FinancialDayEntry.from_dict(d)
        assert e.date == "2026-06-26"
        assert e.import_cost == pytest.approx(5.0)
        assert e.export_income == pytest.approx(10.5)

    def test_roundtrip(self) -> None:
        """Dict roundtrip preserves values."""
        original = FinancialDayEntry(
            date="2026-06-26", import_cost=1.1, export_income=2.2
        )
        restored = FinancialDayEntry.from_dict(original.as_dict())
        assert restored.date == original.date
        assert restored.import_cost == pytest.approx(1.1)
        assert restored.export_income == pytest.approx(2.2)


class TestFinancialTrackerAccumulation:
    """Tests for :class:`FinancialTracker` accumulation logic."""

    def test_accumulate_zero_delta_on_first_call(self) -> None:
        """First accumulate call sets baseline but adds zero cost."""
        tracker = FinancialTracker()
        tracker.accumulate(
            grid_import_energy_kwh=100.0,
            grid_export_energy_kwh=50.0,
            import_price=2.0,
            export_price=1.0,
        )
        # First call should not add any cost (no previous reading).
        assert tracker.import_cost_total == pytest.approx(0.0)
        assert tracker.export_income_total == pytest.approx(0.0)

    def test_accumulate_computes_delta(self) -> None:
        """Second accumulate call computes delta from first reading."""
        tracker = FinancialTracker()
        tracker.accumulate(
            grid_import_energy_kwh=100.0,
            grid_export_energy_kwh=50.0,
            import_price=2.0,
            export_price=1.0,
        )
        tracker.accumulate(
            grid_import_energy_kwh=105.0,
            grid_export_energy_kwh=55.0,
            import_price=3.0,
            export_price=1.5,
        )
        # Import: (105 - 100) * 3.0 = 15.0
        assert tracker.import_cost_total == pytest.approx(15.0)
        # Export: (55 - 50) * 1.5 = 7.5
        assert tracker.export_income_total == pytest.approx(7.5)

    def test_accumulate_ignores_negative_delta(self) -> None:
        """Negative delta (meter reset) is ignored."""
        tracker = FinancialTracker()
        tracker.accumulate(
            grid_import_energy_kwh=100.0,
            grid_export_energy_kwh=50.0,
            import_price=2.0,
            export_price=1.0,
        )
        tracker.accumulate(
            grid_import_energy_kwh=95.0,  # meter went backwards
            grid_export_energy_kwh=45.0,
            import_price=2.0,
            export_price=1.0,
        )
        # Should not accumulate negative deltas.
        assert tracker.import_cost_total == pytest.approx(0.0)
        assert tracker.export_income_total == pytest.approx(0.0)

    def test_accumulate_handles_none_inputs(self) -> None:
        """Accumulate handles None meter readings gracefully."""
        tracker = FinancialTracker()
        tracker.accumulate(
            grid_import_energy_kwh=None,
            grid_export_energy_kwh=None,
            import_price=2.0,
            export_price=1.0,
        )
        # Should not crash.
        assert tracker.import_cost_total == pytest.approx(0.0)
        assert tracker.export_income_total == pytest.approx(0.0)

    def test_accumulate_multiple_cycles(self) -> None:
        """Running totals grow monotonically over multiple cycles."""
        tracker = FinancialTracker()
        tracker.accumulate(
            grid_import_energy_kwh=100.0,
            grid_export_energy_kwh=50.0,
            import_price=2.0,
            export_price=1.0,
        )
        tracker.accumulate(
            grid_import_energy_kwh=110.0,
            grid_export_energy_kwh=60.0,
            import_price=2.5,
            export_price=1.2,
        )
        tracker.accumulate(
            grid_import_energy_kwh=115.0,
            grid_export_energy_kwh=65.0,
            import_price=3.0,
            export_price=1.5,
        )
        # Import: (110-100)*2.5 + (115-110)*3.0 = 25 + 15 = 40
        assert tracker.import_cost_total == pytest.approx(40.0)
        # Export: (60-50)*1.2 + (65-60)*1.5 = 12 + 7.5 = 19.5
        assert tracker.export_income_total == pytest.approx(19.5)


class TestFinancialTrackerDayRollover:
    """Tests for day rollover logic."""

    def test_check_day_rollover_snapshots_yesterday(self) -> None:
        """Day rollover snapshots previous day's totals into daily_log."""
        tracker = FinancialTracker()
        # Accumulate some data on "today"
        tracker.accumulate(
            grid_import_energy_kwh=100.0,
            grid_export_energy_kwh=50.0,
            import_price=2.0,
            export_price=1.0,
        )
        tracker.accumulate(
            grid_import_energy_kwh=110.0,
            grid_export_energy_kwh=60.0,
            import_price=2.0,
            export_price=1.0,
        )
        assert tracker.import_cost_total == pytest.approx(20.0)
        assert tracker.export_income_total == pytest.approx(10.0)

        # Rollover to tomorrow.
        tomorrow = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        tracker.check_day_rollover(tomorrow)

        # Yesterday's entry should be in the daily log.
        yesterday = date.today().isoformat()
        assert yesterday in tracker.daily_log
        entry = tracker.daily_log[yesterday]
        assert entry.import_cost == pytest.approx(20.0)
        assert entry.export_income == pytest.approx(10.0)

        # Today's baselines should be set to current totals.
        assert tracker._today_start_import_cost == pytest.approx(20.0)
        assert tracker._today_start_export_income == pytest.approx(10.0)

        # Today's period values should be zero (reset).
        assert tracker.import_cost_today == pytest.approx(0.0)
        assert tracker.export_income_today == pytest.approx(0.0)

    def test_check_day_rollover_noop_same_day(self) -> None:
        """check_day_rollover on same day is a no-op."""
        tracker = FinancialTracker()
        now = datetime.now()
        tracker.check_day_rollover(now)
        original_daily_log_len = len(tracker.daily_log)
        # Same day again — no new entry.
        tracker.check_day_rollover(now)
        assert len(tracker.daily_log) == original_daily_log_len

    def test_day_rollover_resets_today_values(self) -> None:
        """After rollover, today values reset to zero."""
        tracker = FinancialTracker()
        tracker.accumulate(
            grid_import_energy_kwh=100.0,
            grid_export_energy_kwh=50.0,
            import_price=2.0,
            export_price=1.0,
        )
        tracker.accumulate(
            grid_import_energy_kwh=110.0,
            grid_export_energy_kwh=60.0,
            import_price=2.0,
            export_price=1.0,
        )

        # Today should show the accumulated values.
        assert tracker.import_cost_today == pytest.approx(20.0)
        assert tracker.export_income_today == pytest.approx(10.0)

        # Roll over.
        tomorrow = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        tracker.check_day_rollover(tomorrow)

        # Today values should be zero for the new day.
        assert tracker.import_cost_today == pytest.approx(0.0)
        assert tracker.export_income_today == pytest.approx(0.0)

        # But totals persist.
        assert tracker.import_cost_total == pytest.approx(20.0)
        assert tracker.export_income_total == pytest.approx(10.0)


class TestFinancialTrackerPeriodRollups:
    """Tests for period rollup properties."""

    def _make_tracker_with_history(self) -> FinancialTracker:
        """Create a tracker with known daily_log entries."""
        tracker = FinancialTracker()
        today = date.today()

        # Add entries for the last 35 days.
        for offset in range(35):
            d = today - timedelta(days=offset)
            tracker.daily_log[d.isoformat()] = FinancialDayEntry(
                date=d.isoformat(),
                import_cost=10.0,
                export_income=5.0,
            )
        return tracker

    def test_sum_period_7_days(self) -> None:
        """_sum_period(7) sums the last 7 days."""
        tracker = self._make_tracker_with_history()
        result = tracker._sum_period(7)
        assert result["import_cost"] == pytest.approx(70.0)
        assert result["export_income"] == pytest.approx(35.0)
        assert result["net_balance"] == pytest.approx(-35.0)

    def test_sum_period_30_days(self) -> None:
        """_sum_period(30) sums the last 30 days."""
        tracker = self._make_tracker_with_history()
        result = tracker._sum_period(30)
        assert result["import_cost"] == pytest.approx(300.0)
        assert result["export_income"] == pytest.approx(150.0)
        assert result["net_balance"] == pytest.approx(-150.0)

    def test_sum_month(self) -> None:
        """_sum_month sums entries in the current month."""
        tracker = FinancialTracker()
        today = date.today()
        month_key = today.strftime("%Y-%m")
        tracker.daily_log[f"{month_key}-01"] = FinancialDayEntry(
            date=f"{month_key}-01", import_cost=10.0, export_income=5.0
        )
        tracker.daily_log[f"{month_key}-02"] = FinancialDayEntry(
            date=f"{month_key}-02", import_cost=20.0, export_income=10.0
        )
        # Entry from a different month should be excluded.
        other_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m-%d")
        tracker.daily_log[other_month] = FinancialDayEntry(
            date=other_month, import_cost=100.0, export_income=50.0
        )

        result = tracker._sum_month()
        assert result["import_cost"] == pytest.approx(30.0)
        assert result["export_income"] == pytest.approx(15.0)

    def test_sum_year(self) -> None:
        """_sum_year sums entries in the current year."""
        tracker = FinancialTracker()
        today = date.today()
        year_key = today.strftime("%Y")
        tracker.daily_log[f"{year_key}-01-01"] = FinancialDayEntry(
            date=f"{year_key}-01-01", import_cost=10.0, export_income=5.0
        )
        tracker.daily_log[f"{year_key}-06-15"] = FinancialDayEntry(
            date=f"{year_key}-06-15", import_cost=20.0, export_income=10.0
        )
        # Entry from a different year should be excluded.
        tracker.daily_log["2020-01-01"] = FinancialDayEntry(
            date="2020-01-01", import_cost=100.0, export_income=50.0
        )

        result = tracker._sum_year()
        assert result["import_cost"] == pytest.approx(30.0)
        assert result["export_income"] == pytest.approx(15.0)


class TestFinancialTrackerPersistence:
    """Tests for JSON persistence round-trip."""

    def test_roundtrip_empty(self) -> None:
        """Empty tracker survives round-trip."""
        original = FinancialTracker()
        restored = FinancialTracker.from_dict(original.as_dict())
        assert restored.import_cost_total == pytest.approx(0.0)
        assert restored.export_income_total == pytest.approx(0.0)
        assert restored.today == original.today
        assert len(restored.daily_log) == 0

    def test_roundtrip_with_data(self) -> None:
        """Tracker with accumulated data survives round-trip."""
        original = FinancialTracker()
        original.accumulate(
            grid_import_energy_kwh=100.0,
            grid_export_energy_kwh=50.0,
            import_price=2.0,
            export_price=1.0,
        )
        original.accumulate(
            grid_import_energy_kwh=110.0,
            grid_export_energy_kwh=60.0,
            import_price=2.0,
            export_price=1.0,
        )
        original.daily_log["2026-06-25"] = FinancialDayEntry(
            date="2026-06-25", import_cost=15.0, export_income=8.0
        )

        restored = FinancialTracker.from_dict(original.as_dict())
        assert restored.import_cost_total == pytest.approx(20.0)
        assert restored.export_income_total == pytest.approx(10.0)
        assert restored._today_start_import_cost == pytest.approx(0.0)
        assert restored._today_start_export_income == pytest.approx(0.0)
        assert restored._last_import_energy_kwh == pytest.approx(110.0)
        assert restored._last_export_energy_kwh == pytest.approx(60.0)
        assert "2026-06-25" in restored.daily_log
        assert restored.daily_log["2026-06-25"].import_cost == pytest.approx(15.0)
        assert restored.daily_log["2026-06-25"].export_income == pytest.approx(8.0)

    def test_roundtrip_with_none_meters(self) -> None:
        """Tracker with None meter readings survives round-trip."""
        original = FinancialTracker()
        # _last_import_energy_kwh is None by default.
        d = original.as_dict()
        assert d["_last_import_energy_kwh"] is None
        assert d["_last_export_energy_kwh"] is None

        restored = FinancialTracker.from_dict(d)
        assert restored._last_import_energy_kwh is None
        assert restored._last_export_energy_kwh is None


class TestFinancialTrackerSensorAttributes:
    """Tests for the sensor attributes export."""

    def test_as_sensor_attributes_structure(self) -> None:
        """Attributes include all expected keys and period rollups."""
        tracker = FinancialTracker()
        tracker.daily_log["2026-06-25"] = FinancialDayEntry(
            date="2026-06-25", import_cost=10.0, export_income=5.0
        )
        tracker.daily_log["2026-06-26"] = FinancialDayEntry(
            date="2026-06-26", import_cost=20.0, export_income=10.0
        )

        attrs = tracker.as_sensor_attributes()
        assert "today" in attrs
        assert "last_7_days" in attrs
        assert "last_30_days" in attrs
        assert "this_month" in attrs
        assert "this_year" in attrs
        assert "daily" in attrs

        today = attrs["today"]
        assert "import_cost" in today
        assert "export_income" in today
        assert "net_balance" in today

        daily = attrs["daily"]
        assert isinstance(daily, list)
        assert len(daily) == 2
        assert daily[0]["date"] == "2026-06-25"
        assert daily[1]["date"] == "2026-06-26"

    def test_net_balance_is_export_minus_import(self) -> None:
        """Net balance = export_income - import_cost."""
        tracker = FinancialTracker()
        tracker.accumulate(
            grid_import_energy_kwh=100.0,
            grid_export_energy_kwh=50.0,
            import_price=3.0,
            export_price=1.0,
        )
        tracker.accumulate(
            grid_import_energy_kwh=110.0,
            grid_export_energy_kwh=60.0,
            import_price=3.0,
            export_price=1.0,
        )
        # Import: 10 * 3 = 30, Export: 10 * 1 = 10
        attrs = tracker.as_sensor_attributes()
        assert attrs["today"]["net_balance"] == pytest.approx(-20.0)  # 10 - 30
        assert attrs["today"]["import_cost"] == pytest.approx(30.0)
        assert attrs["today"]["export_income"] == pytest.approx(10.0)

"""Regression tests for the bounded financial daily log (issue #1099 follow-up).

Bug
---
``FinancialTracker.check_day_rollover`` added one ``FinancialDayEntry`` per
day to ``daily_log`` and nothing ever removed them. The log, the history file
and the ``daily`` attribute of all three financial sensors grew forever.

Fix
---
The log is pruned to :data:`MAX_DAILY_LOG_DAYS` (366 — enough for the
``this_year`` rollup) on every rollover and on load, and the sensors' ``daily``
attribute publishes only the newest :data:`SENSOR_DAILY_DAYS` (90) entries.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.hsem.models.financial_tracker import (
    MAX_DAILY_LOG_DAYS,
    SENSOR_DAILY_DAYS,
    FinancialDayEntry,
    FinancialTracker,
)

_TODAY = date(2026, 9, 25)


def _entry(day: date, import_cost: float = 2.0) -> FinancialDayEntry:
    return FinancialDayEntry(
        date=day.isoformat(), import_cost=import_cost, export_income=1.0
    )


def _tracker_with_days(days: int, today: date = _TODAY) -> FinancialTracker:
    """Return a tracker whose log holds *days* consecutive days before *today*."""
    tracker = FinancialTracker(today=today.isoformat())
    for offset in range(1, days + 1):
        day = today - timedelta(days=offset)
        tracker.daily_log[day.isoformat()] = _entry(day)
    return tracker


class TestRolloverPrunesTheLog:
    """Each midnight rollover keeps the log bounded."""

    def test_a_year_of_rollovers_never_exceeds_the_cap(self) -> None:
        tracker = FinancialTracker(today=date(2025, 1, 1).isoformat())
        start = datetime(2025, 1, 2, 0, 5, tzinfo=UTC)

        for offset in range(3 * 365):
            tracker.check_day_rollover(start + timedelta(days=offset))
            assert len(tracker.daily_log) <= MAX_DAILY_LOG_DAYS

        assert len(tracker.daily_log) == MAX_DAILY_LOG_DAYS

    def test_oldest_entries_are_dropped_first(self) -> None:
        tracker = _tracker_with_days(MAX_DAILY_LOG_DAYS)

        tracker.check_day_rollover(
            datetime.combine(_TODAY + timedelta(days=1), datetime.min.time(), UTC)
        )

        oldest_kept = min(tracker.daily_log)
        assert (
            oldest_kept == (_TODAY - timedelta(days=MAX_DAILY_LOG_DAYS - 1)).isoformat()
        )
        assert _TODAY.isoformat() in tracker.daily_log

    def test_same_day_is_a_noop(self) -> None:
        tracker = _tracker_with_days(MAX_DAILY_LOG_DAYS + 50)

        tracker.check_day_rollover(datetime.combine(_TODAY, datetime.min.time(), UTC))

        assert len(tracker.daily_log) == MAX_DAILY_LOG_DAYS + 50


class TestLoadTrimsLegacyHistory:
    """History files written before the cap was added are trimmed on load."""

    def test_multi_year_file_is_trimmed(self) -> None:
        legacy = _tracker_with_days(3 * 365)

        restored = FinancialTracker.from_dict(legacy.as_dict())

        assert len(restored.daily_log) == MAX_DAILY_LOG_DAYS
        assert (
            min(restored.daily_log)
            == (_TODAY - timedelta(days=MAX_DAILY_LOG_DAYS)).isoformat()
        )

    def test_undated_or_malformed_keys_are_dropped(self) -> None:
        data = _tracker_with_days(3).as_dict()
        data["daily_log"].append({"date": "not-a-date", "import_cost": 9.0})

        restored = FinancialTracker.from_dict(data)

        assert "not-a-date" not in restored.daily_log
        assert len(restored.daily_log) == 3

    def test_short_history_is_untouched(self) -> None:
        legacy = _tracker_with_days(40)

        restored = FinancialTracker.from_dict(legacy.as_dict())

        assert restored.daily_log.keys() == legacy.daily_log.keys()


class TestRollupsSurviveThePrune:
    """The cap keeps every day the period rollups can reach."""

    def test_this_year_on_new_years_eve_is_complete(self) -> None:
        today = date(2028, 12, 31)  # leap year: 365 prior days in the year
        tracker = _tracker_with_days(3 * 365, today=today)
        tracker._prune_daily_log()

        assert tracker._sum_year()["import_cost"] == pytest.approx(365 * 2.0)

    def test_last_30_days_is_unchanged(self) -> None:
        tracker = _tracker_with_days(3 * 365)
        before = tracker._sum_period(30)

        tracker._prune_daily_log()

        assert tracker._sum_period(30) == pytest.approx(before)


class TestSensorDailyAttributeIsBounded:
    """The ``daily`` attribute publishes only the newest 90 days."""

    def test_daily_attribute_is_capped_and_newest_first_kept(self) -> None:
        tracker = _tracker_with_days(MAX_DAILY_LOG_DAYS)

        daily = tracker.as_sensor_attributes()["daily"]

        assert len(daily) == SENSOR_DAILY_DAYS
        assert daily[-1]["date"] == (_TODAY - timedelta(days=1)).isoformat()
        assert (
            daily[0]["date"] == (_TODAY - timedelta(days=SENSOR_DAILY_DAYS)).isoformat()
        )
        assert [d["date"] for d in daily] == sorted(d["date"] for d in daily)

    def test_history_file_keeps_the_full_year(self) -> None:
        tracker = _tracker_with_days(MAX_DAILY_LOG_DAYS)

        assert len(tracker.as_dict()["daily_log"]) == MAX_DAILY_LOG_DAYS

"""Tests for interval and schedule filtering with midnight rollover support."""

from datetime import datetime, time
from zoneinfo import ZoneInfo


from custom_components.hsem.utils.misc import (
    get_next_schedule_start_datetime,
    is_interval_within_schedule,
    time_in_schedule,
)

# Use UTC for consistent testing
UTC = ZoneInfo("UTC")


class TestTimeInSchedule:
    """Test time_in_schedule function for various schedule scenarios."""

    def test_normal_schedule_within_bounds(self):
        """Test time within a normal (non-midnight-crossing) schedule."""
        schedule_start = time(10, 0)
        schedule_end = time(15, 0)
        check_time = time(12, 0)

        assert time_in_schedule(check_time, schedule_start, schedule_end) is True

    def test_normal_schedule_at_start(self):
        """Test time exactly at schedule start."""
        schedule_start = time(10, 0)
        schedule_end = time(15, 0)
        check_time = time(10, 0)

        assert time_in_schedule(check_time, schedule_start, schedule_end) is True

    def test_normal_schedule_at_end(self):
        """Test time exactly at schedule end."""
        schedule_start = time(10, 0)
        schedule_end = time(15, 0)
        check_time = time(15, 0)

        assert time_in_schedule(check_time, schedule_start, schedule_end) is True

    def test_normal_schedule_before_start(self):
        """Test time before normal schedule start."""
        schedule_start = time(10, 0)
        schedule_end = time(15, 0)
        check_time = time(9, 0)

        assert time_in_schedule(check_time, schedule_start, schedule_end) is False

    def test_normal_schedule_after_end(self):
        """Test time after normal schedule end."""
        schedule_start = time(10, 0)
        schedule_end = time(15, 0)
        check_time = time(16, 0)

        assert time_in_schedule(check_time, schedule_start, schedule_end) is False

    def test_midnight_crossing_schedule_early_morning(self):
        """Test time in early morning part of midnight-crossing schedule (23:00-02:00)."""
        schedule_start = time(23, 0)
        schedule_end = time(2, 0)
        check_time = time(1, 0)

        assert time_in_schedule(check_time, schedule_start, schedule_end) is True

    def test_midnight_crossing_schedule_late_night(self):
        """Test time in late night part of midnight-crossing schedule (23:00-02:00)."""
        schedule_start = time(23, 0)
        schedule_end = time(2, 0)
        check_time = time(23, 30)

        assert time_in_schedule(check_time, schedule_start, schedule_end) is True

    def test_midnight_crossing_schedule_outside(self):
        """Test time outside midnight-crossing schedule (23:00-02:00)."""
        schedule_start = time(23, 0)
        schedule_end = time(2, 0)
        check_time = time(10, 0)

        assert time_in_schedule(check_time, schedule_start, schedule_end) is False

    def test_midnight_crossing_schedule_at_boundaries(self):
        """Test times at exact boundaries of midnight-crossing schedule."""
        schedule_start = time(23, 0)
        schedule_end = time(2, 0)

        assert time_in_schedule(time(23, 0), schedule_start, schedule_end) is True
        assert time_in_schedule(time(2, 0), schedule_start, schedule_end) is True

    def test_edge_case_22_to_03_crossing_midnight(self):
        """Test another midnight-crossing window (22:00-03:00)."""
        schedule_start = time(22, 0)
        schedule_end = time(3, 0)

        assert time_in_schedule(time(22, 30), schedule_start, schedule_end) is True
        assert time_in_schedule(time(0, 0), schedule_start, schedule_end) is True
        assert time_in_schedule(time(2, 59), schedule_start, schedule_end) is True
        assert time_in_schedule(time(21, 59), schedule_start, schedule_end) is False
        assert time_in_schedule(time(3, 1), schedule_start, schedule_end) is False


class TestIsIntervalWithinSchedule:
    """Test is_interval_within_schedule function for various interval/schedule combinations."""

    def test_interval_within_normal_schedule(self):
        """Test interval completely within a normal schedule."""
        interval_start = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
        interval_end = datetime(2026, 5, 11, 12, 15, tzinfo=UTC)
        schedule_start = time(10, 0)
        schedule_end = time(15, 0)

        assert (
            is_interval_within_schedule(
                interval_start, interval_end, schedule_start, schedule_end
            )
            is True
        )

    def test_interval_outside_normal_schedule(self):
        """Test interval completely outside a normal schedule."""
        interval_start = datetime(2026, 5, 11, 16, 0, tzinfo=UTC)
        interval_end = datetime(2026, 5, 11, 16, 15, tzinfo=UTC)
        schedule_start = time(10, 0)
        schedule_end = time(15, 0)

        assert (
            is_interval_within_schedule(
                interval_start, interval_end, schedule_start, schedule_end
            )
            is False
        )

    def test_late_night_interval_in_midnight_crossing_schedule(self):
        """Test late night interval within midnight-crossing schedule (23:00-02:00)."""
        # 23:15-23:30 should be within 23:00-02:00
        interval_start = datetime(2026, 5, 11, 23, 15, tzinfo=UTC)
        interval_end = datetime(2026, 5, 11, 23, 30, tzinfo=UTC)
        schedule_start = time(23, 0)
        schedule_end = time(2, 0)

        assert (
            is_interval_within_schedule(
                interval_start, interval_end, schedule_start, schedule_end
            )
            is True
        )

    def test_early_morning_interval_in_midnight_crossing_schedule(self):
        """Test early morning interval within midnight-crossing schedule (23:00-02:00)."""
        # 01:15-01:30 should be within 23:00-02:00
        interval_start = datetime(2026, 5, 12, 1, 15, tzinfo=UTC)
        interval_end = datetime(2026, 5, 12, 1, 30, tzinfo=UTC)
        schedule_start = time(23, 0)
        schedule_end = time(2, 0)

        assert (
            is_interval_within_schedule(
                interval_start, interval_end, schedule_start, schedule_end
            )
            is True
        )

    def test_daytime_interval_outside_midnight_crossing_schedule(self):
        """Test daytime interval outside midnight-crossing schedule (23:00-02:00)."""
        # 10:00-10:15 should be outside 23:00-02:00
        interval_start = datetime(2026, 5, 11, 10, 0, tzinfo=UTC)
        interval_end = datetime(2026, 5, 11, 10, 15, tzinfo=UTC)
        schedule_start = time(23, 0)
        schedule_end = time(2, 0)

        assert (
            is_interval_within_schedule(
                interval_start, interval_end, schedule_start, schedule_end
            )
            is False
        )

    def test_interval_crossing_midnight_within_schedule(self):
        """Test interval that crosses midnight within a midnight-crossing schedule."""
        # 23:45-00:15 should be within 23:00-02:00
        interval_start = datetime(2026, 5, 11, 23, 45, tzinfo=UTC)
        interval_end = datetime(2026, 5, 12, 0, 15, tzinfo=UTC)
        schedule_start = time(23, 0)
        schedule_end = time(2, 0)

        assert (
            is_interval_within_schedule(
                interval_start, interval_end, schedule_start, schedule_end
            )
            is True
        )

    def test_acceptance_criteria_23_to_02_window(self):
        """Test acceptance criteria: A window from 23:00 to 02:00 works."""
        # Create intervals within the 23:00-02:00 window
        schedule_start = time(23, 0)
        schedule_end = time(2, 0)

        # Late night interval
        late_night_start = datetime(2026, 5, 11, 23, 30, tzinfo=UTC)
        late_night_end = datetime(2026, 5, 11, 23, 45, tzinfo=UTC)
        assert (
            is_interval_within_schedule(
                late_night_start, late_night_end, schedule_start, schedule_end
            )
            is True
        )

        # Early morning interval
        early_morning_start = datetime(2026, 5, 12, 1, 15, tzinfo=UTC)
        early_morning_end = datetime(2026, 5, 12, 1, 30, tzinfo=UTC)
        assert (
            is_interval_within_schedule(
                early_morning_start, early_morning_end, schedule_start, schedule_end
            )
            is True
        )

    def test_acceptance_criteria_00_to_06_window(self):
        """Test acceptance criteria: A charge window from 00:00 to 06:00 works."""
        schedule_start = time(0, 0)
        schedule_end = time(6, 0)

        # Interval at 00:30-00:45
        interval_start = datetime(2026, 5, 12, 0, 30, tzinfo=UTC)
        interval_end = datetime(2026, 5, 12, 0, 45, tzinfo=UTC)
        assert (
            is_interval_within_schedule(
                interval_start, interval_end, schedule_start, schedule_end
            )
            is True
        )

        # Interval at 05:00-05:15
        interval_start = datetime(2026, 5, 12, 5, 0, tzinfo=UTC)
        interval_end = datetime(2026, 5, 12, 5, 15, tzinfo=UTC)
        assert (
            is_interval_within_schedule(
                interval_start, interval_end, schedule_start, schedule_end
            )
            is True
        )


class TestGetNextScheduleStartDatetime:
    """Test get_next_schedule_start_datetime function."""

    def test_schedule_start_in_future_today(self):
        """Test schedule start later today."""
        now = datetime(2026, 5, 11, 10, 0, tzinfo=UTC)
        schedule_start = time(15, 0)

        result = get_next_schedule_start_datetime(now, schedule_start)

        assert result == datetime(2026, 5, 11, 15, 0, tzinfo=UTC)

    def test_schedule_start_in_past_today_goes_to_tomorrow(self):
        """Test schedule start earlier today goes to tomorrow."""
        now = datetime(2026, 5, 11, 16, 0, tzinfo=UTC)
        schedule_start = time(15, 0)

        result = get_next_schedule_start_datetime(now, schedule_start)

        assert result == datetime(2026, 5, 12, 15, 0, tzinfo=UTC)

    def test_schedule_start_at_exactly_now(self):
        """Test schedule start at exactly current time."""
        now = datetime(2026, 5, 11, 15, 0, tzinfo=UTC)
        schedule_start = time(15, 0)

        result = get_next_schedule_start_datetime(now, schedule_start)

        # Should go to next day since candidate <= now
        assert result == datetime(2026, 5, 12, 15, 0, tzinfo=UTC)

    def test_midnight_crossing_schedule_late_evening(self):
        """Test midnight-crossing schedule (23:00) from late evening."""
        now = datetime(2026, 5, 11, 22, 0, tzinfo=UTC)
        schedule_start = time(23, 0)

        result = get_next_schedule_start_datetime(now, schedule_start)

        assert result == datetime(2026, 5, 11, 23, 0, tzinfo=UTC)

    def test_midnight_crossing_schedule_after_midnight(self):
        """Test midnight-crossing schedule (23:00) from early morning."""
        now = datetime(2026, 5, 12, 2, 0, tzinfo=UTC)
        schedule_start = time(23, 0)

        result = get_next_schedule_start_datetime(now, schedule_start)

        assert result == datetime(2026, 5, 12, 23, 0, tzinfo=UTC)

    def test_acceptance_criteria_midnight_charge_window(self):
        """Test acceptance criteria: Finding next occurrence of 00:00 start."""
        # At 23:59, next 00:00 is in 1 minute
        now = datetime(2026, 5, 11, 23, 59, tzinfo=UTC)
        schedule_start = time(0, 0)

        result = get_next_schedule_start_datetime(now, schedule_start)

        assert result == datetime(2026, 5, 12, 0, 0, tzinfo=UTC)

    def test_acceptance_criteria_23_to_02_discharge_window(self):
        """Test acceptance criteria: Finding next occurrence of 23:00 start."""
        # At 22:00, next 23:00 is today
        now = datetime(2026, 5, 11, 22, 0, tzinfo=UTC)
        schedule_start = time(23, 0)

        result = get_next_schedule_start_datetime(now, schedule_start)

        assert result == datetime(2026, 5, 11, 23, 0, tzinfo=UTC)

        # At 23:30, next 23:00 is tomorrow
        now = datetime(2026, 5, 11, 23, 30, tzinfo=UTC)
        result = get_next_schedule_start_datetime(now, schedule_start)

        assert result == datetime(2026, 5, 12, 23, 0, tzinfo=UTC)

    def test_preserves_timezone_info(self):
        """Test that timezone information is preserved."""
        from zoneinfo import ZoneInfo

        cest = ZoneInfo("Europe/Berlin")
        now = datetime(2026, 5, 11, 10, 0, tzinfo=cest)
        schedule_start = time(15, 0)

        result = get_next_schedule_start_datetime(now, schedule_start)

        # Result should maintain the timezone
        assert result.tzinfo == cest
        assert result == datetime(2026, 5, 11, 15, 0, tzinfo=cest)


class TestSameAndCrossMidnightWindows:
    """Test both same-day windows and cross-midnight windows together."""

    def test_same_day_window_filtered_correctly(self):
        """Test that same-day windows (e.g., 10:00-15:00) are filtered correctly."""
        schedule_start = time(10, 0)
        schedule_end = time(15, 0)

        # Inside window
        assert time_in_schedule(time(12, 0), schedule_start, schedule_end) is True

        # Outside window
        assert time_in_schedule(time(9, 0), schedule_start, schedule_end) is False
        assert time_in_schedule(time(16, 0), schedule_start, schedule_end) is False

    def test_cross_midnight_window_filtered_correctly(self):
        """Test that cross-midnight windows (e.g., 23:00-02:00) are filtered correctly."""
        schedule_start = time(23, 0)
        schedule_end = time(2, 0)

        # Inside window (late night part)
        assert time_in_schedule(time(23, 30), schedule_start, schedule_end) is True

        # Inside window (early morning part)
        assert time_in_schedule(time(1, 0), schedule_start, schedule_end) is True

        # Outside window
        assert time_in_schedule(time(10, 0), schedule_start, schedule_end) is False
        assert time_in_schedule(time(22, 59), schedule_start, schedule_end) is False
        assert time_in_schedule(time(2, 1), schedule_start, schedule_end) is False

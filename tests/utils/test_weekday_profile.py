"""Tests for WeekdayProfile weekday/weekend EWMA consumption profiles."""

from __future__ import annotations

import pytest

from custom_components.hsem.utils.weekday_profile import WeekdayProfile


class TestWeekdayProfile:
    """Unit tests for :class:`WeekdayProfile`."""

    def test_initial_values_are_zero(self) -> None:
        """A fresh profile must have all-zero weekday and weekend slots."""
        profile = WeekdayProfile()
        assert all(v == 0.0 for v in profile.weekday)
        assert all(v == 0.0 for v in profile.weekend)
        assert len(profile.weekday) == 24
        assert len(profile.weekend) == 24

    def test_update_weekday(self) -> None:
        """Updating with a Monday (dow=0) must change the weekday profile."""
        profile = WeekdayProfile()
        profile.update(dow=0, slot=12, value_kwh=2.0)

        expected = 0.0 * (1 - 0.15) + 2.0 * 0.15  # = 0.3
        assert profile.weekday[12] == pytest.approx(expected)
        assert profile.weekend[12] == 0.0  # weekend untouched

    def test_update_weekend(self) -> None:
        """Updating with a Saturday (dow=5) must change the weekend profile."""
        profile = WeekdayProfile()
        profile.update(dow=5, slot=18, value_kwh=1.5)

        expected = 0.0 * (1 - 0.15) + 1.5 * 0.15  # = 0.225
        assert profile.weekend[18] == pytest.approx(expected)
        assert profile.weekday[18] == 0.0  # weekday untouched

    def test_update_sunday_is_weekend(self) -> None:
        """Sunday (dow=6) is routed to the weekend profile."""
        profile = WeekdayProfile()
        profile.update(dow=6, slot=0, value_kwh=0.8)

        expected = 0.0 * (1 - 0.15) + 0.8 * 0.15  # = 0.12
        assert profile.weekend[0] == pytest.approx(expected)
        assert profile.weekday[0] == 0.0

    def test_update_friday_is_weekday(self) -> None:
        """Friday (dow=4) is routed to the weekday profile."""
        profile = WeekdayProfile()
        profile.update(dow=4, slot=23, value_kwh=3.0)

        expected = 0.0 * (1 - 0.15) + 3.0 * 0.15  # = 0.45
        assert profile.weekday[23] == pytest.approx(expected)
        assert profile.weekend[23] == 0.0

    def test_get_weekday_vs_weekend(self) -> None:
        """get() must return the correct profile based on day-of-week."""
        profile = WeekdayProfile()
        profile.weekday[10] = 1.2
        profile.weekend[10] = 0.8

        # Monday (dow=0) → weekday
        assert profile.get(dow=0, slot=10) == 1.2
        # Wednesday (dow=2) → weekday
        assert profile.get(dow=2, slot=10) == 1.2
        # Friday (dow=4) → weekday
        assert profile.get(dow=4, slot=10) == 1.2
        # Saturday (dow=5) → weekend
        assert profile.get(dow=5, slot=10) == 0.8
        # Sunday (dow=6) → weekend
        assert profile.get(dow=6, slot=10) == 0.8

    def test_get_out_of_range_slot_returns_zero(self) -> None:
        """Out-of-range slot indices must return 0.0."""
        profile = WeekdayProfile()
        assert profile.get(dow=0, slot=-1) == 0.0
        assert profile.get(dow=0, slot=24) == 0.0

    def test_ewma_convergence(self) -> None:
        """Repeated updates with the same value must converge to that value."""
        profile = WeekdayProfile()
        target = 1.5  # kWh

        # Apply 100 updates — the EWMA should be very close to target.
        for _ in range(100):
            profile.update(dow=0, slot=8, value_kwh=target)

        # After 100 steps: error ≈ (1-alpha)^100 ≈ 0.85^100 ≈ 8.7e-8
        assert profile.weekday[8] == pytest.approx(target, rel=1e-4)

    def test_ewma_smoothing(self) -> None:
        """EWMA must smooth step changes rather than jumping immediately."""
        profile = WeekdayProfile()

        # Seed the slot with an initial value.
        for _ in range(50):
            profile.update(dow=1, slot=14, value_kwh=1.0)

        # Single update to a new value.
        profile.update(dow=1, slot=14, value_kwh=2.0)

        # After 50 steps: old ≈ 0.9997. After one step with target 2.0:
        # new = old * 0.85 + 2.0 * 0.15 ≈ 0.9997 * 0.85 + 0.3 ≈ 1.1497
        # It should NOT jump to 2.0.
        assert profile.weekday[14] < 1.2  # well below the new target
        assert profile.weekday[14] > 1.0  # but above the old value

    def test_custom_alpha(self) -> None:
        """A profile with a different alpha must weight recent samples accordingly."""
        profile = WeekdayProfile(alpha=0.5)
        profile.update(dow=0, slot=6, value_kwh=4.0)

        # With alpha=0.5: new = 0 * 0.5 + 4.0 * 0.5 = 2.0
        assert profile.weekday[6] == pytest.approx(2.0)

    def test_default_instance_is_created(self) -> None:
        """Module-level singleton must be importable and functional."""
        from custom_components.hsem.utils.weekday_profile import (
            weekday_profile as wp,
        )

        assert wp is not None
        assert wp.slots_per_day == 24
        assert len(wp.weekday) == 24

    def test_update_isolation(self) -> None:
        """Updating one slot must not affect other slots."""
        profile = WeekdayProfile()
        profile.update(dow=0, slot=10, value_kwh=3.0)
        profile.update(dow=0, slot=11, value_kwh=4.0)

        # Slot 11 must differ from slot 10.
        assert profile.weekday[10] != profile.weekday[11]
        # Other slots remain 0.
        assert profile.weekday[0] == 0.0
        assert profile.weekday[23] == 0.0

    def test_weekday_weekend_isolation(self) -> None:
        """Weekday updates must never leak into the weekend profile."""
        profile = WeekdayProfile()
        for _ in range(10):
            profile.update(dow=2, slot=9, value_kwh=2.0)

        assert profile.weekday[9] > 0.0
        assert profile.weekend[9] == 0.0

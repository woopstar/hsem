"""Tests for the last known level shift of the price profile (issue #1452).

A price outage that outlasts the held prices leaves the planner without a
published price to shift the profile to.  The tracker keeps the shift of the
last cycle that had published prices, and the planner uses it for slots that
start within a day of the last published slot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.hsem.models.price_history_tracker import PriceHistoryTracker
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.price_profile import (
    LEVEL_SHIFT_MAX_AGE,
    PROFILE_MIN_ANCHOR_MINUTES,
    PriceProfile,
    level_from_row,
    level_shift_applies,
    median_shift,
)
from tests.test_price_history import (
    _MON,
    _MONDAY,
    _NOW,
    _OLD_LEVEL,
    _TUESDAY,
    _TZ,
    _WEDNESDAY,
    _attributes,
    _cycle,
    _history,
    _populated,
    copenhagen_ha_tz,
)

_UNTIL = _TUESDAY.replace(hour=12)

#: Home Assistant's local timezone is Copenhagen in every test here.
pytestmark = pytest.mark.usefixtures(copenhagen_ha_tz.__name__)


def _hourly_profile(**levels: Any) -> PriceProfile:
    """Return a profile that knows every hour of every weekday as 5.0 / 4.0."""
    return PriceProfile(
        any_day_hours={hour: (5.0, 4.0) for hour in range(24)}, **levels
    )


def _monday_prices(hours: range, price: float = 2.0) -> dict[datetime, float]:
    """Return an hourly price for the given hours of Monday."""
    return {_MONDAY.replace(hour=hour): price for hour in hours}


class TestMedianShift:
    """The shift between published prices and the profile."""

    def test_it_is_the_median_difference(self) -> None:
        known = [1.0, 2.0, 9.0, None, 3.0]
        shape = [0.5, 1.0, 1.0, 1.0, None]

        assert median_shift(known, shape, 120) == pytest.approx(1.0)

    def test_too_little_overlap_gives_none(self) -> None:
        slots = PROFILE_MIN_ANCHOR_MINUTES // 15 - 1

        assert median_shift([1.0] * slots, [0.5] * slots, 15) is None


class TestProfileLevel:
    """The level shift travels with the profile."""

    def test_the_shift_of_a_side_is_read_against_the_profile(self) -> None:
        profile = PriceProfile(any_day_hours={hour: (5.0, 4.0) for hour in range(4)})
        prices = _monday_prices(range(6))

        assert profile.level_shift(prices, 0, 60, _TZ) == pytest.approx(-3.0)
        assert profile.level_shift(prices, 1, 60, _TZ) == pytest.approx(-2.0)
        assert profile.level_shift(_monday_prices(range(3)), 0, 60, _TZ) is None

    def test_levels_survive_the_diagnostics_dump(self) -> None:
        profile = _hourly_profile(import_level=(-3.0, _UNTIL))

        restored = PriceProfile.from_dict(profile.as_dict())

        assert restored.import_level is not None
        assert restored.import_level[0] == pytest.approx(-3.0)
        assert restored.import_level[1] == _UNTIL
        assert restored.export_level is None
        assert profile.as_dict()["export_level"] is None

    @pytest.mark.parametrize(
        "row",
        [
            None,
            "x",
            [1.0],
            [1.0, 5],
            ["a", "2026-06-02T12:00:00+02:00"],
            [float("nan"), "2026-06-02T12:00:00+02:00"],
            [1.0, "not a time"],
            [1.0, "2026-06-02T12:00:00"],
        ],
    )
    def test_an_invalid_level_row_is_dropped(self, row: Any) -> None:
        assert level_from_row(row) is None


class TestFillWithTheLastLevel:
    """``populate_prices`` with no published price to shift the profile to."""

    def test_slots_within_a_day_keep_the_last_level(self) -> None:
        profile = _hourly_profile(import_level=(-3.0, _UNTIL))

        slots, missing = _populated([], profile, interval=60)

        assert len(missing) == 48
        for start, slot in slots.items():
            expected = 2.0 if start < _UNTIL else 5.0
            assert slot.price.import_price == pytest.approx(expected)
            # The export side has no level: the profile as recorded.
            assert slot.price.export_price == pytest.approx(4.0)

    def test_each_side_uses_its_own_level(self) -> None:
        profile = _hourly_profile(export_level=(1.5, _WEDNESDAY))

        slots, _missing = _populated([], profile, interval=60)

        assert slots[_TUESDAY].price.import_price == pytest.approx(5.0)
        assert slots[_TUESDAY].price.export_price == pytest.approx(5.5)

    def test_the_earlier_day_wins_over_the_last_level(self) -> None:
        """One profile hour is too little to shift to: Tuesday reads Monday."""
        profile = PriceProfile(
            any_day_hours={10: (5.0, 4.0)},
            import_level=(-3.0, _WEDNESDAY),
            export_level=(-3.0, _WEDNESDAY),
        )
        points = [
            PricePoint(
                hour=slot // 4,
                import_price=1.0,
                export_price=0.5,
                day_offset=0,
                slot_in_day=slot,
            )
            for slot in range(96)
        ]

        slots, _missing = _populated(points, profile)

        ten = slots[_TUESDAY.replace(hour=10)]
        assert ten.price.import_price == pytest.approx(1.0)
        assert ten.price.export_price == pytest.approx(0.5)

    def test_a_published_level_wins_over_the_last_level(self) -> None:
        profile = _hourly_profile(import_level=(-3.0, _WEDNESDAY))
        points = [
            PricePoint(
                hour=slot // 4,
                import_price=6.0,
                export_price=None,
                day_offset=0,
                slot_in_day=slot,
            )
            for slot in range(96)
        ]

        slots, _missing = _populated(points, profile)

        assert slots[_TUESDAY].price.import_price == pytest.approx(6.0)

    def test_a_slot_without_a_timezone_never_uses_the_level(self) -> None:
        naive = _TUESDAY.replace(tzinfo=None)

        assert level_shift_applies(_TUESDAY, _UNTIL) is True
        assert level_shift_applies(_UNTIL, _UNTIL) is False
        assert level_shift_applies(naive, _UNTIL) is False


class TestTrackerLevel:
    """The tracker keeps the shift of the last cycle with published prices."""

    def _tracker(self) -> PriceHistoryTracker:
        tracker = PriceHistoryTracker()
        for week in range(1, 7):
            for hour in range(24):
                tracker.record(
                    _MONDAY.replace(hour=hour) - timedelta(weeks=week), 60, 5.0, 4.0
                )
        tracker._dirty = False
        return tracker

    def test_a_thin_profile_keeps_no_level(self) -> None:
        tracker = PriceHistoryTracker()

        tracker.anchor_level("import", _monday_prices(range(24)), 60, _TZ)

        assert tracker.profile(_TZ).import_level is None
        assert tracker._dirty is False

    def test_the_level_is_valid_for_a_day_after_the_last_published_slot(
        self,
    ) -> None:
        tracker = self._tracker()

        tracker.anchor_level("import", _monday_prices(range(24)), 60, _TZ)
        tracker.anchor_level("export", _monday_prices(range(24), 1.5), 60, _TZ)

        profile = tracker.profile(_TZ)
        assert profile.lookup(_MON, 10, 0, 60) == pytest.approx((5.0, 4.0))
        assert profile.import_level is not None
        assert profile.export_level is not None
        assert profile.import_level[0] == pytest.approx(-3.0)
        assert profile.export_level[0] == pytest.approx(-2.5)
        assert profile.import_level[1] == _TUESDAY + LEVEL_SHIFT_MAX_AGE
        assert tracker._dirty is True

    def test_an_unchanged_level_does_not_rewrite_the_file(self) -> None:
        tracker = self._tracker()
        tracker.anchor_level("import", _monday_prices(range(24)), 60, _TZ)
        tracker._dirty = False

        tracker.anchor_level("import", _monday_prices(range(24)), 60, _TZ)
        assert tracker._dirty is False

        tracker.anchor_level("import", _monday_prices(range(24), 2.5), 60, _TZ)
        assert tracker._dirty is True
        level = tracker.profile(_TZ).import_level
        assert level is not None
        assert level[0] == pytest.approx(-2.5)

    def test_a_later_last_slot_moves_the_time_limit(self) -> None:
        tracker = self._tracker()
        tracker.anchor_level("import", _monday_prices(range(12)), 60, _TZ)
        tracker._dirty = False

        tracker.anchor_level("import", _monday_prices(range(24)), 60, _TZ)

        assert tracker._dirty is True

    def test_levels_are_persisted_and_invalid_rows_dropped(self) -> None:
        tracker = self._tracker()
        tracker.anchor_level("import", _monday_prices(range(24)), 60, _TZ)
        data = tracker.as_dict()
        data["levels"]["export"] = [1.0, "not a time"]

        loaded = PriceHistoryTracker.from_dict(data)
        restored = PriceHistoryTracker()
        restored.restore_from(loaded)

        level = restored.profile(_TZ).import_level
        assert level is not None
        assert level[0] == pytest.approx(-3.0)
        assert level[1] == _TUESDAY + LEVEL_SHIFT_MAX_AGE
        assert level[1].tzinfo is UTC
        assert restored.profile(_TZ).export_level is None

    def test_a_file_without_levels_loads(self) -> None:
        data = self._tracker().as_dict()
        del data["levels"]

        loaded = PriceHistoryTracker.from_dict(data)

        assert loaded.sample_count == 6 * 24
        assert loaded.profile(_TZ).import_level is None

    def test_another_price_source_drops_the_levels(self) -> None:
        tracker = self._tracker()
        tracker.source = {"import_entity": "sensor.a", "export_entity": "sensor.b"}
        tracker.anchor_level("import", _monday_prices(range(24)), 60, _TZ)

        assert tracker.set_source(
            {"import_entity": "sensor.c", "export_entity": "sensor.b"}
        )

        assert tracker.profile(_TZ).import_level is None


class TestCycleKeepsTheLevel:
    """The level comes from the real cycle, not from a hand-built profile."""

    def test_a_cycle_with_published_prices_anchors_both_sides(self) -> None:
        tracker = _history()

        planner_input, _result = _cycle(tracker, _NOW, _attributes(_MONDAY))

        profile = planner_input.price_profile
        assert profile is not None
        for level in (profile.import_level, profile.export_level):
            assert level is not None
            assert level[0] == pytest.approx(-_OLD_LEVEL)
            assert level[1] == _WEDNESDAY

    def test_an_outage_cycle_keeps_the_level_it_had(self) -> None:
        tracker = _history()
        _cycle(tracker, _NOW, _attributes(_MONDAY))
        before = tracker.profile(_TZ).import_level

        _cycle(tracker, _TUESDAY.replace(hour=9, minute=5), {})

        assert tracker.profile(_TZ).import_level == before

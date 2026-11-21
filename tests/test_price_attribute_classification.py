"""Tests for telling published prices from predictions (issue #1452).

The attributes ``prices``, ``prices_today``, ``prices_tomorrow`` and ``data``
hold published prices in one integration and predictions in another
(``docs/price-attributes.md``).  The populator reads the sensor's own state
to decide, and only published prices are recorded and held.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.hsem.coordinator_builder import (
    generate_recommendation_intervals,
)
from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    _entry_start,
    _published_until,
    populate_price_and_solcast_from_snapshot,
)
from custom_components.hsem.models.forecast_coverage import ForecastCoverage
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.price_history_tracker import PriceHistoryTracker
from custom_components.hsem.models.state_snapshot import StateSnapshot
from tests.test_price_history import (
    _EXPORT,
    _IMPORT,
    _MONDAY,
    _NOW,
    _TUESDAY,
    _WEDNESDAY,
    _attributes,
    _cfg,
    _clock_price,
    _cycle,
    _day_points,
    copenhagen_ha_tz,
)

_THURSDAY = _WEDNESDAY + timedelta(days=1)

#: Home Assistant's local timezone is Copenhagen in every test here.
pytestmark = pytest.mark.usefixtures(copenhagen_ha_tz.__name__)


def _points(*days: datetime, time_key: str = "start", value_key: str = "price") -> Any:
    """Return price entries for *days* under the given keys."""
    return [
        {time_key: point["start"], value_key: point["price"]}
        for day in days
        for point in _day_points(day)
    ]


def _coverage(import_attributes: dict[str, Any]) -> tuple[ForecastCoverage, list[Any]]:
    """Populate one cycle at Monday 09:05 with the given import sensor."""
    attributes = _attributes(_MONDAY)
    attributes[_IMPORT] = import_attributes
    with patch("homeassistant.util.dt.now", return_value=_NOW):
        recs = generate_recommendation_intervals(15, 48)
        coverage = populate_price_and_solcast_from_snapshot(
            recs, StateSnapshot(live=LiveState(), sensor_attributes=attributes), _cfg()
        )
    assert isinstance(coverage, ForecastCoverage)
    return coverage, recs


class TestPublishedOrPredicted:
    """What the price sensor itself published this cycle."""

    def test_stromligning_today_prices_are_published(self) -> None:
        coverage, _recs = _coverage({"prices": _points(_MONDAY)})

        assert len(coverage.published_import) == 96
        assert coverage.import_predictions_only is False

    def test_stromligning_forecast_data_is_a_prediction(self) -> None:
        """The tomorrow sensor holds a forecast until tomorrow is published."""
        coverage, recs = _coverage({"prices": _points(_TUESDAY), "forecast_data": True})

        assert coverage.published_import == {}
        assert coverage.import_predictions_only is True
        tuesday = next(rec for rec in recs if rec.start == _TUESDAY)
        assert tuesday.import_price == pytest.approx(_clock_price(0, 0))

    def test_stromligning_published_tomorrow_is_published(self) -> None:
        coverage, _recs = _coverage(
            {"prices": _points(_TUESDAY), "forecast_data": False}
        )

        assert len(coverage.published_import) == 96

    @pytest.mark.parametrize(
        "known_until",
        [_MONDAY.replace(hour=12).isoformat(), _MONDAY.replace(hour=12)],
    )
    def test_open_spot_forecast_is_published_up_to_known_until(
        self, known_until: Any
    ) -> None:
        coverage, recs = _coverage(
            {
                "prices_today": _points(_MONDAY),
                "prices_tomorrow": _points(_TUESDAY),
                "known_until": known_until,
            }
        )

        assert len(coverage.published_import) == 48
        assert max(coverage.published_import) == datetime(2026, 6, 1, 9, 45, tzinfo=UTC)
        assert all(rec.import_price > 0 for rec in recs)

    def test_an_unreadable_known_until_publishes_nothing(self) -> None:
        coverage, _recs = _coverage(
            {"prices_today": _points(_MONDAY), "known_until": "soon"}
        )

        assert coverage.published_import == {}

    def test_entso_e_days_are_published(self) -> None:
        coverage, _recs = _coverage(
            {
                "prices_today": _points(_MONDAY, time_key="time"),
                "prices_tomorrow": _points(_TUESDAY, time_key="time"),
            }
        )

        assert len(coverage.published_import) == 192

    def test_epex_spot_day_ahead_data_is_published(self) -> None:
        coverage, _recs = _coverage(
            {
                "data": _points(
                    _MONDAY,
                    _TUESDAY,
                    time_key="start_time",
                    value_key="price_per_kwh",
                )
            }
        )

        assert len(coverage.published_import) == 192

    def test_a_series_that_reaches_past_tomorrow_is_a_prediction(self) -> None:
        """EPEX Spot with the Energyforecast source: 96 hours, unmarked."""
        coverage, recs = _coverage(
            {
                "data": _points(
                    _MONDAY,
                    _TUESDAY,
                    _WEDNESDAY,
                    _THURSDAY,
                    time_key="start_time",
                    value_key="price_per_kwh",
                )
            }
        )

        assert coverage.published_import == {}
        assert coverage.import_predictions_only is True
        assert len(coverage.import_price) == 192
        assert all(rec.import_price > 0 for rec in recs)

    def test_a_market_day_in_another_time_zone_is_still_published(self) -> None:
        """Entries may run a few hours into the day after tomorrow."""
        entries = _points(_MONDAY, _TUESDAY) + _points(_WEDNESDAY)[:8]

        coverage, _recs = _coverage({"prices": entries})

        assert len(coverage.published_import) == 192

    def test_the_other_side_is_classified_on_its_own(self) -> None:
        coverage, _recs = _coverage({"prices": _points(_MONDAY), "forecast_data": True})

        assert coverage.published_import == {}
        assert len(coverage.published_export) == 96


class TestRules:
    """The classification rules one by one."""

    _FIRST = _MONDAY.astimezone(UTC)

    def test_a_prediction_attribute_is_never_published(self) -> None:
        assert _published_until("forecast", {}, [self._FIRST], self._FIRST) is None

    def test_a_plain_series_is_published_without_limit(self) -> None:
        until = _published_until("prices", {}, [self._FIRST], self._FIRST)

        assert until is not None
        assert until.year == 9999

    def test_an_entry_time_is_floored_to_its_interval(self) -> None:
        raw = _MONDAY.replace(hour=10, minute=17).isoformat()

        assert _entry_start(raw, 15) == datetime(2026, 6, 1, 8, 15, tzinfo=UTC)

    @pytest.mark.parametrize("raw", [None, "", "not a time"])
    def test_an_unreadable_entry_time_is_skipped(self, raw: Any) -> None:
        assert _entry_start(raw, 15) is None

    def test_an_invalid_interval_gives_no_time(self) -> None:
        assert _entry_start(_MONDAY, 0) is None


class TestNeverRecordedOrHeld:
    """A prediction reaches neither the history nor the held prices."""

    def _four_days(self) -> dict[str, dict[str, Any]]:
        attributes = _attributes(_MONDAY)
        attributes[_IMPORT] = {
            "data": _points(
                _MONDAY,
                _TUESDAY,
                _WEDNESDAY,
                _THURSDAY,
                time_key="start_time",
                value_key="price_per_kwh",
            )
        }
        return attributes

    def test_a_forecast_series_is_not_recorded_or_held(self) -> None:
        tracker = PriceHistoryTracker()

        planner_input, result = _cycle(tracker, _NOW, self._four_days())

        assert result.outage_reason is None
        assert tracker.sample_count == 0
        assert tracker.held_price("import", _MONDAY) is None
        assert tracker.held_price("export", _MONDAY) is not None
        assert len(planner_input.price_points) == 96

    def test_stromligning_forecast_for_tomorrow_is_not_held(self) -> None:
        tracker = PriceHistoryTracker()
        attributes = _attributes(_MONDAY)
        attributes[_EXPORT] = {
            "prices": _points(_MONDAY, _TUESDAY),
            "forecast_data": True,
        }

        _cycle(tracker, _NOW, attributes)

        assert tracker.held_price("import", _MONDAY) is not None
        assert tracker.held_price("export", _MONDAY) is None
        assert tracker.held_price("export", _TUESDAY) is None
        assert tracker.sample_count == 0

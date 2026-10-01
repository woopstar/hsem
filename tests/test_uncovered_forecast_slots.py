"""Tests for issue #1217 — slots without a published price are missing, not 0.0.

``generate_recommendation_intervals`` creates every slot with
``import_price = export_price = 0.0`` and the populator only writes the slots
a source point covers.  ``build_planner_input`` then emitted a ``PricePoint``
for **every** slot, so the planner could not tell "no data" from "zero".
Every morning, before the day-ahead prices publish, tomorrow was planned as
free import and worthless export, and the diagnostics said the data was
complete.

This is the 6.3.x port of two ``main`` fixes: the estimate of issue #1002
(same-hour price from the nearest earlier day) and the coverage of issue #1196
(the populator reports which slots each price source covered and the builder
leaves the others out, so the estimate is reached on the production path).
PV coverage is not part of the port.

Every test goes through the real populator, ``build_planner_input`` and
``run_planner``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

import homeassistant.util.dt as dt_util

from custom_components.hsem.coordinator_builder import (
    build_planner_input,
    generate_recommendation_intervals,
)
from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    populate_price_and_solcast_from_snapshot,
)
from custom_components.hsem.models.forecast_coverage import ForecastCoverage
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.utils.datetime_utils import utc_key
from tests.test_dynamic_floor_reference_plan import (
    _BUILDER,
    _CYCLE,
    _NOW as _FIXTURE_NOW,
    _PHASE,
    _coordinator,
    _plan,
    _populate_consumption,
    _snapshot,
)

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2026, 6, 1, 9, 5, tzinfo=_TZ)
_MIDNIGHT = _NOW.replace(hour=0, minute=0)
_TOMORROW = _MIDNIGHT + timedelta(days=1)
_DAY2 = _MIDNIGHT + timedelta(days=2)
_IMPORT = "sensor.import_price"
_EXPORT = "sensor.export_price"
_PV_TODAY = "sensor.solcast_today"


@pytest.fixture(autouse=True)
def copenhagen_ha_tz() -> Iterator[None]:
    """Run each test with Home Assistant's local timezone set to Copenhagen."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(_TZ)
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


def _import_price(hour: int) -> float:
    """Return a price that identifies its hour: 1.00, 1.01, ... 1.23."""
    return round(1.0 + hour / 100.0, 2)


def _prices(
    day_start: datetime, *, offset: float = 0.0, step_minutes: int = 60
) -> dict[str, Any]:
    """Return a price sensor's attributes covering one local day."""
    points = 24 * 60 // step_minutes
    return {
        "prices_today": [
            {
                "start": (day_start + timedelta(minutes=step_minutes * i)).isoformat(),
                "price": round(_import_price(i * step_minutes // 60) + offset, 5),
            }
            for i in range(points)
        ]
    }


def _cfg(horizon_hours: int = 48) -> SensorConfig:
    cfg = SensorConfig()
    cfg.recommendation_interval_minutes = 15
    cfg.recommendation_interval_length = horizon_hours
    cfg.electricity_price_update_interval = 60
    cfg.import_electricity_price_sensor = _IMPORT
    cfg.export_electricity_price_sensor = _EXPORT
    cfg.solcast_pv_forecast_forecast_likelihood = "pv_estimate"
    cfg.solcast_pv_forecast_forecast_today = _PV_TODAY
    return cfg


def _run(
    cfg: SensorConfig, sensor_attributes: dict[str, dict[str, Any]]
) -> tuple[list[HourlyRecommendation], ForecastCoverage, PlannerInput, PlannerOutput]:
    """Populate, build and plan exactly as the coordinator does."""
    with patch("homeassistant.util.dt.now", return_value=_NOW):
        recs = generate_recommendation_intervals(
            15, int(cfg.recommendation_interval_length)
        )
        coverage = populate_price_and_solcast_from_snapshot(
            recs,
            StateSnapshot(live=LiveState(), sensor_attributes=sensor_attributes),
            cfg,
        )
        planner_input = build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=recs,
            batteries_schedules=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
            forecast_coverage=coverage,
        )
        return recs, coverage, planner_input, run_planner(planner_input)


def _today_prices() -> dict[str, dict[str, Any]]:
    """Prices published for today only: the state before the day-ahead auction."""
    return {
        _IMPORT: _prices(_MIDNIGHT),
        _EXPORT: _prices(_MIDNIGHT, offset=-0.5),
    }


def _both_days_prices() -> dict[str, dict[str, Any]]:
    """Prices published for today and, one unit dearer, for tomorrow."""
    return {
        _IMPORT: {
            "prices_today": _prices(_MIDNIGHT)["prices_today"],
            "prices_tomorrow": _prices(_TOMORROW, offset=1.0)["prices_today"],
        },
        _EXPORT: {
            "prices_today": _prices(_MIDNIGHT, offset=-0.5)["prices_today"],
            "prices_tomorrow": _prices(_TOMORROW, offset=0.5)["prices_today"],
        },
    }


class TestPricesForTodayOnly:
    """The normal state every day until the day-ahead prices publish."""

    def test_tomorrow_is_planned_with_todays_same_hour_price(self) -> None:
        """The #1002 rule, reached on the production path."""
        _recs, _coverage, planner_input, output = _run(_cfg(), _today_prices())

        # 96 quarter-hours of today; nothing for tomorrow.
        assert len(planner_input.price_points) == 96
        assert {point.day_offset for point in planner_input.price_points} == {0}
        tomorrow = [slot for slot in output.slots if slot.start >= _TOMORROW]
        assert len(tomorrow) == 96
        for slot in tomorrow:
            assert slot.price.import_price == pytest.approx(
                _import_price(slot.start.hour)
            )
            assert slot.price.export_price == pytest.approx(
                _import_price(slot.start.hour) - 0.5
            )

    def test_the_gap_is_reported(self) -> None:
        _recs, _coverage, _input, output = _run(_cfg(), _today_prices())

        quality = output.data_quality
        assert quality.tomorrow_price_missing_hours == list(range(24))
        assert quality.today_price_missing_hours == []
        assert quality.is_complete is False
        assert any("Tomorrow price data missing" in w for w in output.warnings)

    def test_complete_prices_are_complete(self) -> None:
        """With both days published nothing is estimated and nothing reported."""
        _recs, _coverage, planner_input, output = _run(_cfg(), _both_days_prices())

        assert len(planner_input.price_points) == 192
        assert output.data_quality.tomorrow_price_missing_hours == []
        assert output.data_quality.today_price_missing_hours == []
        tomorrow = [slot for slot in output.slots if slot.start >= _TOMORROW]
        assert all(
            slot.price.import_price == pytest.approx(_import_price(slot.start.hour) + 1)
            for slot in tomorrow
        )

    def test_a_slot_with_only_one_of_the_two_prices_is_missing(self) -> None:
        """Import published for both days, export for today only."""
        sensor_attributes = {
            _IMPORT: _both_days_prices()[_IMPORT],
            _EXPORT: _prices(_MIDNIGHT, offset=-0.5),
        }

        _recs, coverage, planner_input, output = _run(_cfg(), sensor_attributes)

        assert len(coverage.import_price) == 192
        assert len(coverage.export_price) == 96
        assert len(planner_input.price_points) == 96
        assert output.data_quality.tomorrow_price_missing_hours == list(range(24))

    def test_quarter_hourly_prices_are_estimated_too(self) -> None:
        """A 15-minute source for today only: tomorrow is still estimated."""
        sensor_attributes = {
            _IMPORT: _prices(_MIDNIGHT, step_minutes=15),
            _EXPORT: _prices(_MIDNIGHT, offset=-0.5, step_minutes=15),
        }

        _recs, _coverage, planner_input, output = _run(_cfg(), sensor_attributes)

        assert len(planner_input.price_points) == 96
        tomorrow = [slot for slot in output.slots if slot.start >= _TOMORROW]
        assert all(
            slot.price.import_price == pytest.approx(_import_price(slot.start.hour))
            for slot in tomorrow
        )
        assert output.data_quality.tomorrow_price_missing_hours == list(range(24))


class TestSeventyTwoHourHorizon:
    """6.3.x still offers the 72 h horizon; day+2 never has a published price.

    That the option is still offered is asserted in ``tests/test_init_flow.py``.
    """

    def test_day2_is_estimated_from_tomorrow_when_tomorrow_is_published(self) -> None:
        """The nearest earlier day with data is tomorrow."""
        _recs, _coverage, planner_input, output = _run(_cfg(72), _both_days_prices())

        assert len(planner_input.price_points) == 192
        day2 = [slot for slot in output.slots if slot.start >= _DAY2]
        assert len(day2) == 96
        for slot in day2:
            assert slot.price.import_price == pytest.approx(
                _import_price(slot.start.hour) + 1.0
            )
            assert slot.price.export_price == pytest.approx(
                _import_price(slot.start.hour) + 0.5
            )
        assert output.data_quality.tomorrow_price_missing_hours == []
        assert output.data_quality.day2_price_missing_hours == list(range(24))
        assert any("Day+2 price data missing" in w for w in output.warnings)

    def test_day2_is_estimated_from_today_before_tomorrow_is_published(self) -> None:
        """With today only, both later days take today's same-hour price."""
        _recs, _coverage, _input, output = _run(_cfg(72), _today_prices())

        later = [slot for slot in output.slots if slot.start >= _TOMORROW]
        assert len(later) == 192
        assert all(
            slot.price.import_price == pytest.approx(_import_price(slot.start.hour))
            for slot in later
        )
        assert output.data_quality.tomorrow_price_missing_hours == list(range(24))
        assert output.data_quality.day2_price_missing_hours == list(range(24))


class TestPriceSensorWithoutData:
    """An unavailable price sensor: no price for any slot."""

    def test_every_hour_is_reported_and_nothing_can_be_estimated(self) -> None:
        _recs, coverage, planner_input, output = _run(_cfg(), {})

        assert coverage.import_price == frozenset()
        assert planner_input.price_points == []
        quality = output.data_quality
        assert quality.today_price_missing_hours == list(range(24))
        assert quality.tomorrow_price_missing_hours == list(range(24))
        assert quality.is_complete is False
        # No earlier day to estimate from: the 0.0 fallback, now reported.
        assert {slot.price.import_price for slot in output.slots} == {0.0}


class TestPublishedZeroAndNegativePrices:
    """A real price of 0.0 or below is data, not a gap."""

    def test_zero_and_negative_prices_pass_through_unchanged(self) -> None:
        zero = _prices(_MIDNIGHT)
        zero["prices_today"][10]["price"] = 0.0
        zero["prices_today"][11]["price"] = -0.25
        tomorrow_zero = _prices(_TOMORROW, offset=1.0)["prices_today"]
        for point in tomorrow_zero:
            point["price"] = 0.0
        sensor_attributes = {
            _IMPORT: {**zero, "prices_tomorrow": tomorrow_zero},
            _EXPORT: {
                "prices_today": _prices(_MIDNIGHT, offset=-0.5)["prices_today"],
                "prices_tomorrow": _prices(_TOMORROW, offset=-2.0)["prices_today"],
            },
        }

        _recs, _coverage, planner_input, output = _run(_cfg(), sensor_attributes)

        assert len(planner_input.price_points) == 192
        by_start = {slot.start: slot for slot in output.slots}
        assert by_start[_MIDNIGHT.replace(hour=10)].price.import_price == (
            pytest.approx(0.0)
        )
        assert by_start[_MIDNIGHT.replace(hour=11)].price.import_price == (
            pytest.approx(-0.25)
        )
        # A whole day at exactly 0.0 is still that day's price …
        tomorrow = [slot for slot in output.slots if slot.start >= _TOMORROW]
        assert all(slot.price.import_price == pytest.approx(0.0) for slot in tomorrow)
        assert all(slot.price.export_price < 0.0 for slot in tomorrow)
        # … and is not reported as missing.
        assert output.data_quality.tomorrow_price_missing_hours == []
        assert output.data_quality.today_price_missing_hours == []


class TestPvIsNotPartOfThePort:
    """PV keeps its 6.3.x behaviour: hour-granular entries, gaps unreported."""

    def test_a_missing_pv_forecast_is_not_reported(self) -> None:
        pv_today = {
            "detailedHourly": [
                {
                    "period_start": (_MIDNIGHT + timedelta(hours=h)).isoformat(),
                    "pv_estimate": 2.0 if 8 <= h < 16 else 0.0,
                }
                for h in range(24)
            ]
        }

        _recs, _coverage, planner_input, output = _run(
            _cfg(), {**_both_days_prices(), _PV_TODAY: pv_today}
        )

        # One entry per hour of the horizon, exactly as before the port.
        assert len(planner_input.solcast_slots) == 48
        assert output.data_quality.today_pv_missing_hours == []
        assert output.data_quality.tomorrow_pv_missing_hours == []
        assert output.data_quality.is_complete is True


class TestRecommendationsAreUnchanged:
    """The slot list is a sensor attribute: no new per-slot keys."""

    def test_coverage_is_not_stored_on_the_recommendations(self) -> None:
        before = {field.name for field in fields(HourlyRecommendation)}
        with patch("homeassistant.util.dt.now", return_value=_NOW):
            fresh = generate_recommendation_intervals(15, 48)[0]
        keys_before = set(vars(fresh))

        recs, _coverage, _input, _output = _run(_cfg(), _today_prices())

        assert {field.name for field in fields(HourlyRecommendation)} == before
        assert all(set(vars(rec)) == keys_before for rec in recs)

    def test_uncovered_slots_still_show_zero_on_the_recommendation(self) -> None:
        """Consumers that run before the plan is applied never see NaN."""
        recs, _coverage, _input, _output = _run(_cfg(), _today_prices())

        tomorrow = [rec for rec in recs if rec.start >= _TOMORROW]
        assert all(rec.import_price == pytest.approx(0.0) for rec in tomorrow)
        assert all(rec.export_price == pytest.approx(0.0) for rec in tomorrow)


class TestCoordinatorPassesTheCoverage:
    """The cycle keeps the populator's coverage and the planner phase uses it."""

    @pytest.mark.asyncio
    async def test_coverage_reaches_build_planner_input(self, tmp_path: Path) -> None:
        coverage = ForecastCoverage(import_price=frozenset({utc_key(_FIXTURE_NOW)}))
        coordinator, build, _solved = _coordinator(
            tmp_path, reference=_plan(grid_charge=False), floor_enabled=False
        )
        collected: tuple[Any, None, list[Any]] = (_snapshot(), None, [])

        with (
            patch(f"{_CYCLE}.hsem_now", return_value=_FIXTURE_NOW),
            patch(f"{_BUILDER}.hsem_now", return_value=_FIXTURE_NOW),
            patch(
                f"{_CYCLE}.async_collect_all_states",
                AsyncMock(return_value=collected),
            ),
            patch(
                f"{_CYCLE}.populate_avg_house_consumption_from_snapshot",
                side_effect=_populate_consumption,
            ),
            patch(
                f"{_CYCLE}.populate_price_and_solcast_from_snapshot",
                return_value=coverage,
            ),
            patch(f"{_PHASE}.build_planner_input", build),
        ):
            consumption_ok, state = await coordinator._async_collect_and_populate(
                _FIXTURE_NOW
            )
            live = coordinator._live
            assert live is not None
            await coordinator._run_planner_phase(
                _FIXTURE_NOW, live, coordinator._cfg, state, consumption_ok, 0
            )

        assert coordinator._forecast_coverage is coverage
        build.assert_called_once()
        assert build.call_args.kwargs["forecast_coverage"] is coverage

    def test_a_new_coordinator_has_no_coverage_yet(self, tmp_path: Path) -> None:
        coordinator, _build, _solved = _coordinator(
            tmp_path, reference=_plan(grid_charge=False), floor_enabled=False
        )

        assert coordinator._forecast_coverage is None


class TestForecastCoverage:
    """The coverage object on its own."""

    def test_price_needs_both_sides(self) -> None:
        both = _MIDNIGHT
        import_only = _MIDNIGHT + timedelta(hours=1)
        coverage = ForecastCoverage(
            import_price=frozenset({utc_key(both), utc_key(import_only)}),
            export_price=frozenset({utc_key(both)}),
        )

        assert coverage.has_price(both) is True
        assert coverage.has_price(import_only) is False
        assert coverage.has_price(_MIDNIGHT + timedelta(hours=2)) is False

    def test_slots_are_matched_by_instant_not_by_wall_clock(self) -> None:
        """The same instant in another zone is the same slot (issue #1160)."""
        coverage = ForecastCoverage(
            import_price=frozenset({utc_key(_MIDNIGHT)}),
            export_price=frozenset({utc_key(_MIDNIGHT)}),
        )

        assert coverage.has_price(_MIDNIGHT.astimezone(UTC)) is True


class TestWithoutCoverage:
    """A caller that passes no coverage gets the previous behaviour."""

    def test_every_slot_is_emitted(self) -> None:
        with patch("homeassistant.util.dt.now", return_value=_NOW):
            recs = generate_recommendation_intervals(15, 48)
            planner_input = build_planner_input(
                cfg=_cfg(),
                live=LiveState(),
                hourly_recommendations=recs,
                batteries_schedules=[],
                previous_winner_name=None,
                previous_winner_score=0.0,
            )

        assert len(planner_input.price_points) == 192
        assert len(planner_input.solcast_slots) == 48

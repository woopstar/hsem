"""Regression tests for issue #1191 — sub-hourly Solcast data and the hour's PV.

The Solcast integration can expose ``detailedHourly`` (hourly) and
``detailedForecast`` (half-hourly) on the same sensor, both as average power in
kW.  The populator read both, the half-hourly values overwrote the hourly
ones, and a slot only received the source point whose window contained the
slot start.  ``build_planner_input`` then kept the first slot of each hour, so
the planner used the first half-hour's power as the energy of the whole hour.

These tests go through the real populator and the real builder.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.hsem import coordinator_builder
from custom_components.hsem.coordinator_builder import build_planner_input
from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    _populate_from_attributes,
    populate_price_and_solcast_from_snapshot,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot

_TZ = timezone(timedelta(hours=3))
_NOW = datetime(2026, 10, 1, 0, 5, tzinfo=_TZ)
_H8 = datetime(2026, 10, 1, 8, 0, tzinfo=_TZ)
_SOLCAST_TODAY = "sensor.solcast_pv_forecast_forecast_today"

# 08:00-08:30 averages 0.4 kW and 08:30-09:00 averages 0.8 kW, so the hour
# holds 0.6 kWh; 09:00-10:00 holds 1.2 kWh (1.0 kW then 1.4 kW).
_HOURLY: dict[str, Any] = {
    "detailedHourly": [
        {"period_start": _H8.isoformat(), "pv_estimate": 0.6},
        {"period_start": (_H8 + timedelta(hours=1)).isoformat(), "pv_estimate": 1.2},
    ]
}
_HALF_HOURLY: dict[str, Any] = {
    "detailedForecast": [
        {"period_start": _H8.isoformat(), "pv_estimate": 0.4},
        {"period_start": (_H8 + timedelta(minutes=30)).isoformat(), "pv_estimate": 0.8},
        {"period_start": (_H8 + timedelta(minutes=60)).isoformat(), "pv_estimate": 1.0},
        {"period_start": (_H8 + timedelta(minutes=90)).isoformat(), "pv_estimate": 1.4},
    ]
}
_BOTH: dict[str, Any] = {**_HOURLY, **_HALF_HOURLY}


def _recommendations(interval_minutes: int) -> list[HourlyRecommendation]:
    """Return a fresh 24 h recommendation grid starting at the planning day."""
    with patch.object(coordinator_builder, "hsem_now", return_value=_NOW):
        return coordinator_builder.generate_recommendation_intervals(
            interval_minutes, 24
        )


def _populate_pv(
    recs: list[HourlyRecommendation], *sources: dict[str, Any]
) -> list[HourlyRecommendation]:
    """Populate PV from each source in turn through the snapshot populator."""
    cfg = SensorConfig()
    cfg.solcast_pv_forecast_forecast_today = _SOLCAST_TODAY
    cfg.solcast_pv_forecast_forecast_likelihood = "pv_estimate"
    for attributes in sources:
        snapshot = StateSnapshot(
            live=LiveState(), sensor_attributes={_SOLCAST_TODAY: attributes}
        )
        populate_price_and_solcast_from_snapshot(recs, snapshot, cfg)
    return recs


def _hour_values(recs: list[HourlyRecommendation], hour: datetime) -> list[float]:
    """Return the PV value on every slot starting inside *hour*."""
    return [
        rec.solcast_pv_estimate_kwh
        for rec in recs
        if hour <= rec.start < hour + timedelta(hours=1)
    ]


def _planner_pv_by_hour(
    recs: list[HourlyRecommendation], interval_minutes: int
) -> dict[tuple[int, int], float]:
    """Return ``{(day_offset, hour): pv}`` as ``build_planner_input`` emits it."""
    cfg = SensorConfig()
    cfg.recommendation_interval_minutes = interval_minutes
    with patch.object(coordinator_builder, "hsem_now", return_value=_NOW):
        planner_input = build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=recs,
            batteries_schedules=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
        )
    return {(s.day_offset, s.hour): s.pv_estimate for s in planner_input.solcast_slots}


class TestPopulator:
    """What the populator leaves on the slots."""

    def test_half_hourly_source_fills_quarter_hour_slots_per_half_hour(self) -> None:
        """A source coarser than the slot still fans out unchanged."""
        recs = _populate_pv(_recommendations(15), _HALF_HOURLY)

        assert _hour_values(recs, _H8) == pytest.approx([0.4, 0.4, 0.8, 0.8])

    def test_hourly_slot_gets_the_mean_of_its_half_hours(self) -> None:
        """A source finer than the slot is averaged over the slot (#1191).

        Before the fix the 60-minute slot kept the 08:00 half-hour (0.4) and
        the 08:30 point matched no slot at all.
        """
        recs = _populate_pv(_recommendations(60), _HALF_HOURLY)

        assert _hour_values(recs, _H8) == pytest.approx([0.6])
        assert _hour_values(recs, _H8 + timedelta(hours=1)) == pytest.approx([1.2])

    def test_partly_covered_slot_uses_the_covered_part(self) -> None:
        """A slot with only its first half-hour published keeps that value."""
        source = {"detailedForecast": _HALF_HOURLY["detailedForecast"][:1]}
        recs = _populate_pv(_recommendations(60), source)

        assert _hour_values(recs, _H8) == pytest.approx([0.4])

    def test_hourly_source_is_stored_unchanged(self) -> None:
        """The hourly attribute lands on every slot of its hour, as before."""
        recs = _populate_pv(_recommendations(15), _HOURLY)

        assert _hour_values(recs, _H8) == [0.6, 0.6, 0.6, 0.6]

    def test_prices_keep_start_in_window_matching(self) -> None:
        """Price population is untouched: no averaging across the slot."""
        prices = {
            "prices_today": [
                {"start": (_H8 + timedelta(minutes=m)).isoformat(), "price": price}
                for m, price in ((0, 1.0), (15, 2.0), (30, 3.0), (45, 4.0))
            ]
        }
        recs = _recommendations(60)

        _populate_from_attributes(prices, recs, "import_price", "pv_estimate", 60)

        (rec,) = [r for r in recs if r.start == _H8]
        assert rec.import_price == pytest.approx(1.0)


class TestPlannerHourValue:
    """What ``build_planner_input`` hands the planner for each hour."""

    @pytest.mark.parametrize("interval_minutes", [15, 30, 60])
    @pytest.mark.parametrize(
        "source", [_BOTH, _HALF_HOURLY], ids=["both", "half_hourly_only"]
    )
    def test_hour_value_is_the_hours_energy(
        self, source: dict[str, Any], interval_minutes: int
    ) -> None:
        """The hour's PV is its energy, not its first half-hour (#1191)."""
        recs = _populate_pv(_recommendations(interval_minutes), source)

        pv = _planner_pv_by_hour(recs, interval_minutes)

        assert pv[(0, 8)] == pytest.approx(0.6)
        assert pv[(0, 9)] == pytest.approx(1.2)

    @pytest.mark.parametrize("interval_minutes", [15, 30, 60])
    def test_hourly_only_source_is_identical_to_first_slot_rule(
        self, interval_minutes: int
    ) -> None:
        """An hourly sensor gives exactly what the first-slot rule gave."""
        recs = _populate_pv(_recommendations(interval_minutes), _HOURLY)
        first_slot_rule: dict[tuple[int, int], float] = {}
        for rec in recs:
            first_slot_rule.setdefault(
                (0, rec.start.hour), round(rec.solcast_pv_estimate_kwh, 3)
            )

        pv = _planner_pv_by_hour(recs, interval_minutes)

        assert pv == first_slot_rule
        assert pv[(0, 8)] == 0.6
        assert pv[(0, 9)] == 1.2

    @pytest.mark.parametrize("interval_minutes", [15, 30, 60])
    def test_hour_value_does_not_depend_on_attribute_order(
        self, interval_minutes: int
    ) -> None:
        """Hourly-then-half-hourly and the reverse give the same hour."""
        hourly_last = _populate_pv(
            _recommendations(interval_minutes), _HALF_HOURLY, _HOURLY
        )
        half_hourly_last = _populate_pv(
            _recommendations(interval_minutes), _HOURLY, _HALF_HOURLY
        )

        assert _planner_pv_by_hour(hourly_last, interval_minutes) == pytest.approx(
            _planner_pv_by_hour(half_hourly_last, interval_minutes)
        )

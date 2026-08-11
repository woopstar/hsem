"""Regression tests for issue #720 (stage 2) — planner input must not
collapse quarter-hourly prices to hourly.

Bug
---
``coordinator_builder.build_planner_input`` deduplicated recommendation
slots on ``(day_offset, hour)`` and appended price points *inside* that
guard, so only the first quarter of each hour survived — 192 correct
quarter-hourly slots were reduced to 48 hourly price points.
``planner.slot_population.populate_prices`` then fanned the survivor back
across the hour via ``align_hourly_prices``, so the MILP saw one flat
price per hour even when Nord Pool 15-min MTU data provided 96 distinct
prices per day.

Fix (three parts)
-----------------
1. ``PricePoint`` gained an optional ``slot_in_day`` field (hour-granular
   callers unaffected).
2. ``build_planner_input`` appends price points per slot (outside the
   hourly dedup guard) and sets ``slot_in_day``; consumption averages and
   Solcast PV stay hour-deduplicated.
3. ``populate_prices`` keys by ``(day_offset, slot_in_day)`` when points
   carry it, with an hourly fallback for uncovered slots.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.planner.slot_population import (
    populate_prices,
)


def _tsi(now: datetime, interval: int = 15, hours: int = 48) -> TimeSeriesIndex:
    return TimeSeriesIndex.from_now(now, interval_minutes=interval, horizon_hours=hours)


def _slots_from_tsi(tsi: TimeSeriesIndex) -> list[PlannedSlot]:
    return [PlannedSlot(start=m.start, end=m.end) for m in tsi]


class TestSlotInDayPricePoints:
    """96 distinct 15-min prices must reach 96 distinct planner slots."""

    def setup_method(self) -> None:
        self.now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)

    def _make_points(self, days: int = 2) -> list[PricePoint]:
        points = []
        for d in range(days):
            for i in range(96):
                hour = i // 4
                price = 1.0 + d * 100 + i * 0.01  # unique per slot per day
                points.append(
                    PricePoint(
                        hour=hour,
                        import_price=price,
                        export_price=price - 0.1,
                        day_offset=d,
                        slot_in_day=i,
                    )
                )
        return points

    def test_quarter_hourly_prices_land_on_distinct_slots(self) -> None:
        tsi = _tsi(self.now)
        slots = _slots_from_tsi(tsi)
        points = self._make_points()
        populate_prices(slots, points, tsi=tsi)

        for d in range(2):
            for i in range(96):
                slot = slots[d * 96 + i]
                expected = 1.0 + d * 100 + i * 0.01
                assert slot.price.import_price == pytest.approx(expected), (
                    f"day {d} slot {i}: expected {expected}, "
                    f"got {slot.price.import_price}"
                )
                assert slot.price.export_price == pytest.approx(expected - 0.1)

    def test_intra_hour_variation_preserved(self) -> None:
        """The Nord Pool SE4 example from the issue: hour 17 has four very
        different quarter-hourly prices; all four must survive."""
        tsi = _tsi(self.now, hours=24)
        slots = _slots_from_tsi(tsi)
        points = [
            PricePoint(
                hour=17,
                import_price=p,
                export_price=p,
                day_offset=0,
                slot_in_day=17 * 4 + q,
            )
            for q, p in enumerate([0.179, 0.363, 0.476, 0.603])
        ]
        populate_prices(slots, points, tsi=tsi)

        got = [slots[17 * 4 + q].price.import_price for q in range(4)]
        assert got == pytest.approx([0.179, 0.363, 0.476, 0.603])

    def test_hourly_fallback_for_uncovered_slots(self) -> None:
        """A point set that only covers slot :00 of each hour (e.g. a
        60-min source) must still fan out to the remaining quarters via the
        hourly fallback."""
        tsi = _tsi(self.now, hours=24)
        slots = _slots_from_tsi(tsi)
        points = [
            PricePoint(
                hour=h,
                import_price=0.10 + h * 0.01,
                export_price=0.05,
                day_offset=0,
                slot_in_day=h * 4,  # only the :00 slot of each hour
            )
            for h in range(24)
        ]
        populate_prices(slots, points, tsi=tsi)

        for i, slot in enumerate(slots):
            hour = i // 4
            assert slot.price.import_price == pytest.approx(0.10 + hour * 0.01)

    def test_legacy_hourly_points_unchanged(self) -> None:
        """Points without slot_in_day use the existing hourly path."""
        tsi = _tsi(self.now, hours=24)
        slots = _slots_from_tsi(tsi)
        points = [
            PricePoint(hour=h, import_price=0.20 + h * 0.01, export_price=0.1)
            for h in range(24)
        ]
        populate_prices(slots, points, tsi=tsi)

        for i, slot in enumerate(slots):
            hour = i // 4
            assert slot.price.import_price == pytest.approx(0.20 + hour * 0.01)


class TestBuildPlannerInputSlotInDay:
    """coordinator_builder must emit one price point per recommendation slot."""

    def test_price_point_count_matches_slots(self) -> None:
        """48 h horizon at 15-min slots → 192 price points, not 48."""
        from custom_components.hsem.coordinator_builder import build_planner_input
        from custom_components.hsem.models.hourly_recommendation import (
            HourlyRecommendation,
        )
        from custom_components.hsem.models.live_state import LiveState
        from custom_components.hsem.models.sensor_config import SensorConfig

        cfg = SensorConfig()
        cfg.recommendation_interval_minutes = 15
        cfg.recommendation_interval_length = 48
        cfg.electricity_price_update_interval = 15

        base = datetime(2026, 8, 9, 0, 0, 0, tzinfo=UTC)

        def _rec(i: int) -> HourlyRecommendation:
            start = base + timedelta(minutes=15 * i)
            return HourlyRecommendation(
                start=start,
                end=start + timedelta(minutes=15),
                recommendation="idle",
                avg_house_consumption_kwh=0.1,
                avg_house_consumption_1d_kwh=0.1,
                avg_house_consumption_3d_kwh=0.1,
                avg_house_consumption_7d_kwh=0.1,
                avg_house_consumption_14d_kwh=0.1,
                batteries_charged_kwh=0.0,
                batteries_discharged_kwh=0.0,
                estimated_battery_capacity_kwh=0.0,
                estimated_battery_soc_pct=0.0,
                estimated_cost_currency=0.0,
                estimated_net_consumption_kwh=0.0,
                export_price=round(0.05 + i * 0.001, 5),
                grid_export_kwh=0.0,
                grid_import_kwh=0.0,
                import_price=round(0.10 + i * 0.001, 5),
                solcast_pv_estimate_kwh=0.0,
            )

        recs = [_rec(i) for i in range(192)]

        # build_planner_input derives day_offset from hsem_now() (via
        # dt_util.now()). Pin the current time to the recommendation window so
        # the test stays deterministic regardless of the wall-clock date.
        with patch("homeassistant.util.dt.now", return_value=base):
            inp = build_planner_input(
                cfg=cfg,
                live=LiveState(),
                hourly_recommendations=recs,
                batteries_schedules=[],
                previous_winner_name=None,
                previous_winner_score=0.0,
            )

        assert len(inp.price_points) == 192
        assert len(inp.consumption_averages) == 48  # still hour-deduplicated
        assert len(inp.solcast_slots) == 48

        # Distinct quarter-hourly prices must survive with distinct slot_in_day.
        slot_keys = {(pp.day_offset, pp.slot_in_day) for pp in inp.price_points}
        assert len(slot_keys) == 192

        # First hour of day 0: four distinct prices, none collapsed.
        first_hour = [pp for pp in inp.price_points if pp.day_offset == 0][:4]
        prices = [pp.import_price for pp in first_hour]
        assert len(set(prices)) == 4

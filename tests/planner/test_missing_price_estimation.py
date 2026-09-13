"""Regression tests for missing-price estimation (issue #1002).

Spot-market prices are only known for today (and tomorrow after the ~13:00
day-ahead auction).  Before the fix, slots beyond the data coverage silently
received ``0.0`` import/export prices, so the planner treated unknown future
energy as *free* and could schedule grid charging against a fictitious price
curve.

Fixed behaviour:

- A slot with no source price is filled with the same-hour price from the
  nearest earlier day that has data (day+1 falls back to today, day+2 to
  tomorrow-then-today).
- The gap is still recorded on ``TimeSeriesIndex.missing_price_slots`` so
  ``DataQuality`` warnings keep firing — on the sub-hourly (``slot_in_day``)
  path too, which previously bypassed tracking entirely.
- Only when no earlier day has data for that hour at all does the slot fall
  back to 0.0 (backward-compatible pathological case).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.planner.slot_price_population import populate_prices

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)

_TODAY_IMP = [0.20 + 0.01 * h for h in range(24)]
_TODAY_EXP = [max(p - 0.05, 0.01) for p in _TODAY_IMP]
_TOMORROW_IMP = [0.40 + 0.01 * h for h in range(24)]
_TOMORROW_EXP = [max(p - 0.05, 0.01) for p in _TOMORROW_IMP]


def _build(
    horizon_hours: int, interval_minutes: int
) -> tuple[list[PlannedSlot], TimeSeriesIndex]:
    """Return (slots, tsi) for a horizon starting at midnight."""
    tsi = TimeSeriesIndex.from_now(
        _NOW, interval_minutes=interval_minutes, horizon_hours=horizon_hours
    )
    slots = [PlannedSlot(start=m.start, end=m.end) for m in tsi]
    return slots, tsi


def _hourly_points(
    import_prices: list[float], export_prices: list[float], day_offset: int
) -> list[PricePoint]:
    """Hour-granular price points for one day (no slot_in_day)."""
    return [
        PricePoint(
            hour=h,
            import_price=import_prices[h],
            export_price=export_prices[h],
            day_offset=day_offset,
        )
        for h in range(24)
    ]


def _subhourly_points(
    import_prices: list[float],
    export_prices: list[float],
    day_offset: int,
    slots_per_hour: int,
) -> list[PricePoint]:
    """Sub-hourly price points for one day (slot_in_day set, issue #720 path)."""
    return [
        PricePoint(
            hour=h,
            import_price=import_prices[h],
            export_price=export_prices[h],
            day_offset=day_offset,
            slot_in_day=h * slots_per_hour + q,
        )
        for h in range(24)
        for q in range(slots_per_hour)
    ]


class TestHourlyPathEstimation:
    """Estimation on the ``(day_offset, hour)``-keyed alignment path."""

    def test_today_only_data_is_cyclical_legacy_behavior(self) -> None:
        """Hour-only (single-day) price data intentionally repeats across days.

        When no point carries a non-zero ``day_offset`` the alignment uses
        legacy hour-only keys, which are cyclical by design: today's prices
        cover every day of the horizon and nothing is reported missing.
        """
        slots, tsi = _build(horizon_hours=48, interval_minutes=60)
        populate_prices(slots, _hourly_points(_TODAY_IMP, _TODAY_EXP, 0), tsi)

        for meta, slot in zip(tsi.slots, slots, strict=True):
            assert slot.price.import_price == pytest.approx(_TODAY_IMP[meta.hour])
            assert slot.price.export_price == pytest.approx(_TODAY_EXP[meta.hour])
        assert tsi.missing_future_day_price_hours(1) == set()

    def test_missing_gap_still_tracked_for_data_quality(self) -> None:
        """Estimated slots must still be reported as missing in DataQuality."""
        slots, tsi = _build(horizon_hours=48, interval_minutes=60)
        missing = {10, 11, 12, 13}
        points = _hourly_points(_TODAY_IMP, _TODAY_EXP, 0) + [
            p
            for p in _hourly_points(_TOMORROW_IMP, _TOMORROW_EXP, 1)
            if p.hour not in missing
        ]
        populate_prices(slots, points, tsi)

        assert tsi.missing_future_day_price_hours(1) == missing
        assert tsi.missing_future_day_price_hours(0) == set()
        for meta, slot in zip(tsi.slots, slots, strict=True):
            if meta.key.day_offset != 1:
                continue
            if meta.hour in missing:
                # Gap hours are estimated from today, not zeroed.
                assert slot.price.import_price == pytest.approx(_TODAY_IMP[meta.hour])
            else:
                assert slot.price.import_price == pytest.approx(
                    _TOMORROW_IMP[meta.hour]
                )

    def test_known_tomorrow_prices_used_verbatim(self) -> None:
        """When tomorrow's prices exist they must not be overwritten."""
        slots, tsi = _build(horizon_hours=48, interval_minutes=60)
        points = _hourly_points(_TODAY_IMP, _TODAY_EXP, 0) + _hourly_points(
            _TOMORROW_IMP, _TOMORROW_EXP, 1
        )
        populate_prices(slots, points, tsi)

        for meta, slot in zip(tsi.slots, slots, strict=True):
            if meta.key.day_offset == 1:
                assert slot.price.import_price == pytest.approx(
                    _TOMORROW_IMP[meta.hour]
                )
        assert tsi.missing_future_day_price_hours(1) == set()

    def test_day2_prefers_nearest_earlier_day(self) -> None:
        """Day+2 must be estimated from day+1 when known, else from today."""
        slots, tsi = _build(horizon_hours=72, interval_minutes=60)
        points = _hourly_points(_TODAY_IMP, _TODAY_EXP, 0) + _hourly_points(
            _TOMORROW_IMP, _TOMORROW_EXP, 1
        )
        populate_prices(slots, points, tsi)

        for meta, slot in zip(tsi.slots, slots, strict=True):
            if meta.key.day_offset == 2:
                assert slot.price.import_price == pytest.approx(
                    _TOMORROW_IMP[meta.hour]
                )
        assert tsi.missing_future_day_price_hours(2) == set(range(24))

    def test_hour_missing_on_all_days_keeps_zero_fallback(self) -> None:
        """An hour with no data on any earlier day keeps the 0.0 fallback."""
        slots, tsi = _build(horizon_hours=48, interval_minutes=60)
        points = [p for p in _hourly_points(_TODAY_IMP, _TODAY_EXP, 0) if p.hour != 13]
        populate_prices(slots, points, tsi)

        for meta, slot in zip(tsi.slots, slots, strict=True):
            if meta.hour == 13:
                assert slot.price.import_price == pytest.approx(0.0)
                assert slot.price.export_price == pytest.approx(0.0)
            else:
                assert abs(slot.price.import_price) > 1e-9


class TestSubHourlyPathEstimation:
    """Estimation on the ``slot_in_day`` path (issue #720 quarter-hourly)."""

    def test_missing_tomorrow_estimated_from_today(self) -> None:
        """15-min slots beyond the data coverage get today's same-hour price."""
        slots, tsi = _build(horizon_hours=48, interval_minutes=15)
        populate_prices(slots, _subhourly_points(_TODAY_IMP, _TODAY_EXP, 0, 4), tsi)

        for meta, slot in zip(tsi.slots, slots, strict=True):
            if meta.key.day_offset == 1:
                assert slot.price.import_price == pytest.approx(_TODAY_IMP[meta.hour])

    def test_subhourly_gap_tracked_for_data_quality(self) -> None:
        """The slot_in_day path must record missing prices on the TSI.

        Previously this path bypassed ``align_hourly_prices`` entirely, so
        ``missing_price_slots`` stayed empty and DataQuality never warned.
        """
        slots, tsi = _build(horizon_hours=48, interval_minutes=15)
        populate_prices(slots, _subhourly_points(_TODAY_IMP, _TODAY_EXP, 0, 4), tsi)

        assert tsi.missing_future_day_price_hours(1) == set(range(24))
        assert tsi.missing_future_day_price_hours(0) == set()

    def test_subhourly_known_tomorrow_not_overwritten(self) -> None:
        """Known quarter-hourly day+1 prices survive untouched."""
        slots, tsi = _build(horizon_hours=48, interval_minutes=15)
        points = _subhourly_points(_TODAY_IMP, _TODAY_EXP, 0, 4) + _subhourly_points(
            _TOMORROW_IMP, _TOMORROW_EXP, 1, 4
        )
        populate_prices(slots, points, tsi)

        for meta, slot in zip(tsi.slots, slots, strict=True):
            if meta.key.day_offset == 1:
                assert slot.price.import_price == pytest.approx(
                    _TOMORROW_IMP[meta.hour]
                )
        assert tsi.missing_future_day_price_hours(1) == set()

    def test_hourly_source_feeding_subhourly_slots_estimated(self) -> None:
        """A 60-min source on a 15-min grid: uncovered days still estimated."""
        slots, tsi = _build(horizon_hours=48, interval_minutes=15)
        # Hourly source (slot_in_day=None is NOT allowed to trigger the other
        # path) — emulate by tagging only the first sub-slot of each hour.
        points = [
            PricePoint(
                hour=h,
                import_price=_TODAY_IMP[h],
                export_price=_TODAY_EXP[h],
                day_offset=0,
                slot_in_day=h * 4,
            )
            for h in range(24)
        ]
        populate_prices(slots, points, tsi)

        for meta, slot in zip(tsi.slots, slots, strict=True):
            assert slot.price.import_price == pytest.approx(_TODAY_IMP[meta.hour])

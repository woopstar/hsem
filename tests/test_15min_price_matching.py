"""Regression tests for issue #720 — 15-minute prices collapsed to hourly.

Bug
---
``custom_sensors/hourly_data_populator/prices_solcast.py`` normalised every
price timestamp with ``.replace(minute=0, second=0)`` on both the sensor data
points and the recommendation slots before matching.  With 15-minute
recommendation slots and 15-minute price data (e.g. Energi Data Service on
Nord Pool 15-min MTUs), all four quarter-hour price points within an hour
collapsed onto the same key and the *last* one was written to all four
recommendation slots of that hour.

Fix
---
Both sides of the match are now floored to the enclosing recommendation slot
via ``normalize_slot_start(dt, interval_minutes)`` so each sub-hourly price
lands on exactly one slot.

These tests exercise the snapshot-based path
(``populate_price_and_solcast_from_snapshot``), which shares the matching
helper logic with the async path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    populate_price_and_solcast_from_snapshot,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot


def _make_rec(start: datetime, end: datetime) -> HourlyRecommendation:
    return HourlyRecommendation(
        start=start,
        end=end,
        recommendation="idle",
        avg_house_consumption_kwh=0.0,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=0.0,
        batteries_discharged_kwh=0.0,
        estimated_battery_capacity_kwh=0.0,
        estimated_battery_soc_pct=0.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.0,
        export_price=0.0,
        grid_export_kwh=0.0,
        grid_import_kwh=0.0,
        import_price=0.0,
        solcast_pv_estimate_kwh=0.0,
    )


class _Cfg(SensorConfig):
    """SensorConfig with only the fields the price populator reads overridden."""

    def __init__(self, price_interval: int, slot_interval: int) -> None:
        super().__init__()
        self.electricity_price_update_interval = price_interval
        self.recommendation_interval_minutes = slot_interval
        self.import_electricity_price_sensor = "sensor.eds_import"
        self.import_electricity_price_forecast_sensor = None
        self.export_electricity_price_sensor = "sensor.eds_export"
        self.export_electricity_price_forecast_sensor = None
        self.solcast_pv_forecast_forecast_today = None
        self.solcast_pv_forecast_forecast_tomorrow = None
        self.solcast_pv_forecast_forecast_likelihood = "pv_estimate"


def _populate(recs: list[HourlyRecommendation], attrs: dict, cfg: _Cfg) -> None:
    snapshot = StateSnapshot(
        live=LiveState(), energy_average_values={}, sensor_attributes=attrs
    )
    populate_price_and_solcast_from_snapshot(recs, snapshot, cfg)


class TestQuarterHourlyPriceMatching:
    """96 distinct 15-min prices must land on 96 distinct 15-min slots."""

    def setup_method(self) -> None:
        self.base = datetime(2026, 8, 7, 0, 0, 0, tzinfo=UTC)

    def _quarter_hour_prices(self, count: int = 96) -> list[dict[str, str]]:
        return [
            {
                "start": (self.base + timedelta(minutes=15 * i)).isoformat(),
                "price": f"{1.0 + i * 0.01:.5f}",
            }
            for i in range(count)
        ]

    def test_15min_prices_land_on_distinct_slots(self) -> None:
        """Each 15-min price point must reach exactly its own slot (issue #720)."""
        cfg = _Cfg(price_interval=15, slot_interval=15)
        recs = [
            _make_rec(
                self.base + timedelta(minutes=15 * i),
                self.base + timedelta(minutes=15 * (i + 1)),
            )
            for i in range(96)
        ]
        raw = self._quarter_hour_prices()
        attrs = {
            "sensor.eds_import": {"prices_today": raw},
            "sensor.eds_export": {"prices_today": raw},
        }
        _populate(recs, attrs, cfg)

        for i, rec in enumerate(recs):
            expected = 1.0 + i * 0.01
            assert rec.import_price == pytest.approx(expected, abs=1e-5), (
                f"Slot {i} ({rec.start}): expected {expected}, got {rec.import_price}"
            )
            assert rec.export_price == pytest.approx(expected, abs=1e-5)

    def test_quarter_hour_prices_not_overwritten_within_hour(self) -> None:
        """Prices inside one hour must differ when the source differs.

        Mirrors the exact scenario from issue #720: 20:00-20:45 all showed
        the same price even though the source had 0.097/0.098/0.170/0.188.
        """
        cfg = _Cfg(price_interval=15, slot_interval=15)
        hour20 = self.base.replace(hour=20)
        recs = [
            _make_rec(
                hour20 + timedelta(minutes=15 * i),
                hour20 + timedelta(minutes=15 * (i + 1)),
            )
            for i in range(4)
        ]
        prices = [0.097, 0.098, 0.170, 0.188]
        raw = [
            {
                "start": (hour20 + timedelta(minutes=15 * i)).isoformat(),
                "price": f"{p:.5f}",
            }
            for i, p in enumerate(prices)
        ]
        attrs = {
            "sensor.eds_import": {"prices_today": raw},
            "sensor.eds_export": {"prices_today": raw},
        }
        _populate(recs, attrs, cfg)

        for rec, expected in zip(recs, prices, strict=True):
            assert rec.import_price == pytest.approx(expected, abs=1e-5), (
                f"{rec.start}: expected {expected}, got {rec.import_price}"
            )

    def test_hourly_prices_still_fan_out_to_15min_slots(self) -> None:
        """60-min price config must still replicate the hourly value to all
        four quarter-hour slots (existing behavior preserved)."""
        cfg = _Cfg(price_interval=60, slot_interval=15)
        recs = [
            _make_rec(
                self.base + timedelta(minutes=15 * i),
                self.base + timedelta(minutes=15 * (i + 1)),
            )
            for i in range(8)
        ]
        raw = [
            {
                "start": (self.base + timedelta(hours=h)).isoformat(),
                "price": f"{0.10 + h * 0.05:.5f}",
            }
            for h in range(2)
        ]
        attrs = {
            "sensor.eds_import": {"prices_today": raw},
            "sensor.eds_export": {"prices_today": raw},
        }
        _populate(recs, attrs, cfg)

        share = 60 / 15  # stored value is raw / share
        for i, rec in enumerate(recs):
            hour = i // 4
            expected = (0.10 + hour * 0.05) / share
            assert rec.import_price == pytest.approx(expected, abs=1e-5), (
                f"Slot {i}: expected {expected}, got {rec.import_price}"
            )

    def test_15min_prices_into_60min_slots_use_slot_start(self) -> None:
        """With 60-min slots and 15-min price data, the price at the slot
        start is used (matching the planner's hourly-equivalent resolution)."""
        cfg = _Cfg(price_interval=15, slot_interval=60)
        recs = [
            _make_rec(
                self.base + timedelta(hours=h), self.base + timedelta(hours=h + 1)
            )
            for h in range(2)
        ]
        raw = self._quarter_hour_prices(count=8)
        attrs = {
            "sensor.eds_import": {"prices_today": raw},
            "sensor.eds_export": {"prices_today": raw},
        }
        _populate(recs, attrs, cfg)

        # share = 15/60 = 0.25 → stored = raw / 0.25
        for h, rec in enumerate(recs):
            expected_raw = 1.0 + (h * 4) * 0.01  # price at the hour boundary
            expected = expected_raw / 0.25
            assert rec.import_price == pytest.approx(expected, abs=1e-4), (
                f"Hour {h}: expected {expected}, got {rec.import_price}"
            )


class TestNordpoolRawFormat:
    """Regression tests for issue #750 — nordpool raw_today/raw_tomorrow
    entries use ``start``/``end``/``value`` keys, not ``hour``/``price``.

    Bug
    ---
    ``custom-components/nordpool`` publishes ``raw_today`` and
    ``raw_tomorrow`` attributes as::

        {"start": datetime, "end": datetime, "value": price}

    HSEM's price populator mapped those attributes as ``{"k": "hour",
    "v": "price"}``, so ``data.get("hour")`` returned ``None`` for every
    entry and all prices were silently skipped.  Every planner slot ended
    up with ``import_price = 0.0`` — no error, no warning.

    Fix
    ---
    The ``raw_today`` / ``raw_tomorrow`` mapping now accepts both the
    legacy ``hour``/``price`` format and the nordpool ``start``/``value``
    format.
    """

    def setup_method(self) -> None:
        self.base = datetime(2026, 8, 11, 0, 0, 0, tzinfo=UTC)

    def _nordpool_entries(self, count: int = 96) -> list[dict[str, str]]:
        """Entries in the exact format published by custom-components/nordpool."""
        return [
            {
                "start": (self.base + timedelta(minutes=15 * i)).isoformat(),
                "end": (self.base + timedelta(minutes=15 * (i + 1))).isoformat(),
                "value": f"{0.5 + i * 0.01:.5f}",
            }
            for i in range(count)
        ]

    def test_nordpool_raw_today_prices_ingested(self) -> None:
        """Nordpool-format raw_today entries must land on the correct slots."""
        cfg = _Cfg(price_interval=15, slot_interval=15)
        recs = [
            _make_rec(
                self.base + timedelta(minutes=15 * i),
                self.base + timedelta(minutes=15 * (i + 1)),
            )
            for i in range(96)
        ]
        raw = self._nordpool_entries()
        attrs = {
            "sensor.eds_import": {"raw_today": raw},
            "sensor.eds_export": {"raw_today": raw},
        }
        _populate(recs, attrs, cfg)

        for i, rec in enumerate(recs):
            expected = 0.5 + i * 0.01
            assert rec.import_price == pytest.approx(expected, abs=1e-5), (
                f"Slot {i} ({rec.start}): expected {expected}, got {rec.import_price}"
            )
            assert rec.export_price == pytest.approx(expected, abs=1e-5)

    def test_nordpool_raw_tomorrow_prices_ingested(self) -> None:
        """Nordpool-format raw_tomorrow entries must land on the correct slots."""
        cfg = _Cfg(price_interval=15, slot_interval=15)
        recs = [
            _make_rec(
                self.base + timedelta(minutes=15 * i),
                self.base + timedelta(minutes=15 * (i + 1)),
            )
            for i in range(96)
        ]
        raw = self._nordpool_entries()
        attrs = {
            "sensor.eds_import": {"raw_tomorrow": raw},
            "sensor.eds_export": {"raw_tomorrow": raw},
        }
        _populate(recs, attrs, cfg)

        for i, rec in enumerate(recs):
            expected = 0.5 + i * 0.01
            assert rec.import_price == pytest.approx(expected, abs=1e-5), (
                f"Slot {i} ({rec.start}): expected {expected}, got {rec.import_price}"
            )

    def test_legacy_hour_price_format_still_works(self) -> None:
        """The legacy ``hour``/``price`` format must continue to work."""
        cfg = _Cfg(price_interval=15, slot_interval=15)
        recs = [
            _make_rec(
                self.base + timedelta(minutes=15 * i),
                self.base + timedelta(minutes=15 * (i + 1)),
            )
            for i in range(4)
        ]
        raw = [
            {
                "hour": (self.base + timedelta(minutes=15 * i)).isoformat(),
                "price": f"{0.10 + i * 0.01:.5f}",
            }
            for i in range(4)
        ]
        attrs = {
            "sensor.eds_import": {"raw_today": raw},
            "sensor.eds_export": {"raw_today": raw},
        }
        _populate(recs, attrs, cfg)

        for i, rec in enumerate(recs):
            expected = 0.10 + i * 0.01
            assert rec.import_price == pytest.approx(expected, abs=1e-5), (
                f"Slot {i}: expected {expected}, got {rec.import_price}"
            )

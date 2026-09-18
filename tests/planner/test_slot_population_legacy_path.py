"""Tests for the hour-keyed slot population path used without a time index.

``populate_prices``, ``populate_consumption`` and ``populate_solcast`` accept
``tsi=None``. That path matches source data to slots by wall-clock hour instead
of through the shared slot axis, and is what runs for callers that have no
``TimeSeriesIndex`` — so its scaling and its "no data for this hour" behaviour
both need pinning down.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner.slot_population import (
    clamp_window_to_peer_median,
    detect_outliers_iqr,
    populate_consumption,
    populate_solcast,
    weighted_avg_consumption,
)
from custom_components.hsem.planner.slot_price_population import populate_prices

_DAY = datetime(2026, 6, 1, tzinfo=UTC)


def _slots(*hours: int, minutes: int = 60) -> list[PlannedSlot]:
    """Return one slot per given hour, each *minutes* long."""
    return [
        PlannedSlot(
            start=_DAY + timedelta(hours=hour),
            end=_DAY + timedelta(hours=hour, minutes=minutes),
        )
        for hour in hours
    ]


class TestPopulatePricesByHour:
    """Prices are matched to slots by hour when no index is supplied."""

    def test_matching_hours_receive_their_price(self) -> None:
        """Each slot takes the price point for its own hour."""
        slots = _slots(10, 11)
        points = [
            PricePoint(hour=10, import_price=2.0, export_price=0.5),
            PricePoint(hour=11, import_price=3.0, export_price=1.0),
        ]

        populate_prices(slots, points, None)

        assert slots[0].price.import_price == pytest.approx(2.0)
        assert slots[0].price.export_price == pytest.approx(0.5)
        assert slots[1].price.import_price == pytest.approx(3.0)

    def test_hours_without_a_price_keep_their_default(self) -> None:
        """A slot with no matching price point is left untouched."""
        slots = _slots(10, 23)
        points = [PricePoint(hour=10, import_price=2.0, export_price=0.5)]

        populate_prices(slots, points, None)

        assert slots[1].price.import_price == pytest.approx(0.0)
        assert slots[1].price.export_price == pytest.approx(0.0)


def _average(hour: int, value: float) -> HourlyConsumptionAverage:
    """Return an hourly average whose four windows all read *value*."""
    return HourlyConsumptionAverage(
        hour=hour, avg_1d=value, avg_3d=value, avg_7d=value, avg_14d=value
    )


class TestPopulateConsumptionByHour:
    """Hourly averages are scaled to the slot width when no index is used."""

    def test_hourly_values_are_written_unscaled_for_hourly_slots(self) -> None:
        """A 60-minute slot takes the hourly average as-is."""
        slots = _slots(10)

        populate_consumption(slots, [_average(10, 1.2)], 25, 25, 25, 25, 60, None)

        assert slots[0].avg_house_consumption_kwh == pytest.approx(1.2)
        assert slots[0].avg_house_consumption_1d_kwh == pytest.approx(1.2)
        assert slots[0].avg_house_consumption_14d_kwh == pytest.approx(1.2)

    def test_quarter_hour_slots_receive_a_quarter_of_the_hour(self) -> None:
        """A 15-minute slot takes a quarter of the hourly average."""
        slots = _slots(10, minutes=15)

        populate_consumption(slots, [_average(10, 1.2)], 25, 25, 25, 25, 15, None)

        assert slots[0].avg_house_consumption_kwh == pytest.approx(0.3)
        assert slots[0].avg_house_consumption_3d_kwh == pytest.approx(0.3)

    def test_hours_without_an_average_keep_their_default(self) -> None:
        """A slot with no matching average is left at zero."""
        slots = _slots(10, 23)

        populate_consumption(slots, [_average(10, 1.2)], 25, 25, 25, 25, 60, None)

        assert slots[1].avg_house_consumption_kwh == pytest.approx(0.0)


class TestPopulateSolcastByHour:
    """PV estimates are scaled to the slot width when no index is used."""

    def test_quarter_hour_slots_receive_a_quarter_of_the_hour(self) -> None:
        """A 15-minute slot takes a quarter of the hourly PV estimate."""
        slots = _slots(12, minutes=15)

        populate_solcast(slots, [SolcastSlot(hour=12, pv_estimate=2.0)], 15, None)

        assert slots[0].solcast_pv_estimate_kwh == pytest.approx(0.5)

    def test_hours_without_a_forecast_keep_their_default(self) -> None:
        """A slot with no matching forecast stays at zero."""
        slots = _slots(12, 20)

        populate_solcast(slots, [SolcastSlot(hour=12, pv_estimate=2.0)], 60, None)

        assert slots[1].solcast_pv_estimate_kwh == pytest.approx(0.0)


class TestConsumptionBlendHelpers:
    """The spike-aware blend degrades safely on short or degenerate input."""

    @pytest.mark.parametrize(
        "values",
        [
            pytest.param([], id="empty"),
            pytest.param([1.0], id="single_window"),
        ],
    )
    def test_too_few_windows_are_returned_unclamped(self, values: list[float]) -> None:
        """A peer band needs at least one peer to compare against."""
        assert clamp_window_to_peer_median(values) == values

    def test_a_spike_is_pulled_towards_its_peers(self) -> None:
        """One window far above its peers is clamped into their band."""
        clamped = clamp_window_to_peer_median([10.0, 1.0, 1.0, 1.0])

        assert clamped[0] < 10.0
        assert clamped[1:] == pytest.approx([1.0, 1.0, 1.0])

    @pytest.mark.parametrize(
        "values",
        [
            pytest.param([], id="empty"),
            pytest.param([1.0, 2.0, 3.0], id="three_windows"),
        ],
    )
    def test_outlier_detection_needs_four_windows(self, values: list[float]) -> None:
        """With fewer than four windows the quartiles are undefined."""
        assert detect_outliers_iqr(values) == [False] * len(values)

    def test_zero_configured_weights_leave_nothing_to_redistribute(self) -> None:
        """With every weight at zero the blend falls back to those weights."""
        result, outliers = weighted_avg_consumption(1.0, 1.0, 1.0, 1.0, 0, 0, 0, 0)

        assert result == pytest.approx(0.0)
        assert outliers == [False, False, False, False]

    def test_equal_windows_blend_to_their_shared_value(self) -> None:
        """Four identical windows average to that value regardless of weights."""
        result, _outliers = weighted_avg_consumption(1.2, 1.2, 1.2, 1.2, 25, 25, 25, 25)

        assert result == pytest.approx(1.2)

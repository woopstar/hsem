"""Tests for the shared meter and power helpers (issue #1435)."""

from __future__ import annotations

import pytest

from custom_components.hsem.utils.metering import (
    MAX_METER_KW,
    METER_STEP_KWH,
    mean_power_kwh,
    meter_delta,
)


class TestMeterDelta:
    def test_the_increase_of_a_cumulative_meter(self) -> None:
        assert meter_delta(100.0, 100.5, 0.25) == pytest.approx(0.5)

    @pytest.mark.parametrize(("before", "after"), [(None, 1.0), (1.0, None)])
    def test_a_missing_reading_is_unknown(
        self, before: float | None, after: float | None
    ) -> None:
        assert meter_delta(before, after, 0.25) is None

    def test_a_meter_running_backwards_is_unknown(self) -> None:
        assert meter_delta(100.0, 99.0, 0.25) is None

    def test_an_impossible_power_is_unknown(self) -> None:
        limit = MAX_METER_KW * 0.25 + METER_STEP_KWH

        assert meter_delta(0.0, limit, 0.25) == pytest.approx(limit)
        assert meter_delta(0.0, limit + 0.1, 0.25) is None

    def test_rounding_noise_below_zero_is_zero(self) -> None:
        assert meter_delta(100.0, 100.0 - 1e-12, 0.25) == pytest.approx(0.0)


class TestMeanPowerKwh:
    def test_the_mean_of_two_readings_over_the_interval(self) -> None:
        assert mean_power_kwh(1000.0, 3000.0, 0.5) == pytest.approx(1.0)

    @pytest.mark.parametrize(("before", "after"), [(None, 1.0), (1.0, None)])
    def test_a_missing_reading_is_unknown(
        self, before: float | None, after: float | None
    ) -> None:
        assert mean_power_kwh(before, after, 0.5) is None

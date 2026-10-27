"""Tests for custom_sensors/hourly_data_populator.py.

The core spike-aware weighting algorithm (_compute_weighted_average) is pure
Python and can be tested without any Home Assistant dependency.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.coordinator_builder import (
    generate_recommendation_intervals,
)
from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    populate_price_and_solcast_from_snapshot,
)
from custom_components.hsem.custom_sensors.state_collector import (
    async_collect_all_states,
)
from custom_components.hsem.models.forecast_coverage import ForecastCoverage
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.planner.slot_population import (
    weighted_avg_consumption as _compute_weighted_average,
)
from custom_components.hsem.utils.datetime_utils import utc_key


class TestComputeWeightedAverage:
    """Unit tests for the spike-aware dynamic reweighting algorithm."""

    def test_equal_values_returns_same(self):
        """When all windows have the same value, the weighted average is very close to that value.

        Reliability scaling slightly dampens weights when all windows agree (rel factors
        approach 1 but not exactly), so the result may be fractionally below the input.
        """
        result, mask = _compute_weighted_average(1.0, 1.0, 1.0, 1.0, 50, 20, 15, 10)
        # Should be within 10% of the input value
        assert result == pytest.approx(1.0, rel=0.1)
        assert mask == [False, False, False, False]

    def test_all_zero_values_returns_zero(self):
        result, mask = _compute_weighted_average(0.0, 0.0, 0.0, 0.0, 50, 20, 15, 10)
        assert result == pytest.approx(0.0)
        assert mask == [False, False, False, False]

    def test_spike_in_1d_reduces_its_contribution(self):
        """A large spike in 1d should be flagged as outlier and its weight redistributed."""
        normal = 1.0
        spiked = 10.0  # 10× the baseline — clear spike
        result_spiked, mask_spiked = _compute_weighted_average(
            spiked, normal, normal, normal, 50, 20, 15, 10
        )
        result_normal, _ = _compute_weighted_average(
            normal, normal, normal, normal, 50, 20, 15, 10
        )
        # 1d should be flagged as outlier
        assert mask_spiked[0] is True
        # With 1d weight (50) redistributed to 3d/7d/14d (total 45 scaled to 100),
        # the result equals the weighted average of the non-outlier windows
        # which is 1.0 (all normal values)
        assert result_spiked == pytest.approx(result_normal, abs=0.01)

    def test_weights_summing_to_100(self):
        """Standard 25/30/30/15 weights should produce a sensible result."""
        result, _ = _compute_weighted_average(2.0, 1.8, 1.7, 1.6, 25, 30, 30, 15)
        # Should be between the min and max input values
        assert 1.6 <= result <= 2.0

    def test_all_weights_zero_edge_case(self):
        """w_total == 0 should not raise — returns 0."""
        result, mask = _compute_weighted_average(1.0, 1.0, 1.0, 1.0, 0, 0, 0, 0)
        assert result == pytest.approx(0.0)
        assert mask == [False, False, False, False]

    def test_negative_values_handled(self):
        """Should not raise even with negative consumption values (edge case)."""
        result, mask = _compute_weighted_average(-0.1, -0.05, 0.0, 0.0, 50, 20, 15, 10)
        assert isinstance(result, float)
        assert isinstance(mask, list)

    def test_large_14d_spike_redistributes_to_7d(self):
        """14d spike should redistribute weight towards 7d."""
        normal = 1.0
        spiked_14d = 5.0
        result, mask = _compute_weighted_average(
            normal, normal, normal, spiked_14d, 25, 25, 25, 25
        )
        # Result should be closer to normal (7d) than to spiked_14d
        assert result < spiked_14d
        # 14d should be flagged as outlier
        assert mask[3] is True

    def test_round_trip_symmetry(self):
        """Swapping v1 and v14 with symmetric weights should give different results
        because the reweighting is asymmetric (1d vs 14d are treated differently)."""
        r1, m1 = _compute_weighted_average(3.0, 1.0, 1.0, 1.0, 25, 25, 25, 25)
        r2, m2 = _compute_weighted_average(1.0, 1.0, 1.0, 3.0, 25, 25, 25, 25)
        # They need not be equal — this just confirms the function doesn't crash
        assert isinstance(r1, float)
        assert isinstance(r2, float)
        assert isinstance(m1, list)
        assert isinstance(m2, list)

    def test_downward_outlier_detected(self):
        """A very low 1d value (downward anomaly) should be flagged as outlier."""
        result, mask = _compute_weighted_average(
            0.188, 0.578, 0.708, 0.718, 50, 25, 15, 10
        )
        # 1d (0.188) is far below the other three (0.578, 0.708, 0.718)
        # After capping: 0.188 → 0.80 * baseline ≈ 0.80 * 0.711 = 0.569
        # The IQR on [0.569, 0.578, 0.708, 0.718] should flag 0.569 as outlier
        assert mask[0] is True, "1d should be flagged as downward outlier"
        # The weighted average should be close to the 7d/14d baseline, not pulled down by 1d
        assert result > 0.5, (
            f"Result {result} should be > 0.5 (not dragged down by 1d outlier)"
        )

    def test_repeated_high_load_not_outlier(self):
        """When 1d, 3d, and 7d are all high (repeated load), none should be outlier."""
        _, mask = _compute_weighted_average(2.0, 1.9, 1.8, 1.0, 25, 25, 25, 25)
        # 1d, 3d, 7d are all elevated — this is a trend, not a spike
        # Median of [1.0, 1.8, 1.9, 2.0] = (1.8+1.9)/2 = 1.85
        # Upper fence: 1.85 * 1.5 = 2.775 — all values below
        # Lower fence: 1.85 / 1.5 = 1.233 — 1.0 is below this
        # So 14d (1.0) IS flagged as outlier since it's the old normal
        # But 1d, 3d, 7d are NOT outliers — they represent the new trend
        assert not mask[0], "1d should not be outlier (part of trend)"
        assert not mask[1], "3d should not be outlier (part of trend)"
        assert not mask[2], "7d should not be outlier (part of trend)"


# ---------------------------------------------------------------------------
# Snapshot-based population tests
# ---------------------------------------------------------------------------


class TestSnapshotPopulation:
    """Verify that the snapshot-based population functions work deterministically.

    These tests construct :class:`StateSnapshot` objects in memory and confirm
    the population produces the same results as the original pipeline logic,
    without any HA state lookups.
    """

    def test_populate_avg_consumption_from_snapshot(self) -> None:
        """populate_avg_house_consumption_from_snapshot must fill slots from pre-read data."""
        from unittest.mock import MagicMock

        from custom_components.hsem.custom_sensors.hourly_data_populator.consumption import (
            populate_avg_house_consumption_from_snapshot,
        )
        from custom_components.hsem.models.hourly_recommendation import (
            HourlyRecommendation,
        )
        from custom_components.hsem.models.live_state import LiveState
        from custom_components.hsem.models.sensor_config import SensorConfig
        from custom_components.hsem.models.state_snapshot import StateSnapshot
        from custom_components.hsem.utils.sensornames.energy import (
            get_energy_average_sensor_unique_id,
        )

        # Build a mock config with default weights
        cfg = MagicMock(spec=SensorConfig)
        cfg.recommendation_interval_minutes = 60
        cfg.house_consumption_energy_weight_1d = 25
        cfg.house_consumption_energy_weight_3d = 30
        cfg.house_consumption_energy_weight_7d = 30
        cfg.house_consumption_energy_weight_14d = 15

        # Build entity ID cache and snapshot with pre-read values
        eid_cache: dict[str, str] = {}
        energy_average_values: dict[str, float] = {}
        for h in range(24):
            hour_end = (h + 1) % 24
            for days, val in [(1, 1.0), (3, 1.0), (7, 1.0), (14, 1.0)]:
                uid = get_energy_average_sensor_unique_id(
                    "test_entry_id", h, hour_end, days
                )
                eid = f"sensor.energy_avg_{h:02d}_{days}d"
                eid_cache[uid] = eid
                energy_average_values[eid] = float(val)

        snapshot = StateSnapshot(
            live=LiveState(),
            energy_average_values=energy_average_values,
        )

        # Generate 24 hourly recommendation slots
        from datetime import datetime, timedelta

        base = datetime(2025, 1, 1, 0, 0, 0)
        _hz = 0.0
        recs = [
            HourlyRecommendation(
                start=base + timedelta(hours=h),
                end=base + timedelta(hours=h + 1),
                recommendation="idle",
                avg_house_consumption_kwh=_hz,
                avg_house_consumption_1d_kwh=_hz,
                avg_house_consumption_3d_kwh=_hz,
                avg_house_consumption_7d_kwh=_hz,
                avg_house_consumption_14d_kwh=_hz,
                batteries_charged_kwh=_hz,
                batteries_discharged_kwh=_hz,
                estimated_battery_capacity_kwh=_hz,
                estimated_battery_soc_pct=_hz,
                estimated_cost_currency=_hz,
                estimated_net_consumption_kwh=_hz,
                export_price=_hz,
                grid_export_kwh=_hz,
                grid_import_kwh=_hz,
                import_price=_hz,
                solcast_pv_estimate_kwh=_hz,
            )
            for h in range(24)
        ]

        result = populate_avg_house_consumption_from_snapshot(
            recs, snapshot, cfg, eid_cache, entry_id="test_entry_id"
        )

        assert result.ok is True
        assert result.estimated_hours == ()
        for _h, rec in enumerate(recs):
            assert rec.avg_house_consumption_kwh > 0.0
            assert rec.avg_house_consumption_1d_kwh > 0.0

        # Reproduce: calling again with same snapshot must give identical result
        recs2 = [
            HourlyRecommendation(
                start=base + timedelta(hours=h),
                end=base + timedelta(hours=h + 1),
                recommendation="idle",
                avg_house_consumption_kwh=_hz,
                avg_house_consumption_1d_kwh=_hz,
                avg_house_consumption_3d_kwh=_hz,
                avg_house_consumption_7d_kwh=_hz,
                avg_house_consumption_14d_kwh=_hz,
                batteries_charged_kwh=_hz,
                batteries_discharged_kwh=_hz,
                estimated_battery_capacity_kwh=_hz,
                estimated_battery_soc_pct=_hz,
                estimated_cost_currency=_hz,
                estimated_net_consumption_kwh=_hz,
                export_price=_hz,
                grid_export_kwh=_hz,
                grid_import_kwh=_hz,
                import_price=_hz,
                solcast_pv_estimate_kwh=_hz,
            )
            for h in range(24)
        ]
        populate_avg_house_consumption_from_snapshot(
            recs2, snapshot, cfg, eid_cache, entry_id="test_entry_id"
        )
        for r1, r2 in zip(recs, recs2, strict=False):
            assert r1.avg_house_consumption_kwh == pytest.approx(
                r2.avg_house_consumption_kwh
            )
            assert r1.avg_house_consumption_1d_kwh == pytest.approx(
                r2.avg_house_consumption_1d_kwh
            )

    def test_snapshot_determinism_reproduces_plan(self) -> None:
        """A single snapshot must be able to reproduce the same plan.

        Two calls to the snapshot-based population with the same data must
        produce identical recommendation slots, confirming the snapshot is
        immutable and deterministic.
        """
        from unittest.mock import MagicMock

        from custom_components.hsem.custom_sensors.hourly_data_populator.consumption import (
            populate_avg_house_consumption_from_snapshot,
        )
        from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
            populate_price_and_solcast_from_snapshot,
        )
        from custom_components.hsem.models.hourly_recommendation import (
            HourlyRecommendation,
        )
        from custom_components.hsem.models.live_state import LiveState
        from custom_components.hsem.models.sensor_config import SensorConfig
        from custom_components.hsem.models.state_snapshot import StateSnapshot
        from custom_components.hsem.utils.sensornames.energy import (
            get_energy_average_sensor_unique_id,
        )

        cfg = MagicMock(spec=SensorConfig)
        cfg.recommendation_interval_minutes = 60
        cfg.recommendation_interval_length = 24
        cfg.house_consumption_energy_weight_1d = 25
        cfg.house_consumption_energy_weight_3d = 30
        cfg.house_consumption_energy_weight_7d = 30
        cfg.house_consumption_energy_weight_14d = 15
        cfg.electricity_price_update_interval = 60
        cfg.import_electricity_price_sensor = "sensor.eds_import"
        cfg.export_electricity_price_sensor = "sensor.eds_export"
        cfg.solcast_pv_forecast_forecast_today = "sensor.solcast_today"
        cfg.solcast_pv_forecast_forecast_tomorrow = None
        cfg.solcast_pv_forecast_forecast_likelihood = "pv_estimate"

        # Build energy average cache and values
        eid_cache: dict[str, str] = {}
        energy_average_values: dict[str, float] = {}
        for h in range(24):
            hour_end = (h + 1) % 24
            base_val = 0.3 + (h * 0.02)
            for days, val in [
                (1, base_val),
                (3, base_val * 1.05),
                (7, base_val * 1.10),
                (14, base_val * 1.15),
            ]:
                uid = get_energy_average_sensor_unique_id(
                    "test_entry_id", h, hour_end, days
                )
                eid = f"sensor.energy_avg_{h:02d}_{days}d"
                eid_cache[uid] = eid
                energy_average_values[eid] = float(val)

        # Build mock sensor attributes for EDS
        from datetime import UTC, datetime

        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        prices_today = [
            {
                "start": (now.replace(hour=h, minute=0)).isoformat(),
                "price": 0.3 + h * 0.01,
            }
            for h in range(24)
        ]
        sensor_attributes = {
            "sensor.eds_import": {"prices_today": prices_today},
            "sensor.eds_export": {"prices_today": prices_today},
            "sensor.solcast_today": {
                "detailedHourly": [
                    {
                        "period_start": (now.replace(hour=h, minute=0)).isoformat(),
                        "pv_estimate": h * 0.1,
                    }
                    for h in range(24)
                ]
            },
        }

        snapshot = StateSnapshot(
            live=LiveState(),
            energy_average_values=energy_average_values,
            sensor_attributes=sensor_attributes,
        )

        # Helper to create a recommendation slot with zeroed fields
        _hz = 0.0

        def _make_rec(start, end):
            return HourlyRecommendation(
                start=start,
                end=end,
                recommendation="idle",
                avg_house_consumption_kwh=_hz,
                avg_house_consumption_1d_kwh=_hz,
                avg_house_consumption_3d_kwh=_hz,
                avg_house_consumption_7d_kwh=_hz,
                avg_house_consumption_14d_kwh=_hz,
                batteries_charged_kwh=_hz,
                batteries_discharged_kwh=_hz,
                estimated_battery_capacity_kwh=_hz,
                estimated_battery_soc_pct=_hz,
                estimated_cost_currency=_hz,
                estimated_net_consumption_kwh=_hz,
                export_price=_hz,
                grid_export_kwh=_hz,
                grid_import_kwh=_hz,
                import_price=_hz,
                solcast_pv_estimate_kwh=_hz,
            )

        # First population
        from datetime import datetime as dt, timedelta as td

        base = dt(2025, 1, 1, 0, 0, 0)
        recs1 = [
            _make_rec(base + td(hours=h), base + td(hours=h + 1)) for h in range(24)
        ]

        populate_avg_house_consumption_from_snapshot(
            recs1, snapshot, cfg, eid_cache, entry_id="test_entry_id"
        )
        populate_price_and_solcast_from_snapshot(recs1, snapshot, cfg)

        # Second population with identical data
        recs2 = [
            _make_rec(base + td(hours=h), base + td(hours=h + 1)) for h in range(24)
        ]
        populate_avg_house_consumption_from_snapshot(
            recs2, snapshot, cfg, eid_cache, entry_id="test_entry_id"
        )
        populate_price_and_solcast_from_snapshot(recs2, snapshot, cfg)

        # Assert determinism
        for i, (r1, r2) in enumerate(zip(recs1, recs2, strict=False)):
            assert r1.avg_house_consumption_kwh == pytest.approx(
                r2.avg_house_consumption_kwh
            ), f"Hour {i}: avg_house_consumption_kwh differs between runs"
            assert r1.import_price == pytest.approx(r2.import_price), (
                f"Hour {i}: import_price differs between runs"
            )
            assert r1.export_price == pytest.approx(r2.export_price), (
                f"Hour {i}: export_price differs between runs"
            )
            assert r1.solcast_pv_estimate_kwh == pytest.approx(
                r2.solcast_pv_estimate_kwh
            ), f"Hour {i}: solcast_pv_estimate_kwh differs between runs"


class TestPeerMedianClamp:
    """Peer-median clamp: no single window may dominate when the other
    three agree (issue #592 — stale 14d pollution after a behavioural
    change)."""

    def test_stale_14d_clamped_toward_clean_peers(self):
        """Extati's 02:00-03:00 slot: 1d/3d/7d clean (~0.02), 14d polluted
        at 2.14.  The clamped blend must land near the clean windows, not
        the raw blend of 1.72."""
        result, _ = _compute_weighted_average(0.01, 0.02, 0.03, 2.14, 25, 30, 30, 15)
        assert result == pytest.approx(0.12, abs=0.05)

    def test_stale_14d_moderate_pollution(self):
        """01:00-02:00 slot: 14d at 1.75 vs clean ~0.1 peers."""
        result, _ = _compute_weighted_average(0.07, 0.07, 0.15, 1.75, 25, 30, 30, 15)
        assert result < 0.30  # raw blend was 1.49

    def test_clean_windows_untouched(self):
        """All four windows in agreement → no clamping, blend unchanged."""
        result, mask = _compute_weighted_average(0.37, 0.38, 0.39, 0.45, 25, 30, 30, 15)
        assert result == pytest.approx(0.386, abs=0.02)
        assert mask == [False, False, False, False]

    def test_gradual_trend_not_clamped_to_zero(self):
        """A genuine rising trend (heat pump season) where all windows
        move together must pass through — the clamp only bites on
        disagreement."""
        result, _ = _compute_weighted_average(2.0, 1.9, 1.8, 1.7, 25, 30, 30, 15)
        assert 1.7 <= result <= 2.0

    def test_low_window_never_inflated_upward(self):
        """A window reading BELOW its peers is kept as-is — only the
        upward side is clamped (a genuine consumption drop must flow
        through immediately)."""
        from custom_components.hsem.planner.slot_population import (
            clamp_window_to_peer_median,
        )

        out = clamp_window_to_peer_median([0.01, 0.9, 1.0, 1.1])
        assert out[0] == pytest.approx(0.01)  # low 1d preserved

    def test_near_zero_peers_use_absolute_floor(self):
        """With peers at ~0, the band floor (0.15 kWh) still allows clamping."""
        from custom_components.hsem.planner.slot_population import (
            clamp_window_to_peer_median,
        )

        out = clamp_window_to_peer_median([0.01, 0.01, 0.01, 2.0])
        assert out[3] == pytest.approx(0.15)  # clamped to the floor


_FLAT_NOW = datetime(2026, 10, 20, 9, 5, tzinfo=ZoneInfo("Europe/Berlin"))
_FLAT_MIDNIGHT = _FLAT_NOW.replace(hour=0, minute=0)
_FLAT_IMPORT = "sensor.epex_spot_data_total_price"
_FLAT_EXPORT = "sensor.feed_in_tariff"
_FLAT_IMPORT_FORECAST = "sensor.import_price_forecast"


def _epex_data(day_start: datetime, quarters: Iterable[int]) -> dict[str, Any]:
    """Return EPEX Spot ``data`` attributes, one 15-minute price per quarter."""
    return {
        "data": [
            {
                "start_time": (day_start + timedelta(minutes=15 * q)).isoformat(),
                "end_time": (day_start + timedelta(minutes=15 * (q + 1))).isoformat(),
                "price_per_kwh": round(0.3 + q / 1000.0, 5),
            }
            for q in quarters
        ]
    }


def _populate_flat(
    sensor_states: dict[str, Any],
    sensor_attributes: dict[str, dict[str, Any]],
    *,
    import_forecast_sensor: str | None = None,
) -> tuple[list[HourlyRecommendation], ForecastCoverage]:
    """Populate a 48 h quarter-hourly horizon from the given price sensors."""
    cfg = SensorConfig()
    cfg.electricity_price_update_interval = 15
    cfg.import_electricity_price_sensor = _FLAT_IMPORT
    cfg.export_electricity_price_sensor = _FLAT_EXPORT
    cfg.import_electricity_price_forecast_sensor = import_forecast_sensor
    with patch("homeassistant.util.dt.now", return_value=_FLAT_NOW):
        recs = generate_recommendation_intervals(15, 48)
    coverage = populate_price_and_solcast_from_snapshot(
        recs,
        StateSnapshot(
            live=LiveState(),
            sensor_attributes=sensor_attributes,
            sensor_states=sensor_states,
        ),
        cfg,
    )
    return recs, coverage


def _all_slots(recs: list[HourlyRecommendation]) -> frozenset[datetime]:
    return frozenset(utc_key(rec.start) for rec in recs)


class TestFlatTariffPriceSensor:
    """A price sensor with a numeric state and no forecast attribute (issue #978).

    A fixed feed-in tariff published as a plain sensor state used to cover no
    slot, so the export side was missing everywhere and the builder dropped
    every price point.
    """

    def test_flat_export_with_forecast_import_covers_every_slot(self) -> None:
        recs, coverage = _populate_flat(
            {_FLAT_IMPORT: "0.348741", _FLAT_EXPORT: "0.0778"},
            {
                _FLAT_IMPORT: _epex_data(_FLAT_MIDNIGHT, range(96)),
                _FLAT_EXPORT: {"unit_of_measurement": "EUR/kWh"},
            },
        )

        # The import forecast covers today only; the import sensor's numeric
        # state is the current price, not a flat price for tomorrow.
        assert coverage.export_price == _all_slots(recs)
        assert [rec.export_price for rec in recs] == pytest.approx([0.0778] * len(recs))
        today = [rec for rec in recs if rec.start.date() == _FLAT_MIDNIGHT.date()]
        assert coverage.import_price == _all_slots(today)
        assert today[4].import_price == pytest.approx(0.304)
        assert all(coverage.has_price(rec.start) for rec in today)

    def test_flat_import_and_export_cover_every_slot(self) -> None:
        recs, coverage = _populate_flat(
            {_FLAT_IMPORT: "0.25", _FLAT_EXPORT: "0.08"},
            {_FLAT_IMPORT: {}, _FLAT_EXPORT: {}},
        )

        assert coverage.import_price == _all_slots(recs)
        assert coverage.export_price == _all_slots(recs)
        assert [rec.import_price for rec in recs] == pytest.approx([0.25] * len(recs))
        assert [rec.export_price for rec in recs] == pytest.approx([0.08] * len(recs))

    def test_flat_price_keeps_more_than_three_decimals(self) -> None:
        """The raw state is used, not LiveState's 3-decimal reading."""
        recs, _coverage = _populate_flat(
            {_FLAT_IMPORT: "0.25", _FLAT_EXPORT: "0.07784"},
            {_FLAT_IMPORT: {}, _FLAT_EXPORT: {}},
        )

        assert recs[0].export_price == pytest.approx(0.07784)

    def test_a_flat_zero_price_is_a_price(self) -> None:
        recs, coverage = _populate_flat(
            {_FLAT_IMPORT: "0.25", _FLAT_EXPORT: "0"},
            {_FLAT_IMPORT: {}, _FLAT_EXPORT: {}},
        )

        assert coverage.export_price == _all_slots(recs)

    @pytest.mark.parametrize("state", ["unavailable", "unknown", "", "nan", "inf"])
    def test_unusable_flat_state_covers_nothing(
        self, state: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="custom_components.hsem"):
            recs, coverage = _populate_flat(
                {_FLAT_IMPORT: "0.25", _FLAT_EXPORT: state},
                {_FLAT_IMPORT: {}, _FLAT_EXPORT: {}},
            )

        assert coverage.import_price == _all_slots(recs)
        assert coverage.export_price == frozenset()
        assert [rec.export_price for rec in recs] == pytest.approx([0.0] * len(recs))
        assert "No export price data matched" in caplog.text
        assert "plans with 0.0" not in caplog.text

    def test_sensor_missing_from_the_snapshot_covers_nothing(self) -> None:
        _recs, coverage = _populate_flat({_FLAT_IMPORT: "0.25"}, {_FLAT_IMPORT: {}})

        assert coverage.export_price == frozenset()

    def test_gaps_in_a_forecast_stay_gaps(self) -> None:
        """A forecast sensor's numeric state never fills the slots it skips."""
        quarters = [q for q in range(96) if not 40 <= q < 48]
        recs, coverage = _populate_flat(
            {_FLAT_IMPORT: "0.348741", _FLAT_EXPORT: "0.08"},
            {_FLAT_IMPORT: _epex_data(_FLAT_MIDNIGHT, quarters), _FLAT_EXPORT: {}},
        )

        today = recs[:96]
        assert coverage.import_price == frozenset(
            utc_key(today[q].start) for q in quarters
        )
        assert today[40].import_price == pytest.approx(0.0)
        assert not coverage.has_price(today[40].start)
        assert coverage.has_price(today[39].start)

    @pytest.mark.parametrize("attribute", ["data", "prices_tomorrow", "raw_today"])
    def test_an_empty_forecast_attribute_is_not_a_flat_price(
        self, attribute: str
    ) -> None:
        """A forecast sensor with nothing published yet keeps every slot a gap."""
        _recs, coverage = _populate_flat(
            {_FLAT_IMPORT: "0.348741", _FLAT_EXPORT: "0.08"},
            {_FLAT_IMPORT: {attribute: []}, _FLAT_EXPORT: {}},
        )

        assert coverage.import_price == frozenset()

    def test_a_forecast_sensor_takes_precedence_over_a_flat_sensor(self) -> None:
        recs, coverage = _populate_flat(
            {_FLAT_IMPORT: "0.25", _FLAT_EXPORT: "0.08"},
            {
                _FLAT_IMPORT: {},
                _FLAT_EXPORT: {},
                _FLAT_IMPORT_FORECAST: _epex_data(_FLAT_MIDNIGHT, range(4)),
            },
            import_forecast_sensor=_FLAT_IMPORT_FORECAST,
        )

        assert coverage.import_price == _all_slots(recs)
        assert recs[1].import_price == pytest.approx(0.301)
        assert recs[4].import_price == pytest.approx(0.25)

    def test_flat_price_is_logged_at_debug_not_warned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="custom_components.hsem"):
            _populate_flat(
                {_FLAT_IMPORT: "0.25", _FLAT_EXPORT: "0.08"},
                {_FLAT_IMPORT: {}, _FLAT_EXPORT: {}},
            )

        assert "using its state 0.08 as a flat price" in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.asyncio
    async def test_snapshot_carries_the_raw_price_sensor_state(self) -> None:
        """The collector keeps the unrounded state next to the attributes."""
        cfg = SensorConfig()
        cfg.import_electricity_price_sensor = _FLAT_IMPORT
        cfg.export_electricity_price_sensor = _FLAT_EXPORT
        states = {
            _FLAT_IMPORT: MagicMock(state="0.348741", attributes={"data": []}),
            _FLAT_EXPORT: MagicMock(state="0.07784", attributes={}),
        }
        sensor = MagicMock()
        sensor.hass.states.get.side_effect = states.get

        async def resolve(_sensor: object, _cache: dict[str, str], uid: str) -> str:
            return f"sensor.{uid}"

        with (
            patch(
                "custom_components.hsem.custom_sensors.state_collector.async_collect_live_state",
                new=AsyncMock(return_value=(LiveState(), None, [])),
            ),
            patch(
                "custom_components.hsem.custom_sensors.state_collector._resolve_cached",
                new=resolve,
            ),
            patch(
                "custom_components.hsem.custom_sensors.state_collector.ha_get_entity_state_and_convert",
                return_value=None,
            ),
        ):
            snapshot, _force, _unsubs = await async_collect_all_states(
                sensor, cfg, None, set(), {}, entry_id="test_entry"
            )

        assert snapshot.sensor_states == {
            _FLAT_IMPORT: "0.348741",
            _FLAT_EXPORT: "0.07784",
        }
        assert snapshot.sensor_attributes == {
            _FLAT_IMPORT: {"data": []},
            _FLAT_EXPORT: {},
        }

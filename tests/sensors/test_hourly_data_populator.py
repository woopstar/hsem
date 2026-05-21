"""Tests for custom_sensors/hourly_data_populator.py.

The core spike-aware weighting algorithm (_compute_weighted_average) is pure
Python and can be tested without any Home Assistant dependency.
"""

from __future__ import annotations

import pytest

from custom_components.hsem.planner.slot_population import (
    weighted_avg_consumption as _compute_weighted_average,
)


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
        result_normal, mask_normal = _compute_weighted_average(
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
        result, mask = _compute_weighted_average(2.0, 1.8, 1.7, 1.6, 25, 30, 30, 15)
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
        result, mask = _compute_weighted_average(2.0, 1.9, 1.8, 1.0, 25, 25, 25, 25)
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

        from custom_components.hsem.custom_sensors.hourly_data_populator import (
            populate_avg_house_consumption_from_snapshot,
        )
        from custom_components.hsem.models.hourly_recommendation import (
            HourlyRecommendation,
        )
        from custom_components.hsem.models.live_state import LiveState
        from custom_components.hsem.models.sensor_config import SensorConfig
        from custom_components.hsem.models.state_snapshot import StateSnapshot
        from custom_components.hsem.utils.sensornames import (
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
                uid = get_energy_average_sensor_unique_id(h, hour_end, days)
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
            recs, snapshot, cfg, eid_cache
        )

        assert result is True
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
        populate_avg_house_consumption_from_snapshot(recs2, snapshot, cfg, eid_cache)
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

        from custom_components.hsem.custom_sensors.hourly_data_populator import (
            populate_avg_house_consumption_from_snapshot,
            populate_price_and_solcast_from_snapshot,
        )
        from custom_components.hsem.models.hourly_recommendation import (
            HourlyRecommendation,
        )
        from custom_components.hsem.models.live_state import LiveState
        from custom_components.hsem.models.sensor_config import SensorConfig
        from custom_components.hsem.models.state_snapshot import StateSnapshot
        from custom_components.hsem.utils.sensornames import (
            get_energy_average_sensor_unique_id,
        )

        cfg = MagicMock(spec=SensorConfig)
        cfg.recommendation_interval_minutes = 60
        cfg.recommendation_interval_length = 24
        cfg.house_consumption_energy_weight_1d = 25
        cfg.house_consumption_energy_weight_3d = 30
        cfg.house_consumption_energy_weight_7d = 30
        cfg.house_consumption_energy_weight_14d = 15
        cfg.energi_data_service_update_interval = 60
        cfg.energi_data_service_import = "sensor.eds_import"
        cfg.energi_data_service_export = "sensor.eds_export"
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
                uid = get_energy_average_sensor_unique_id(h, hour_end, days)
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
        from datetime import datetime as dt
        from datetime import timedelta as td

        base = dt(2025, 1, 1, 0, 0, 0)
        recs1 = [
            _make_rec(base + td(hours=h), base + td(hours=h + 1)) for h in range(24)
        ]

        tz = UTC
        populate_avg_house_consumption_from_snapshot(recs1, snapshot, cfg, eid_cache)
        populate_price_and_solcast_from_snapshot(recs1, snapshot, cfg, tz)

        # Second population with identical data
        recs2 = [
            _make_rec(base + td(hours=h), base + td(hours=h + 1)) for h in range(24)
        ]
        populate_avg_house_consumption_from_snapshot(recs2, snapshot, cfg, eid_cache)
        populate_price_and_solcast_from_snapshot(recs2, snapshot, cfg, tz)

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

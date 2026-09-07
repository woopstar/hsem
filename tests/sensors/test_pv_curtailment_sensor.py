"""Tests for the HSEMPVTailedSensor ("PV Curtailment") diagnostic sensor.

Regression coverage for issue #924: the sensor previously reported
``"curtailed"`` any time the inverter's active-power-control register showed
*any* limit — including the routine, always-applied
``max_grid_export_power_kw`` DNO/grid cap that the applier writes for every
non-negative export price (issue #767). That made the sensor report
``"curtailed"`` permanently for any installation with a configured grid
export cap, regardless of whether real price/SoC-driven curtailment was
happening.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.hsem.coordinator import CoordinatorData
from custom_components.hsem.custom_sensors.pv_curtailment_sensor import (
    HSEMPVTailedSensor,
    _is_derived_curtailment,
    _is_directly_limited,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sensor(data: CoordinatorData | None = None) -> HSEMPVTailedSensor:
    """Return a bare HSEMPVTailedSensor wired to a mock coordinator."""
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.last_update_success = data is not None

    sensor = object.__new__(HSEMPVTailedSensor)
    sensor.coordinator = coordinator
    sensor._config_entry = MagicMock()
    sensor._attr_unique_id = "hsem_pv_curtailment_sensor"
    sensor.entity_id = "sensor.hsem_pv_curtailment_sensor"
    sensor._restored_state = None
    return sensor


def _make_data(cfg: SensorConfig, live: LiveState) -> CoordinatorData:
    data = CoordinatorData()
    data.cfg = cfg
    data.live = live
    return data


# ===========================================================================
# _is_directly_limited — pure helper
# ===========================================================================


class TestIsDirectlyLimited:
    """Unit tests for the direct active-power-control detection method."""

    def test_none_state_is_not_limited(self):
        assert _is_directly_limited(None, None) is False

    def test_unlimited_is_not_limited(self):
        cfg = SensorConfig()
        cfg.max_grid_export_power_kw = 10.0
        assert _is_directly_limited("Unlimited", cfg) is False

    def test_unlimited_localized(self):
        cfg = SensorConfig()
        assert _is_directly_limited("Unbegrenzt", cfg) is False

    def test_no_cap_configured_any_limit_is_curtailment(self):
        """Legacy behaviour preserved when no grid export cap is configured."""
        cfg = SensorConfig()
        cfg.max_grid_export_power_kw = 0.0
        assert _is_directly_limited("Limited to 80%", cfg) is True

    def test_none_cfg_falls_back_to_legacy_behaviour(self):
        assert _is_directly_limited("Limited to 80%", None) is True

    def test_limit_matching_configured_cap_is_not_curtailment(self):
        """Regression test for issue #924.

        The applier always writes the configured grid-export cap in watts
        for any non-negative export price. Reading that same cap back must
        NOT be reported as curtailment — it is the routine steady state.
        """
        cfg = SensorConfig()
        cfg.max_grid_export_power_kw = 10.0
        assert _is_directly_limited("Limited to 10000W", cfg) is False

    def test_limit_matching_cap_within_tolerance_is_not_curtailment(self):
        cfg = SensorConfig()
        cfg.max_grid_export_power_kw = 10.0
        assert _is_directly_limited("Limited to 9998W", cfg) is False

    def test_limit_below_configured_cap_is_curtailment(self):
        """A watt limit meaningfully below the configured cap is real
        curtailment (e.g. the negative-export-price 100 W block)."""
        cfg = SensorConfig()
        cfg.max_grid_export_power_kw = 10.0
        assert _is_directly_limited("Limited to 100W", cfg) is True

    def test_percentage_limit_with_watt_cap_configured_is_curtailment(self):
        cfg = SensorConfig()
        cfg.max_grid_export_power_kw = 10.0
        assert _is_directly_limited("Limited to 50%", cfg) is True

    def test_unparseable_limit_is_reported_as_curtailment(self):
        cfg = SensorConfig()
        cfg.max_grid_export_power_kw = 10.0
        assert _is_directly_limited("some other value", cfg) is True


# ===========================================================================
# _is_derived_curtailment — unchanged fallback heuristic
# ===========================================================================


class TestIsDerivedCurtailment:
    def test_no_pv_production_is_not_curtailed(self):
        live = LiveState()
        live.solar_production_power_w = 0.0
        assert _is_derived_curtailment(live) is False

    def test_low_soc_is_not_curtailed(self):
        live = LiveState()
        live.solar_production_power_w = 500.0
        live.huawei_batteries_soc_pct = 50.0
        live.export_electricity_price = 0.0
        live.huawei_inverter_active_power_control = None
        assert _is_derived_curtailment(live) is False

    def test_export_price_above_threshold_is_not_curtailed(self):
        live = LiveState()
        live.solar_production_power_w = 500.0
        live.huawei_batteries_soc_pct = 99.0
        live.export_electricity_price = 0.5
        live.huawei_inverter_active_power_control = None
        assert _is_derived_curtailment(live) is False

    def test_known_register_overrides_derived_heuristic(self):
        live = LiveState()
        live.solar_production_power_w = 500.0
        live.huawei_batteries_soc_pct = 99.0
        live.export_electricity_price = 0.0
        live.huawei_inverter_active_power_control = "Unlimited"
        assert _is_derived_curtailment(live) is False

    def test_full_battery_blocked_export_unknown_register_is_curtailed(self):
        live = LiveState()
        live.solar_production_power_w = 500.0
        live.huawei_batteries_soc_pct = 99.0
        live.export_electricity_price = 0.0
        live.huawei_inverter_active_power_control = None
        assert _is_derived_curtailment(live) is True


# ===========================================================================
# HSEMPVTailedSensor.state — end-to-end
# ===========================================================================


class TestState:
    def test_no_coordinator_data_defaults_to_normal(self):
        sensor = _make_sensor(None)
        assert sensor.state == "normal"

    def test_issue_924_configured_cap_at_steady_state_is_normal(self):
        """Reproduces the exact issue #924 report: a 10 kW configured grid
        export cap, non-negative export price, register reading the cap
        back verbatim — must be "normal", not "curtailed"."""
        cfg = SensorConfig()
        cfg.max_grid_export_power_kw = 10.0

        live = LiveState()
        live.solar_production_power_w = 6000.0
        live.huawei_batteries_soc_pct = 100.0
        live.export_electricity_price = 0.0
        live.huawei_inverter_active_power_control = "Limited to 10000W"

        sensor = _make_sensor(_make_data(cfg, live))
        assert sensor.state == "normal"

    def test_negative_price_block_below_cap_is_curtailed(self):
        cfg = SensorConfig()
        cfg.max_grid_export_power_kw = 10.0

        live = LiveState()
        live.solar_production_power_w = 6000.0
        live.huawei_batteries_soc_pct = 100.0
        live.export_electricity_price = -0.05
        live.huawei_inverter_active_power_control = "Limited to 100W"

        sensor = _make_sensor(_make_data(cfg, live))
        assert sensor.state == "curtailed"

    def test_no_cap_configured_any_limit_is_curtailed(self):
        cfg = SensorConfig()
        cfg.max_grid_export_power_kw = 0.0

        live = LiveState()
        live.solar_production_power_w = 6000.0
        live.huawei_batteries_soc_pct = 100.0
        live.export_electricity_price = 0.0
        live.huawei_inverter_active_power_control = "Limited to 50%"

        sensor = _make_sensor(_make_data(cfg, live))
        assert sensor.state == "curtailed"

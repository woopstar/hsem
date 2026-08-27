"""Tests for the live per-phase Huawei grid-charge safety limiter (issue #831).

Covers:
- :mod:`utils.phase_power`: :func:`phase_powers_valid` and
  :func:`compute_phase_charge_limits` (the pure phase-math core).
- :mod:`custom_sensors.phase_charge_limiter`: :func:`build_phase_aware_charge_commands`
  (the recommendation-aware wrapper used by the applier).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.const import DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE
from custom_components.hsem.custom_sensors.applier import async_apply_battery_settings
from custom_components.hsem.custom_sensors.phase_charge_limiter import (
    build_phase_aware_charge_commands,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import ApplyResult, ApplyStatus
from custom_components.hsem.utils.phase_power import (
    compute_phase_charge_limits,
    phase_powers_valid,
)
from custom_components.hsem.utils.recommendations import Recommendations

_LOGGER_PATCH = "custom_components.hsem.utils.logger.HSEM_LOGGER.debug"

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _rec(
    *,
    recommendation: str | None = Recommendations.BatteriesChargeGrid.value,
    batteries_charged_kwh: float = 2.5,
) -> HourlyRecommendation:
    """Return a minimal HourlyRecommendation for a one-hour slot."""
    return HourlyRecommendation(
        start=_NOW,
        end=_NOW + timedelta(hours=1),
        recommendation=recommendation,
        avg_house_consumption_kwh=0.0,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=batteries_charged_kwh,
        batteries_discharged_kwh=0.0,
        estimated_battery_capacity_kwh=0.0,
        estimated_battery_soc_pct=50.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.0,
        export_price=0.0,
        grid_export_kwh=0.0,
        grid_import_kwh=0.0,
        import_price=1.0,
        solcast_pv_estimate_kwh=0.0,
    )


def _config() -> SensorConfig:
    cfg = SensorConfig()
    cfg.phase_aware_charging_enabled = True
    cfg.main_fuse_amps = 25
    cfg.main_fuse_phases = 3
    cfg.batteries_charge_efficiency = 98.0
    cfg.batteries_discharge_efficiency = 98.0
    return cfg


# ---------------------------------------------------------------------------
# phase_powers_valid
# ---------------------------------------------------------------------------


class TestPhasePowersValid:
    def test_all_finite_is_valid(self):
        assert phase_powers_valid((700.0, 1200.0, 1700.0)) is True

    def test_any_none_is_invalid(self):
        assert phase_powers_valid((700.0, None, 1700.0)) is False

    def test_any_nan_is_invalid(self):
        assert phase_powers_valid((700.0, float("nan"), 1700.0)) is False

    def test_any_infinite_is_invalid(self):
        assert phase_powers_valid((700.0, float("inf"), 1700.0)) is False

    def test_all_none_is_invalid(self):
        assert phase_powers_valid((None, None, None)) is False


# ---------------------------------------------------------------------------
# compute_phase_charge_limits
# ---------------------------------------------------------------------------


class TestComputePhaseChargeLimits:
    def test_full_charge_granted_when_headroom_available(self):
        """25 A / 3-phase fuse gives 5750 W/phase; light load leaves ample room."""
        result = compute_phase_charge_limits(
            measured_phase_power_w=(700.0, 1200.0, 1700.0),
            fuse_amps=25.0,
            desired_charge_power_w=5000.0,
            battery_actual_power_w=0.0,
            charge_efficiency_pct=98.0,
            discharge_efficiency_pct=98.0,
        )
        assert result.primary_charge_power_w == pytest.approx(5000.0)
        assert max(result.predicted_phase_power_w) <= 25.0 * 230.0 + 1e-6

    def test_charge_throttled_when_phase_near_fuse_limit(self):
        """An appliance spike on one phase must cap the charge, not just warn."""
        result = compute_phase_charge_limits(
            measured_phase_power_w=(700.0, 1200.0, 5500.0),
            fuse_amps=25.0,
            desired_charge_power_w=5000.0,
            battery_actual_power_w=0.0,
            charge_efficiency_pct=98.0,
            discharge_efficiency_pct=98.0,
        )
        assert result.primary_charge_power_w < 5000.0
        assert max(result.predicted_phase_power_w) <= 25.0 * 230.0 + 1e-6

    def test_own_battery_contribution_is_removed_before_headroom_check(self):
        """A battery already charging at full power must not starve itself."""
        # base_phase_power already includes ~1020 W/phase from a 3000 W charge
        # at 98% efficiency (3000 / 0.98 / 3 ≈ 1020.4 W/phase).
        result = compute_phase_charge_limits(
            measured_phase_power_w=(1717.0, 2217.0, 2717.0),
            fuse_amps=25.0,
            desired_charge_power_w=5000.0,
            battery_actual_power_w=3000.0,
            charge_efficiency_pct=98.0,
            discharge_efficiency_pct=98.0,
        )
        assert result.primary_charge_power_w == pytest.approx(5000.0)

    def test_discharging_battery_contribution_is_removed_too(self):
        """A discharging battery is suppressing import; removing it recovers headroom."""
        # Battery discharging at 3000 W lowers the meter by ~980 W/phase
        # (3000 * 0.98 / 3 ≈ 980 W/phase), so the raw meter under-reports load.
        result = compute_phase_charge_limits(
            measured_phase_power_w=(-280.0, 220.0, 720.0),
            fuse_amps=25.0,
            desired_charge_power_w=5000.0,
            battery_actual_power_w=-3000.0,
            charge_efficiency_pct=98.0,
            discharge_efficiency_pct=98.0,
        )
        assert result.primary_charge_power_w == pytest.approx(5000.0)

    def test_zero_fuse_amps_yields_zero_charge(self):
        result = compute_phase_charge_limits(
            measured_phase_power_w=(0.0, 0.0, 0.0),
            fuse_amps=0.0,
            desired_charge_power_w=5000.0,
            battery_actual_power_w=0.0,
            charge_efficiency_pct=98.0,
            discharge_efficiency_pct=98.0,
        )
        assert result.primary_charge_power_w == pytest.approx(0.0)

    def test_zero_desired_charge_yields_zero_regardless_of_headroom(self):
        result = compute_phase_charge_limits(
            measured_phase_power_w=(0.0, 0.0, 0.0),
            fuse_amps=25.0,
            desired_charge_power_w=0.0,
            battery_actual_power_w=0.0,
            charge_efficiency_pct=98.0,
            discharge_efficiency_pct=98.0,
        )
        assert result.primary_charge_power_w == pytest.approx(0.0)

    def test_command_is_floored_to_a_100w_step(self):
        result = compute_phase_charge_limits(
            measured_phase_power_w=(700.0, 1200.0, 1700.0),
            fuse_amps=25.0,
            desired_charge_power_w=4567.0,
            battery_actual_power_w=0.0,
            charge_efficiency_pct=98.0,
            discharge_efficiency_pct=98.0,
        )
        assert result.primary_charge_power_w % 100.0 == pytest.approx(0.0, abs=1e-6)
        assert result.primary_charge_power_w <= 4567.0


# ---------------------------------------------------------------------------
# build_phase_aware_charge_commands
# ---------------------------------------------------------------------------


class TestBuildPhaseAwareChargeCommands:
    def test_disabled_feature_returns_no_override(self):
        cfg = _config()
        cfg.phase_aware_charging_enabled = False
        live = LiveState()
        commands = build_phase_aware_charge_commands(cfg, live, _rec())
        assert commands.primary_grid_charge_power_w is None

    def test_non_grid_charge_slot_returns_no_override(self):
        cfg = _config()
        live = LiveState()
        live.grid_phase_power_w = (700.0, 1200.0, 1700.0)
        live.huawei_batteries_charge_discharge_power_w = 0.0
        commands = build_phase_aware_charge_commands(
            cfg, live, _rec(recommendation=Recommendations.BatteriesWaitMode.value)
        )
        assert commands.primary_grid_charge_power_w is None

    def test_invalid_fuse_configuration_fails_closed(self):
        cfg = _config()
        cfg.main_fuse_phases = 1
        live = LiveState()
        live.grid_phase_power_w = (700.0, 1200.0, 1700.0)
        live.huawei_batteries_charge_discharge_power_w = 0.0
        commands = build_phase_aware_charge_commands(cfg, live, _rec())
        assert commands.primary_grid_charge_power_w == pytest.approx(0.0)

    def test_missing_phase_reading_fails_closed(self):
        cfg = _config()
        live = LiveState()
        live.grid_phase_power_w = (700.0, None, 1700.0)
        live.huawei_batteries_charge_discharge_power_w = 0.0
        commands = build_phase_aware_charge_commands(cfg, live, _rec())
        assert commands.primary_grid_charge_power_w == pytest.approx(0.0)

    def test_missing_battery_power_fails_closed(self):
        cfg = _config()
        live = LiveState()
        live.grid_phase_power_w = (700.0, 1200.0, 1700.0)
        live.huawei_batteries_charge_discharge_power_w = None
        commands = build_phase_aware_charge_commands(cfg, live, _rec())
        assert commands.primary_grid_charge_power_w == pytest.approx(0.0)

    def test_non_finite_battery_power_fails_closed(self):
        cfg = _config()
        live = LiveState()
        live.grid_phase_power_w = (700.0, 1200.0, 1700.0)
        live.huawei_batteries_charge_discharge_power_w = float("nan")
        commands = build_phase_aware_charge_commands(cfg, live, _rec())
        assert commands.primary_grid_charge_power_w == pytest.approx(0.0)

    def test_valid_inputs_compute_a_safe_command(self):
        cfg = _config()
        live = LiveState()
        live.grid_phase_power_w = (700.0, 1200.0, 1700.0)
        live.huawei_batteries_charge_discharge_power_w = 0.0
        commands = build_phase_aware_charge_commands(
            cfg, live, _rec(batteries_charged_kwh=2.5)
        )
        # 2.5 kWh over a 1-hour slot = 2500 W desired.
        assert commands.primary_grid_charge_power_w == pytest.approx(2500.0)
        assert commands.limits is not None

    def test_command_respects_live_max_charge_power_cap(self):
        cfg = _config()
        live = LiveState()
        live.grid_phase_power_w = (700.0, 1200.0, 1700.0)
        live.huawei_batteries_charge_discharge_power_w = 0.0
        live.huawei_batteries_max_charge_power_w = 2000.0
        commands = build_phase_aware_charge_commands(
            cfg, live, _rec(batteries_charged_kwh=2.5)
        )
        assert commands.primary_grid_charge_power_w is not None
        assert commands.primary_grid_charge_power_w <= 2000.0

    def test_phase_spike_throttles_the_command(self):
        cfg = _config()
        live = LiveState()
        live.grid_phase_power_w = (700.0, 1200.0, 5500.0)
        live.huawei_batteries_charge_discharge_power_w = 0.0
        commands = build_phase_aware_charge_commands(
            cfg, live, _rec(batteries_charged_kwh=2.5)
        )
        assert commands.primary_grid_charge_power_w is not None
        assert commands.primary_grid_charge_power_w < 2500.0


# ---------------------------------------------------------------------------
# Integration: async_apply_battery_settings actually writes the cap
# ---------------------------------------------------------------------------


def _make_sensor() -> MagicMock:
    sensor = MagicMock()
    sensor.hass = MagicMock()
    return sensor


async def _write_and_verify_ok(entity_id, desired, writer, reader, **kwargs):  # type: ignore[no-untyped-def]  # local test shim mirrors async_write_and_verify signature
    await writer()
    return ApplyResult(
        entity_id=entity_id,
        desired=desired,
        actual=desired,
        status=ApplyStatus.OK,
        attempts=1,
    )


class TestApplierWritesPhaseAwareCap:
    """End-to-end: async_apply_battery_settings writes the computed cap."""

    @pytest.mark.asyncio
    async def test_writes_computed_cap_to_grid_charge_entity(self):
        sensor = _make_sensor()
        cfg = _config()
        cfg.read_only = False
        cfg.huawei_solar_batteries_grid_charge_maximum_power = "number.gcmp"
        cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = "sensor.tou"
        live = LiveState()
        live._degraded_mode = DegradedMode.OK
        live.grid_phase_power_w = (700.0, 1200.0, 1700.0)
        live.huawei_batteries_charge_discharge_power_w = 0.0
        live.huawei_batteries_grid_charge_max_power_w = 0.0
        live.huawei_batteries_max_discharge_power_w = 2500
        live.huawei_batteries_excess_pv_use_in_tou = "charge"
        live.tou_periods.periods = list(DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE)
        rec = _rec(batteries_charged_kwh=2.5)

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                side_effect=_write_and_verify_ok,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_number_value",
                new_callable=AsyncMock,
            ) as mock_set_number,
        ):
            summary = await async_apply_battery_settings(sensor, cfg, live, rec, 0.0)

        mock_set_number.assert_awaited_once_with(sensor, "number.gcmp", 2500)
        assert any(r.entity_id == "number.gcmp" for r in summary.results)

    @pytest.mark.asyncio
    async def test_skips_write_when_target_matches_live_value(self):
        """No-op guard: an unchanged cap must not trigger a redundant write."""
        sensor = _make_sensor()
        cfg = _config()
        cfg.read_only = False
        cfg.huawei_solar_batteries_grid_charge_maximum_power = "number.gcmp"
        cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = "sensor.tou"
        live = LiveState()
        live._degraded_mode = DegradedMode.OK
        live.grid_phase_power_w = (700.0, 1200.0, 1700.0)
        live.huawei_batteries_charge_discharge_power_w = 0.0
        live.huawei_batteries_grid_charge_max_power_w = 2500.0
        live.huawei_batteries_max_discharge_power_w = 2500
        live.huawei_batteries_excess_pv_use_in_tou = "charge"
        live.tou_periods.periods = list(DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE)
        rec = _rec(batteries_charged_kwh=2.5)

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                side_effect=_write_and_verify_ok,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_number_value",
                new_callable=AsyncMock,
            ) as mock_set_number,
        ):
            await async_apply_battery_settings(sensor, cfg, live, rec, 0.0)

        mock_set_number.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_grid_charge_entity_blocks_further_writes(self):
        """No configured write target means the cycle must stop, not skip silently."""
        sensor = _make_sensor()
        cfg = _config()
        cfg.read_only = False
        cfg.huawei_solar_batteries_grid_charge_maximum_power = None
        live = LiveState()
        live._degraded_mode = DegradedMode.OK
        live.grid_phase_power_w = (700.0, 1200.0, 1700.0)
        live.huawei_batteries_charge_discharge_power_w = 0.0
        live.huawei_batteries_grid_charge_max_power_w = None
        live.huawei_batteries_max_discharge_power_w = 2500
        rec = _rec(batteries_charged_kwh=2.5)

        with patch(_LOGGER_PATCH, new_callable=MagicMock):
            summary = await async_apply_battery_settings(sensor, cfg, live, rec, 0.0)

        assert summary.results == []

    @pytest.mark.asyncio
    async def test_disabled_feature_does_not_touch_grid_charge_entity(self):
        """phase_aware_charging_enabled=False must not write the cap at all."""
        sensor = _make_sensor()
        cfg = _config()
        cfg.phase_aware_charging_enabled = False
        cfg.read_only = False
        cfg.huawei_solar_batteries_grid_charge_maximum_power = "number.gcmp"
        cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = "sensor.tou"
        live = LiveState()
        live._degraded_mode = DegradedMode.OK
        live.huawei_batteries_max_discharge_power_w = 2500
        live.huawei_batteries_excess_pv_use_in_tou = "charge"
        live.tou_periods.periods = list(DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE)
        rec = _rec(batteries_charged_kwh=2.5)

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                side_effect=_write_and_verify_ok,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_number_value",
                new_callable=AsyncMock,
            ) as mock_set_number,
        ):
            await async_apply_battery_settings(sensor, cfg, live, rec, 0.0)

        mock_set_number.assert_not_awaited()

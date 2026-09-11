"""Tests for the Error-mode Huawei grid-charge emergency stop (issue #840).

Covers:
- :mod:`custom_sensors.applier_emergency_stop`: the pure ownership/telemetry
  helpers and :func:`async_emergency_disable_grid_charge`.
- :class:`GridChargeEmergencyStopMixin`: ownership latch/release lifecycle,
  mixed into :class:`HSEMWorkingModeSensor`.
- The top-level gate in ``_async_apply_hardware_writes``: Error mode blocks
  every ordinary write but still runs the narrow emergency stop when HSEM
  owns an armed charge, and never claims an externally-armed one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.const import DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE
from custom_components.hsem.custom_sensors.applier_emergency_stop import (
    async_emergency_disable_grid_charge,
    huawei_grid_charge_emergency_needed,
    primary_grid_charge_is_known_disarmed,
    summary_verifies_zero_grid_charge,
)
from custom_components.hsem.custom_sensors.working_mode_sensor import (
    HSEMWorkingModeSensor,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import (
    ApplyResult,
    ApplyStatus,
    CycleApplySummary,
)
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.workingmodes import WorkingModes

_LOGGER_PATCH = "custom_components.hsem.utils.logger.HSEM_LOGGER.debug"
_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _rec(
    *,
    recommendation: str | None = Recommendations.BatteriesChargeGrid.value,
) -> HourlyRecommendation:
    return HourlyRecommendation(
        start=_NOW,
        end=_NOW + timedelta(hours=1),
        recommendation=recommendation,
        avg_house_consumption_kwh=0.0,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=2.5,
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


def _config(*, phase_aware_charging_enabled: bool = True) -> SensorConfig:
    cfg = SensorConfig()
    cfg.phase_aware_charging_enabled = phase_aware_charging_enabled
    cfg.huawei_solar_batteries_grid_charge_maximum_power = "number.gcmp"
    return cfg


def _armed_live(*, cap_w: float = 5900.0) -> LiveState:
    """Return a LiveState snapshot for an actively armed grid charge."""
    live = LiveState()
    live.huawei_batteries_grid_charge_max_power_w = cap_w
    live.huawei_batteries_working_mode = WorkingModes.TimeOfUse.value
    live.tou_periods.raw_state = "active"
    live.tou_periods.periods = list(DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE)
    return live


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


async def _write_and_verify_failed(entity_id, desired, writer, reader, **kwargs):  # type: ignore[no-untyped-def]  # local test shim mirrors async_write_and_verify signature
    return ApplyResult(
        entity_id=entity_id,
        desired=desired,
        actual=5900.0,
        status=ApplyStatus.FAILED,
        attempts=3,
    )


# ---------------------------------------------------------------------------
# primary_grid_charge_is_known_disarmed
# ---------------------------------------------------------------------------


class TestPrimaryGridChargeIsKnownDisarmed:
    def test_armed_charge_is_not_disarmed(self):
        live = _armed_live()
        assert primary_grid_charge_is_known_disarmed(live) is False

    def test_zero_cap_is_disarmed(self):
        live = _armed_live(cap_w=0.0)
        assert primary_grid_charge_is_known_disarmed(live) is True

    def test_non_tou_working_mode_is_disarmed(self):
        live = _armed_live()
        live.huawei_batteries_working_mode = WorkingModes.MaximizeSelfConsumption.value
        assert primary_grid_charge_is_known_disarmed(live) is True

    def test_tou_periods_not_matching_force_charge_is_disarmed(self):
        live = _armed_live()
        live.tou_periods.periods = ["06:00-09:00/1234567/+"]
        assert primary_grid_charge_is_known_disarmed(live) is True

    def test_unknown_cap_and_unknown_working_mode_is_not_disarmed(self):
        """Missing telemetry must not be mistaken for proof of a safe state."""
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = None
        live.huawei_batteries_working_mode = None
        assert primary_grid_charge_is_known_disarmed(live) is False


# ---------------------------------------------------------------------------
# huawei_grid_charge_emergency_needed
# ---------------------------------------------------------------------------


class TestHuaweiGridChargeEmergencyNeeded:
    def test_current_grid_charge_recommendation_needs_emergency_stop(self):
        cfg = _config()
        live = _armed_live()
        rec = _rec()
        assert huawei_grid_charge_emergency_needed(cfg, live, rec, False) is True

    def test_latched_ownership_needs_emergency_stop_even_without_current_rec(self):
        cfg = _config()
        live = _armed_live()
        assert huawei_grid_charge_emergency_needed(cfg, live, None, True) is True

    def test_externally_armed_charge_is_never_claimed(self):
        """No HSEM ownership signal (current rec nor latch) -> never touched."""
        cfg = _config()
        live = _armed_live()
        rec = _rec(recommendation=Recommendations.BatteriesWaitMode.value)
        assert huawei_grid_charge_emergency_needed(cfg, live, rec, False) is False

    def test_disabled_feature_never_needs_emergency_stop(self):
        cfg = _config(phase_aware_charging_enabled=False)
        live = _armed_live()
        rec = _rec()
        assert huawei_grid_charge_emergency_needed(cfg, live, rec, True) is False

    def test_already_disarmed_does_not_need_emergency_stop(self):
        cfg = _config()
        live = _armed_live(cap_w=0.0)
        rec = _rec()
        assert huawei_grid_charge_emergency_needed(cfg, live, rec, True) is False


# ---------------------------------------------------------------------------
# summary_verifies_zero_grid_charge
# ---------------------------------------------------------------------------


class TestSummaryVerifiesZeroGridCharge:
    def test_verified_zero_write_is_recognised(self):
        cfg = _config()
        summary = CycleApplySummary(
            results=[
                ApplyResult(
                    entity_id="number.gcmp",
                    desired=0.0,
                    actual=0.0,
                    status=ApplyStatus.OK,
                    attempts=1,
                )
            ]
        )
        assert summary_verifies_zero_grid_charge(cfg, summary) is True

    def test_failed_write_is_not_recognised(self):
        cfg = _config()
        summary = CycleApplySummary(
            results=[
                ApplyResult(
                    entity_id="number.gcmp",
                    desired=0.0,
                    actual=5900.0,
                    status=ApplyStatus.FAILED,
                    attempts=3,
                )
            ]
        )
        assert summary_verifies_zero_grid_charge(cfg, summary) is False

    def test_no_entity_configured_is_not_recognised(self):
        cfg = _config()
        cfg.huawei_solar_batteries_grid_charge_maximum_power = None
        summary = CycleApplySummary(
            results=[
                ApplyResult(
                    entity_id="number.gcmp",
                    desired=0.0,
                    actual=0.0,
                    status=ApplyStatus.OK,
                    attempts=1,
                )
            ]
        )
        assert summary_verifies_zero_grid_charge(cfg, summary) is False


# ---------------------------------------------------------------------------
# async_emergency_disable_grid_charge
# ---------------------------------------------------------------------------


class TestAsyncEmergencyDisableGridCharge:
    @pytest.mark.asyncio
    async def test_writes_and_verifies_zero(self):
        sensor = _make_sensor()
        cfg = _config()
        live = _armed_live()

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier_emergency_stop.async_write_and_verify",
                side_effect=_write_and_verify_ok,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier_emergency_stop.async_set_number_value",
                new_callable=AsyncMock,
            ) as mock_set_number,
        ):
            summary = await async_emergency_disable_grid_charge(sensor, cfg, live)

        mock_set_number.assert_awaited_once_with(sensor, "number.gcmp", 0.0)
        assert summary.results[0].status == ApplyStatus.OK
        assert summary.results[0].desired == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_read_only_is_a_no_op(self):
        sensor = _make_sensor()
        cfg = _config()
        cfg.read_only = True
        live = _armed_live()

        with patch(_LOGGER_PATCH, new_callable=MagicMock):
            summary = await async_emergency_disable_grid_charge(sensor, cfg, live)

        assert summary.results == []

    @pytest.mark.asyncio
    async def test_already_zero_is_a_no_op(self):
        sensor = _make_sensor()
        cfg = _config()
        live = _armed_live(cap_w=0.0)

        with patch(_LOGGER_PATCH, new_callable=MagicMock):
            summary = await async_emergency_disable_grid_charge(sensor, cfg, live)

        assert summary.results == []

    @pytest.mark.asyncio
    async def test_missing_entity_records_a_failed_result(self):
        sensor = _make_sensor()
        cfg = _config()
        cfg.huawei_solar_batteries_grid_charge_maximum_power = None
        live = _armed_live()

        with patch(_LOGGER_PATCH, new_callable=MagicMock):
            summary = await async_emergency_disable_grid_charge(sensor, cfg, live)

        assert len(summary.results) == 1
        assert summary.results[0].status == ApplyStatus.FAILED


# ---------------------------------------------------------------------------
# GridChargeEmergencyStopMixin — ownership lifecycle
# ---------------------------------------------------------------------------


class TestGridChargeEmergencyStopMixinLifecycle:
    def _make_working_mode_sensor(self) -> HSEMWorkingModeSensor:
        cfg_entry = MagicMock()
        cfg_entry.entry_id = "test_entry_id_emergency_stop"
        cfg_entry.options = {}
        cfg_entry.data = {}
        coord = MagicMock()
        coord.data = None
        coord.last_update_success = True
        sensor = HSEMWorkingModeSensor(cfg_entry, coord)
        sensor.hass = MagicMock()
        return sensor

    def test_ownership_defaults_to_false(self):
        sensor = self._make_working_mode_sensor()
        assert sensor._primary_grid_charge_owned is False

    @pytest.mark.asyncio
    async def test_owned_charge_triggers_emergency_stop(self):
        sensor = self._make_working_mode_sensor()
        cfg = _config()
        live = _armed_live()
        rec = _rec()

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier_emergency_stop.async_write_and_verify",
                side_effect=_write_and_verify_ok,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier_emergency_stop.async_set_number_value",
                new_callable=AsyncMock,
            ),
        ):
            summary = await sensor._async_run_error_mode_emergency_stop(
                sensor, cfg, live, rec
            )

        assert summary.overall_status == ApplyStatus.OK
        # Verified 0 W write releases ownership again.
        assert sensor._primary_grid_charge_owned is False

    @pytest.mark.asyncio
    async def test_externally_armed_charge_is_not_touched(self):
        sensor = self._make_working_mode_sensor()
        cfg = _config()
        live = _armed_live()
        rec = _rec(recommendation=Recommendations.BatteriesWaitMode.value)

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier_emergency_stop.async_write_and_verify",
                new_callable=AsyncMock,
            ) as mock_wv,
        ):
            summary = await sensor._async_run_error_mode_emergency_stop(
                sensor, cfg, live, rec
            )

        mock_wv.assert_not_awaited()
        assert summary.results == []
        assert sensor._primary_grid_charge_owned is False

    @pytest.mark.asyncio
    async def test_failed_stop_retains_ownership_for_retry(self):
        sensor = self._make_working_mode_sensor()
        cfg = _config()
        live = _armed_live()
        rec = _rec()

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier_emergency_stop.async_write_and_verify",
                side_effect=_write_and_verify_failed,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier_emergency_stop.async_set_number_value",
                new_callable=AsyncMock,
            ),
        ):
            summary = await sensor._async_run_error_mode_emergency_stop(
                sensor, cfg, live, rec
            )

        assert summary.overall_status == ApplyStatus.FAILED
        assert sensor._primary_grid_charge_owned is True

        # A later cycle with no current recommendation must still retry,
        # because ownership was latched.
        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier_emergency_stop.async_write_and_verify",
                side_effect=_write_and_verify_ok,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier_emergency_stop.async_set_number_value",
                new_callable=AsyncMock,
            ),
        ):
            retry_summary = await sensor._async_run_error_mode_emergency_stop(
                sensor, cfg, live, None
            )

        assert retry_summary.overall_status == ApplyStatus.OK
        assert sensor._primary_grid_charge_owned is False

    def test_release_if_safe_clears_ownership_on_verified_disarm(self):
        sensor = self._make_working_mode_sensor()
        sensor._primary_grid_charge_owned = True
        cfg = _config()
        live = _armed_live(cap_w=0.0)

        sensor._release_primary_grid_charge_ownership_if_safe(cfg, live)

        assert sensor._primary_grid_charge_owned is False

    def test_release_if_safe_keeps_ownership_while_still_armed(self):
        sensor = self._make_working_mode_sensor()
        sensor._primary_grid_charge_owned = True
        cfg = _config()
        live = _armed_live()

        sensor._release_primary_grid_charge_ownership_if_safe(cfg, live)

        assert sensor._primary_grid_charge_owned is True

    def test_release_if_safe_clears_ownership_when_feature_disabled(self):
        sensor = self._make_working_mode_sensor()
        sensor._primary_grid_charge_owned = True
        cfg = _config(phase_aware_charging_enabled=False)
        live = _armed_live()

        sensor._release_primary_grid_charge_ownership_if_safe(cfg, live)

        assert sensor._primary_grid_charge_owned is False


# ---------------------------------------------------------------------------
# Top-level gate integration: _async_apply_hardware_writes in Error mode
# ---------------------------------------------------------------------------


def _make_cfg(*, read_only: bool = False) -> SensorConfig:
    cfg = _config()
    cfg.read_only = read_only
    cfg.export_electricity_min_price = 0.0
    return cfg


def _make_live_error_mode() -> LiveState:
    live = _armed_live()
    live._degraded_mode = DegradedMode.Error
    live.export_electricity_price = 1.0
    live.missing_entities_list = ["Missing entity: batteries_state_of_capacity"]
    return live


class TestErrorModeTopLevelGateIntegration:
    def _make_coordinator_data(self, *, rec: HourlyRecommendation | None) -> MagicMock:
        cfg = _make_cfg(read_only=False)
        live = _make_live_error_mode()

        data = MagicMock()
        data.cfg = cfg
        data.live = live
        data.hourly_recommendation = rec
        data.current_required_battery = 0.0
        data.apply_summary = None
        return data

    @pytest.mark.asyncio
    async def test_error_mode_stops_hsem_owned_charge(self):
        data = self._make_coordinator_data(rec=_rec())

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.working_mode_sensor.resolve_current_recommendation"
            ),
        ):
            sensor = MagicMock(spec=HSEMWorkingModeSensor)
            sensor.hass = MagicMock()
            sensor._primary_grid_charge_owned = False
            sensor._release_primary_grid_charge_ownership_if_safe = MagicMock()
            sensor._async_run_error_mode_emergency_stop = AsyncMock(
                return_value=CycleApplySummary(
                    results=[
                        ApplyResult(
                            entity_id="number.gcmp",
                            desired=0.0,
                            actual=0.0,
                            status=ApplyStatus.OK,
                            attempts=1,
                        )
                    ]
                )
            )

            await HSEMWorkingModeSensor._async_apply_hardware_writes(sensor, data)

        sensor._async_run_error_mode_emergency_stop.assert_awaited_once_with(
            sensor, data.cfg, data.live, data.hourly_recommendation
        )
        assert data.apply_summary.results[0].status == ApplyStatus.OK

    @pytest.mark.asyncio
    async def test_error_mode_does_not_touch_externally_armed_charge(self):
        data = self._make_coordinator_data(
            rec=_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        )

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.working_mode_sensor.resolve_current_recommendation"
            ),
        ):
            sensor = MagicMock(spec=HSEMWorkingModeSensor)
            sensor.hass = MagicMock()
            sensor._primary_grid_charge_owned = False
            sensor._release_primary_grid_charge_ownership_if_safe = MagicMock()
            sensor._async_run_error_mode_emergency_stop = AsyncMock(
                return_value=CycleApplySummary()
            )

            await HSEMWorkingModeSensor._async_apply_hardware_writes(sensor, data)

        sensor._async_run_error_mode_emergency_stop.assert_awaited_once()
        assert data.apply_summary.results == []

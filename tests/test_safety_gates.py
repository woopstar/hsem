"""Safety gate tests for inverter/battery hardware write paths (issue P0).

These tests prove that every combination of blocking mode prevents writes from
reaching the Huawei Solar service layer, and that normal mode still allows
them when valid data is present.

Covered scenarios
-----------------
- ``read_only=True`` blocks both :func:`async_apply_inverter_power_control` and
  :func:`async_apply_battery_settings`.
- ``DegradedMode.Error`` blocks both applier functions.
- ``DegradedMode.Degraded`` (non-critical entities missing) still allows writes.
- Normal mode (``read_only=False``, ``DegradedMode.OK``) allows writes.
- The top-level gate in ``_async_apply_hardware_writes`` (working_mode_sensor)
  logs the correct message for each blocking scenario.

All tests are pure-Python and require no running Home Assistant instance.

Note on logging
----------------
``HSEM_LOGGER.debug`` is patched with a no-op ``MagicMock`` in every test so
that planner/applier output never reaches the standard ``custom_components.hsem``
logger during the test run.  This keeps test output clean and decouples the
safety-gate assertions from log-formatting changes.
"""

from __future__ import annotations

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from custom_components.hsem.const import DEFAULT_HSEM_BATTERIES_WAIT_MODE
from custom_components.hsem.custom_sensors.applier import (
    async_apply_battery_settings,
    async_apply_inverter_power_control,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import ApplyResult, ApplyStatus

# ---------------------------------------------------------------------------
# Module-level patch targets (reused across all test classes)
# ---------------------------------------------------------------------------

# Patch HSEM_LOGGER.debug to suppress log output during tests.
# Use MagicMock because debug() is a synchronous method; AsyncMock would
# return a coroutine that never gets awaited and emits RuntimeWarnings.
_LOGGER_PATCH = "custom_components.hsem.utils.logger.HSEM_LOGGER.debug"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sensor():
    """Return a minimal mock sensor for testing."""
    sensor = MagicMock()
    sensor.hass = MagicMock()
    return sensor


def _make_cfg(*, read_only: bool = False) -> SensorConfig:
    """Return a minimal :class:`SensorConfig` with the given read_only flag."""
    cfg = SensorConfig()
    cfg.read_only = read_only
    cfg.export_electricity_min_price = 0.0
    return cfg


def _make_live(*, degraded_mode: DegradedMode = DegradedMode.OK) -> LiveState:
    """Return a :class:`LiveState` with the chosen degraded mode forced."""
    live = LiveState()
    # Override the lazily-computed cached value directly so no entities need
    # to be set up just to drive the mode.
    live._degraded_mode = degraded_mode
    live.export_electricity_price = 1.0
    return live


def _make_rec(recommendation: str = "batteries_discharge_mode") -> HourlyRecommendation:
    """Return a minimal :class:`HourlyRecommendation` for testing."""
    rec = HourlyRecommendation.__new__(HourlyRecommendation)
    object.__setattr__(rec, "recommendation", recommendation)
    # async_apply_battery_settings derives primary_battery_hold /
    # held_planned_export from these energy fields unconditionally
    # (issue #797); a bare __new__() instance has no dataclass defaults.
    object.__setattr__(rec, "batteries_charged_kwh", 0.0)
    object.__setattr__(rec, "batteries_discharged_kwh", 0.0)
    object.__setattr__(rec, "grid_export_kwh", 0.0)
    return rec


# ---------------------------------------------------------------------------
# async_apply_inverter_power_control — safety gate
# ---------------------------------------------------------------------------


class TestInverterPowerControlSafetyGate:
    """Defense-in-depth gate inside async_apply_inverter_power_control."""

    @pytest.mark.asyncio
    async def test_read_only_blocks_inverter_writes(self):
        """read_only=True must return an empty summary without any service call."""
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=True)
        live = _make_live(degraded_mode=DegradedMode.OK)

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier_power_control.async_set_grid_export_power_pct"
            ) as mock_write,
        ):
            summary = await async_apply_inverter_power_control(sensor, cfg, live)

        mock_write.assert_not_called()
        assert len(summary.results) == 0

    @pytest.mark.asyncio
    async def test_error_mode_blocks_inverter_writes(self):
        """DegradedMode.Error must block all inverter writes."""
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=False)
        live = _make_live(degraded_mode=DegradedMode.Error)

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier_power_control.async_set_grid_export_power_pct"
            ) as mock_write,
        ):
            summary = await async_apply_inverter_power_control(sensor, cfg, live)

        mock_write.assert_not_called()
        assert len(summary.results) == 0

    @pytest.mark.asyncio
    async def test_degraded_mode_allows_inverter_writes(self):
        """DegradedMode.Degraded must NOT block writes (non-critical data only)."""
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=False)
        # Degraded: price entity missing, but battery data present.
        live = _make_live(degraded_mode=DegradedMode.Degraded)
        # Set a numeric export price so the function can compute export_pct.
        live.export_electricity_price = 0.5
        # Set current inverter state to a watt-based limit so the applier must
        # restore unlimited/100% export (issue #767).
        live.huawei_inverter_active_power_control = "Limited to 100W"

        # Set up an inverter device ID so the write loop has something to call.
        cfg.huawei_solar_device_id_inverter_1 = "device_123"
        cfg.huawei_solar_inverter_active_power_control = (
            "sensor.inverter_active_power_control"
        )
        cfg.export_electricity_min_price = 1.0

        # Make the HA state read return the same watt-limited entity.
        mock_state = MagicMock()
        mock_state.state = "Limited to 100W"
        sensor.hass.states.get.return_value = mock_state

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier_power_control.async_write_and_verify",
                new_callable=AsyncMock,
            ) as mock_wv,
        ):
            from custom_components.hsem.utils.inverter_verify import ApplyResult

            mock_wv.return_value = ApplyResult(
                entity_id="sensor.inverter_active_power_control",
                desired=100,
                actual=100,
                status=ApplyStatus.OK,
                attempts=1,
            )
            _summary = await async_apply_inverter_power_control(sensor, cfg, live)

        # The write-and-verify function should have been reached (not blocked).
        mock_wv.assert_called_once()

    @pytest.mark.asyncio
    async def test_normal_mode_allows_inverter_writes(self):
        """OK mode with read_only=False must reach the write path."""
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=False)
        cfg.huawei_solar_device_id_inverter_1 = "device_abc"
        cfg.huawei_solar_inverter_active_power_control = (
            "sensor.inverter_active_power_control"
        )
        cfg.export_electricity_min_price = 1.0

        live = _make_live(degraded_mode=DegradedMode.OK)
        live.export_electricity_price = 0.5
        # Force a write by starting from a watt-limited state (legacy state).
        live.huawei_inverter_active_power_control = "Limited to 100W"

        mock_state = MagicMock()
        mock_state.state = "Limited to 100W"
        sensor.hass.states.get.return_value = mock_state

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier_power_control.async_write_and_verify",
                new_callable=AsyncMock,
            ) as mock_wv,
        ):
            from custom_components.hsem.utils.inverter_verify import ApplyResult

            mock_wv.return_value = ApplyResult(
                entity_id="sensor.inverter_active_power_control",
                desired=100,
                actual=100,
                status=ApplyStatus.OK,
                attempts=1,
            )
            _summary = await async_apply_inverter_power_control(sensor, cfg, live)

        mock_wv.assert_called_once()

    @pytest.mark.asyncio
    async def test_low_export_price_does_not_block_pv_export(self):
        """Below export_electricity_min_price the applier must not write a watt limit.

        Regression test for issue #767: writing a grid feed-in limit of 0 W
        (or any small watt value) below the price threshold blocks surplus PV
        export as well as battery export.  The fix keeps the connection point
        at unlimited/100% and gates battery export in the planner.
        """
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=False)
        cfg.huawei_solar_device_id_inverter_1 = "device_abc"
        cfg.huawei_solar_inverter_active_power_control = (
            "sensor.inverter_active_power_control"
        )
        cfg.export_electricity_min_price = 0.22

        live = _make_live(degraded_mode=DegradedMode.OK)
        live.export_electricity_price = 0.10
        # Inverter currently at unlimited; no write should happen.
        live.huawei_inverter_active_power_control = "Unlimited"

        mock_state = MagicMock()
        mock_state.state = "Unlimited"
        sensor.hass.states.get.return_value = mock_state

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.utils.huawei.async_set_grid_export_power_watt"
            ) as mock_watt_write,
            patch(
                "custom_components.hsem.utils.huawei.async_set_grid_export_power_pct"
            ) as mock_pct_write,
            patch(
                "custom_components.hsem.custom_sensors.applier_power_control.async_write_and_verify",
                new_callable=AsyncMock,
            ) as mock_wv,
        ):
            _summary = await async_apply_inverter_power_control(sensor, cfg, live)

        mock_watt_write.assert_not_called()
        mock_pct_write.assert_not_called()
        mock_wv.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_export_price_restores_unlimited_from_watt_limit(self):
        """If the inverter is stuck at a watt limit, restore unlimited even below min price."""
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=False)
        cfg.huawei_solar_device_id_inverter_1 = "device_abc"
        cfg.huawei_solar_inverter_active_power_control = (
            "sensor.inverter_active_power_control"
        )
        cfg.export_electricity_min_price = 0.22

        live = _make_live(degraded_mode=DegradedMode.OK)
        live.export_electricity_price = 0.10
        live.huawei_inverter_active_power_control = "Limited to 0W"

        mock_state = MagicMock()
        mock_state.state = "Limited to 0W"
        sensor.hass.states.get.return_value = mock_state

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.utils.huawei.async_set_grid_export_power_watt"
            ) as mock_watt_write,
            patch(
                "custom_components.hsem.custom_sensors.applier_power_control.async_write_and_verify",
                new_callable=AsyncMock,
            ) as mock_wv,
        ):
            from custom_components.hsem.utils.inverter_verify import ApplyResult

            mock_wv.return_value = ApplyResult(
                entity_id="sensor.inverter_active_power_control",
                desired=100,
                actual=100,
                status=ApplyStatus.OK,
                attempts=1,
            )
            _summary = await async_apply_inverter_power_control(sensor, cfg, live)

        mock_watt_write.assert_not_called()
        mock_wv.assert_called_once()

    @pytest.mark.asyncio
    async def test_negative_export_price_blocks_all_export(self):
        """Negative export price must write a watt limit to avoid paying to export.

        This preserves the original physical protection that issue #767 was
        not meant to remove: when exporting costs money, the connection
        point must be blocked.
        """
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=False)
        cfg.huawei_solar_device_id_inverter_1 = "device_abc"
        cfg.huawei_solar_inverter_active_power_control = (
            "sensor.inverter_active_power_control"
        )
        cfg.export_electricity_min_price = 0.22

        live = _make_live(degraded_mode=DegradedMode.OK)
        live.export_electricity_price = -0.05
        # Inverter currently unlimited; must switch to watt limit.
        live.huawei_inverter_active_power_control = "Unlimited"

        mock_state = MagicMock()
        mock_state.state = "Unlimited"
        sensor.hass.states.get.return_value = mock_state

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.utils.huawei.async_set_grid_export_power_pct"
            ) as mock_pct_write,
            patch(
                "custom_components.hsem.custom_sensors.applier_power_control.async_write_and_verify",
                new_callable=AsyncMock,
            ) as mock_wv,
        ):
            from custom_components.hsem.utils.inverter_verify import ApplyResult

            mock_wv.return_value = ApplyResult(
                entity_id="sensor.inverter_active_power_control",
                desired=100,
                actual=100,
                status=ApplyStatus.OK,
                attempts=1,
            )
            _summary = await async_apply_inverter_power_control(sensor, cfg, live)

        mock_pct_write.assert_not_called()
        mock_wv.assert_called_once()

    @pytest.mark.asyncio
    async def test_grid_export_cap_writes_configured_watt_limit(self):
        """Non-negative price with a configured cap must write watts, not 100%."""
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=False)
        cfg.huawei_solar_device_id_inverter_1 = "device_abc"
        cfg.huawei_solar_inverter_active_power_control = (
            "sensor.inverter_active_power_control"
        )
        cfg.max_grid_export_power_kw = 10.0
        cfg.export_electricity_min_price = 0.02

        live = _make_live(degraded_mode=DegradedMode.OK)
        live.export_electricity_price = 0.05
        # Inverter currently unlimited; must switch to the configured cap.
        live.huawei_inverter_active_power_control = "Unlimited"

        mock_state = MagicMock()
        mock_state.state = "Unlimited"
        sensor.hass.states.get.return_value = mock_state

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.utils.huawei.async_set_grid_export_power_pct"
            ) as mock_pct_write,
            patch(
                "custom_components.hsem.custom_sensors.applier_power_control.async_write_and_verify",
                new_callable=AsyncMock,
            ) as mock_wv,
        ):
            from custom_components.hsem.utils.inverter_verify import ApplyResult

            mock_wv.return_value = ApplyResult(
                entity_id="sensor.inverter_active_power_control",
                desired=10000,
                actual=10000,
                status=ApplyStatus.OK,
                attempts=1,
            )
            _summary = await async_apply_inverter_power_control(sensor, cfg, live)

        mock_pct_write.assert_not_called()
        mock_wv.assert_called_once()
        # Desired value passed to write-and-verify must be the watt cap.
        assert mock_wv.call_args.kwargs["desired"] == 10000

    @pytest.mark.asyncio
    async def test_grid_export_cap_skips_when_already_at_limit(self):
        """No write is needed when the inverter already reports the configured cap."""
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=False)
        cfg.huawei_solar_device_id_inverter_1 = "device_abc"
        cfg.huawei_solar_inverter_active_power_control = (
            "sensor.inverter_active_power_control"
        )
        cfg.max_grid_export_power_kw = 10.0

        live = _make_live(degraded_mode=DegradedMode.OK)
        live.export_electricity_price = 0.05
        live.huawei_inverter_active_power_control = "Limited to 10000W"

        mock_state = MagicMock()
        mock_state.state = "Limited to 10000W"
        sensor.hass.states.get.return_value = mock_state

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.utils.huawei.async_set_grid_export_power_watt"
            ) as mock_watt_write,
            patch(
                "custom_components.hsem.utils.huawei.async_set_grid_export_power_pct"
            ) as mock_pct_write,
            patch(
                "custom_components.hsem.custom_sensors.applier_power_control.async_write_and_verify",
                new_callable=AsyncMock,
            ) as mock_wv,
        ):
            _summary = await async_apply_inverter_power_control(sensor, cfg, live)

        mock_watt_write.assert_not_called()
        mock_pct_write.assert_not_called()
        mock_wv.assert_not_called()


# ---------------------------------------------------------------------------
# async_apply_battery_settings — safety gate
# ---------------------------------------------------------------------------


class TestBatterySettingsSafetyGate:
    """Defense-in-depth gate inside async_apply_battery_settings."""

    def _make_rec(self) -> HourlyRecommendation:
        from custom_components.hsem.utils.recommendations import Recommendations

        return _make_rec(Recommendations.BatteriesDischargeMode.value)

    @pytest.mark.asyncio
    async def test_read_only_blocks_battery_writes(self):
        """read_only=True must return an empty summary without any service call."""
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=True)
        live = _make_live(degraded_mode=DegradedMode.OK)
        rec = self._make_rec()

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                new_callable=AsyncMock,
            ) as mock_wv,
        ):
            summary = await async_apply_battery_settings(sensor, cfg, live, rec, 5.0)

        mock_wv.assert_not_called()
        assert len(summary.results) == 0

    @pytest.mark.asyncio
    async def test_error_mode_blocks_battery_writes(self):
        """DegradedMode.Error must block all battery writes."""
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=False)
        live = _make_live(degraded_mode=DegradedMode.Error)
        rec = self._make_rec()

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                new_callable=AsyncMock,
            ) as mock_wv,
        ):
            summary = await async_apply_battery_settings(sensor, cfg, live, rec, 5.0)

        mock_wv.assert_not_called()
        assert len(summary.results) == 0

    @pytest.mark.asyncio
    async def test_degraded_mode_allows_battery_writes(self):
        """DegradedMode.Degraded must NOT block battery writes."""
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=False)
        cfg.huawei_solar_batteries_working_mode = "select.batteries_working_mode"
        cfg.huawei_solar_batteries_maximum_discharging_power = (
            "number.batteries_max_discharge"
        )
        cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou = (
            "select.batteries_excess_pv"
        )

        live = _make_live(degraded_mode=DegradedMode.Degraded)
        live.huawei_batteries_max_discharge_power_w = 3000.0
        live.huawei_batteries_rated_capacity_wh = 10000.0
        live.huawei_batteries_working_mode = "TimeOfUse"
        live.huawei_batteries_excess_pv_use_in_tou = "charge"

        from custom_components.hsem.utils.recommendations import Recommendations

        rec = _make_rec(Recommendations.BatteriesDischargeMode.value)

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                new_callable=AsyncMock,
            ) as mock_wv,
        ):
            from custom_components.hsem.utils.inverter_verify import ApplyResult

            mock_wv.return_value = ApplyResult(
                entity_id="select.batteries_working_mode",
                desired="MaximizeSelfConsumption",
                actual="MaximizeSelfConsumption",
                status=ApplyStatus.OK,
                attempts=1,
            )
            _summary = await async_apply_battery_settings(sensor, cfg, live, rec, 5.0)

        # At least one write was attempted (not blocked).
        mock_wv.assert_called()

    @pytest.mark.asyncio
    async def test_normal_mode_allows_battery_writes(self):
        """OK mode with read_only=False must reach the write path."""
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=False)
        cfg.huawei_solar_batteries_working_mode = "select.batteries_working_mode"
        cfg.huawei_solar_batteries_maximum_discharging_power = (
            "number.batteries_max_discharge"
        )
        cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou = (
            "select.batteries_excess_pv"
        )

        live = _make_live(degraded_mode=DegradedMode.OK)
        live.huawei_batteries_max_discharge_power_w = 3000.0
        live.huawei_batteries_rated_capacity_wh = 10000.0
        live.huawei_batteries_working_mode = "TimeOfUse"
        live.huawei_batteries_excess_pv_use_in_tou = "charge"

        from custom_components.hsem.utils.recommendations import Recommendations

        rec = _make_rec(Recommendations.BatteriesDischargeMode.value)

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                new_callable=AsyncMock,
            ) as mock_wv,
        ):
            from custom_components.hsem.utils.inverter_verify import ApplyResult

            mock_wv.return_value = ApplyResult(
                entity_id="select.batteries_working_mode",
                desired="MaximizeSelfConsumption",
                actual="MaximizeSelfConsumption",
                status=ApplyStatus.OK,
                attempts=1,
            )
            _summary = await async_apply_battery_settings(sensor, cfg, live, rec, 5.0)

        mock_wv.assert_called()

    @pytest.mark.asyncio
    async def test_non_force_mode_stops_forcible_discharge_on_both_batteries(self):
        """Active forcible state should be stopped on both configured battery devices."""
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=False)
        cfg.huawei_solar_device_id_batteries = "bat1"
        cfg.huawei_solar_device_id_batteries_2 = "bat2"
        live = _make_live(degraded_mode=DegradedMode.OK)
        live.huawei_batteries_max_discharge_power_w = 2500
        live.huawei_batteries_forcible_charge_state = "Discharging at 3000W"

        from custom_components.hsem.utils.recommendations import Recommendations

        rec = _make_rec(Recommendations.BatteriesDischargeMode.value)

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_stop_forcible_discharge",
                new_callable=AsyncMock,
            ) as mock_stop,
        ):
            _summary = await async_apply_battery_settings(sensor, cfg, live, rec, 5.0)

        assert mock_stop.await_count == 2
        mock_stop.assert_has_awaits([call(sensor, "bat1"), call(sensor, "bat2")])

    @pytest.mark.asyncio
    async def test_force_discharge_fans_out_to_both_battery_device_ids(self):
        """Force-discharge service call should be sent to both configured batteries."""
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=False)
        cfg.huawei_solar_device_id_batteries = "bat1"
        cfg.huawei_solar_device_id_batteries_2 = "bat2"
        cfg.huawei_solar_batteries_forcible_charge = "sensor.batteries_forcible_charge"
        live = _make_live(degraded_mode=DegradedMode.OK)
        live.battery_usable_capacity_kwh = 10.0
        live.huawei_batteries_end_of_discharge_soc_pct = 10.0
        live.huawei_batteries_max_discharge_power_w = 2500

        from custom_components.hsem.utils.inverter_verify import ApplyResult
        from custom_components.hsem.utils.recommendations import Recommendations

        rec = _make_rec(Recommendations.ForceBatteriesDischarge.value)

        async def _run_writer_then_ok(entity_id, desired, writer, reader, **kwargs):  # type: ignore[no-untyped-def]  # local test shim mirrors async_write_and_verify signature
            await writer()
            return ApplyResult(
                entity_id=entity_id,
                desired=desired,
                actual=desired,
                status=ApplyStatus.OK,
                attempts=1,
            )

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier_forcible_discharge.async_set_forcible_discharge",
                new_callable=AsyncMock,
            ) as mock_forcible,
            patch(
                "custom_components.hsem.custom_sensors.applier_forcible_discharge.async_write_and_verify",
                side_effect=_run_writer_then_ok,
            ),
        ):
            _summary = await async_apply_battery_settings(sensor, cfg, live, rec, 2.0)

        assert mock_forcible.await_count == 2
        assert mock_forcible.await_args_list[0].args[1] == "bat1"
        assert mock_forcible.await_args_list[1].args[1] == "bat2"

    @pytest.mark.asyncio
    async def test_tou_period_write_fans_out_to_both_battery_device_ids(self):
        """TOU period writes should be applied to both configured battery devices."""
        sensor = _make_sensor()
        cfg = _make_cfg(read_only=False)
        cfg.huawei_solar_device_id_batteries = "bat1"
        cfg.huawei_solar_device_id_batteries_2 = "bat2"
        cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = "sensor.tou"
        live = _make_live(degraded_mode=DegradedMode.OK)
        live.huawei_batteries_max_discharge_power_w = 2500
        live.tou_periods.periods = []
        # Excess PV use already matches desired ("charge") so that check is a no-op
        # and execution reaches the TOU period fan-out block under test.
        live.huawei_batteries_excess_pv_use_in_tou = "charge"

        from custom_components.hsem.utils.inverter_verify import ApplyResult
        from custom_components.hsem.utils.recommendations import Recommendations

        rec = _make_rec(Recommendations.BatteriesChargeGrid.value)

        async def _run_writer_then_ok(entity_id, desired, writer, reader, **kwargs):  # type: ignore[no-untyped-def]  # local test shim mirrors async_write_and_verify signature
            await writer()
            return ApplyResult(
                entity_id=entity_id,
                desired=desired,
                actual=desired,
                status=ApplyStatus.OK,
                attempts=1,
            )

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_tou_periods",
                new_callable=AsyncMock,
            ) as mock_set_tou,
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                side_effect=_run_writer_then_ok,
            ),
        ):
            _summary = await async_apply_battery_settings(sensor, cfg, live, rec, 2.0)

        assert mock_set_tou.await_count == 2
        assert mock_set_tou.await_args_list[0].args[1] == "bat1"
        assert mock_set_tou.await_args_list[1].args[1] == "bat2"


# ---------------------------------------------------------------------------
# Working-mode sensor top-level gate (_async_apply_hardware_writes)
# ---------------------------------------------------------------------------


class TestWorkingModeSensorTopLevelGate:
    """Prove the outer gate in HSEMWorkingModeSensor._async_apply_hardware_writes.

    We import the gate function directly via the applier module to verify
    the plumbing without a full HA setup.
    """

    def _make_coordinator_data(
        self,
        *,
        read_only: bool = False,
        degraded_mode: DegradedMode = DegradedMode.OK,
    ) -> MagicMock:
        """Build a minimal CoordinatorData-like object for gate testing."""
        cfg = _make_cfg(read_only=read_only)
        live = _make_live(degraded_mode=degraded_mode)
        live.energi_data_service_export_price = 1.0  # type: ignore[attr-defined]  # mock attribute set in test

        data = MagicMock()
        data.cfg = cfg
        data.live = live
        data.hourly_recommendation = None
        data.batteries_schedules_remaining_capacity_needed = 0.0
        data.current_required_battery = 0.0
        data.apply_summary = None
        return data

    @pytest.mark.asyncio
    async def test_read_only_skips_both_appliers(self):
        """When read_only=True the applier functions must not be called at all."""
        data = self._make_coordinator_data(read_only=True)

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.working_mode_sensor.async_apply_inverter_power_control",
                new_callable=AsyncMock,
            ) as mock_inv,
            patch(
                "custom_components.hsem.custom_sensors.working_mode_sensor.async_apply_battery_settings",
                new_callable=AsyncMock,
            ) as mock_bat,
        ):
            # Import here to avoid circular import issues in test collection.
            from custom_components.hsem.custom_sensors.working_mode_sensor import (
                HSEMWorkingModeSensor,
            )

            sensor = MagicMock(spec=HSEMWorkingModeSensor)
            sensor.hass = MagicMock()

            await HSEMWorkingModeSensor._async_apply_hardware_writes(sensor, data)

        mock_inv.assert_not_called()
        mock_bat.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_mode_skips_both_appliers(self):
        """DegradedMode.Error must prevent both applier calls."""
        data = self._make_coordinator_data(
            read_only=False, degraded_mode=DegradedMode.Error
        )

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.working_mode_sensor.async_apply_inverter_power_control",
                new_callable=AsyncMock,
            ) as mock_inv,
            patch(
                "custom_components.hsem.custom_sensors.working_mode_sensor.async_apply_battery_settings",
                new_callable=AsyncMock,
            ) as mock_bat,
        ):
            from custom_components.hsem.custom_sensors.working_mode_sensor import (
                HSEMWorkingModeSensor,
            )

            sensor = MagicMock(spec=HSEMWorkingModeSensor)
            sensor.hass = MagicMock()

            await HSEMWorkingModeSensor._async_apply_hardware_writes(sensor, data)

        mock_inv.assert_not_called()
        mock_bat.assert_not_called()

    @pytest.mark.asyncio
    async def test_degraded_mode_calls_inverter_applier(self):
        """DegradedMode.Degraded must still call the inverter applier."""
        data = self._make_coordinator_data(
            read_only=False, degraded_mode=DegradedMode.Degraded
        )

        from custom_components.hsem.utils.inverter_verify import CycleApplySummary

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.working_mode_sensor.async_apply_inverter_power_control",
                new_callable=AsyncMock,
                return_value=CycleApplySummary(),
            ) as mock_inv,
            patch(
                "custom_components.hsem.custom_sensors.working_mode_sensor.async_apply_battery_settings",
                new_callable=AsyncMock,
                return_value=CycleApplySummary(),
            ) as mock_bat,
        ):
            from custom_components.hsem.custom_sensors.working_mode_sensor import (
                HSEMWorkingModeSensor,
            )

            sensor = MagicMock(spec=HSEMWorkingModeSensor)
            sensor.hass = MagicMock()

            await HSEMWorkingModeSensor._async_apply_hardware_writes(sensor, data)

        mock_inv.assert_called_once()
        # Battery applier not called because hourly_rec is None.
        mock_bat.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_mode_calls_both_appliers(self):
        """OK mode with read_only=False must call both appliers when a rec exists."""
        data = self._make_coordinator_data(
            read_only=False, degraded_mode=DegradedMode.OK
        )
        # Provide a dummy hourly_recommendation so the battery applier gets called.
        data.hourly_recommendation = MagicMock()

        from custom_components.hsem.utils.inverter_verify import CycleApplySummary

        inv_summary = CycleApplySummary()
        # overall_status of an empty CycleApplySummary is SKIPPED, not FAILED,
        # so the battery gate passes.

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.working_mode_sensor.async_apply_inverter_power_control",
                new_callable=AsyncMock,
                return_value=inv_summary,
            ) as mock_inv,
            patch(
                "custom_components.hsem.custom_sensors.working_mode_sensor.async_apply_battery_settings",
                new_callable=AsyncMock,
                return_value=CycleApplySummary(),
            ) as mock_bat,
            patch(
                "custom_components.hsem.custom_sensors.working_mode_sensor.resolve_current_recommendation"
            ),
        ):
            from custom_components.hsem.custom_sensors.working_mode_sensor import (
                HSEMWorkingModeSensor,
            )

            sensor = MagicMock(spec=HSEMWorkingModeSensor)
            sensor.hass = MagicMock()

            await HSEMWorkingModeSensor._async_apply_hardware_writes(sensor, data)

        mock_inv.assert_called_once()
        mock_bat.assert_called_once()


# ---------------------------------------------------------------------------
# hardware_writes_allowed — unit tests (full coverage)
# ---------------------------------------------------------------------------


class TestHardwareWritesAllowedDirectly:
    """Direct unit tests for :func:`hardware_writes_allowed` covering every mode."""

    def test_ok_mode_allows(self):
        from custom_components.hsem.utils.degraded_mode import hardware_writes_allowed

        assert hardware_writes_allowed(DegradedMode.OK) is True

    def test_degraded_mode_allows(self):
        from custom_components.hsem.utils.degraded_mode import hardware_writes_allowed

        assert hardware_writes_allowed(DegradedMode.Degraded) is True

    def test_error_mode_blocks(self):
        from custom_components.hsem.utils.degraded_mode import hardware_writes_allowed

        assert hardware_writes_allowed(DegradedMode.Error) is False


# ---------------------------------------------------------------------------
# EV discharge permission + held-export authority (issue #797)
# ---------------------------------------------------------------------------


def _make_rec_with_energy(
    recommendation: str,
    *,
    batteries_charged_kwh: float = 0.0,
    batteries_discharged_kwh: float = 0.0,
    grid_export_kwh: float = 0.0,
    ev_charger_calculated_power: float = 0.0,
    ev_second_charger_calculated_power: float = 0.0,
    ev_total_planned_load_kwh: float = 0.0,
) -> HourlyRecommendation:
    """Return a minimal recommendation with the fields this module reads."""
    from datetime import datetime, timedelta

    rec = _make_rec(recommendation)
    object.__setattr__(rec, "batteries_charged_kwh", batteries_charged_kwh)
    object.__setattr__(rec, "batteries_discharged_kwh", batteries_discharged_kwh)
    object.__setattr__(rec, "grid_export_kwh", grid_export_kwh)
    object.__setattr__(rec, "ev_charger_calculated_power", ev_charger_calculated_power)
    object.__setattr__(
        rec,
        "ev_second_charger_calculated_power",
        ev_second_charger_calculated_power,
    )
    object.__setattr__(rec, "ev_total_planned_load_kwh", ev_total_planned_load_kwh)
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    object.__setattr__(rec, "start", start)
    object.__setattr__(rec, "end", start + timedelta(hours=1))
    return rec


class TestEvDischargePermissionAndHeldExport:
    """EVConfig.force_max_discharge_power is permission, never a command."""

    @staticmethod
    def _cfg_and_live() -> tuple[SensorConfig, LiveState]:
        cfg = _make_cfg(read_only=False)
        cfg.huawei_solar_batteries_maximum_discharging_power = "number.max_discharge"
        cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou = "select.excess_pv"
        cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = (
            "sensor.tou_periods"
        )
        cfg.huawei_solar_device_id_batteries = "bat1"
        live = _make_live(degraded_mode=DegradedMode.OK)
        live.huawei_batteries_max_discharge_power_w = 5000
        live.huawei_batteries_excess_pv_use_in_tou = "charge"
        return cfg, live

    @staticmethod
    async def _run(cfg, live, rec):
        written: dict[str, object] = {}

        async def _record_desired(entity_id, desired, writer, reader, **kwargs):  # type: ignore[no-untyped-def]
            written[entity_id] = desired
            return ApplyResult(
                entity_id=entity_id,
                desired=desired,
                actual=desired,
                status=ApplyStatus.OK,
                attempts=1,
            )

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                side_effect=_record_desired,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_tou_periods",
                new_callable=AsyncMock,
            ),
        ):
            await async_apply_battery_settings(_make_sensor(), cfg, live, rec, 0.0)
        return written

    @pytest.mark.asyncio
    async def test_discharge_blocked_when_relevant_ev_lacks_permission(self):
        """A charging EV without opt-in forces the discharge cap to 0 W."""
        from custom_components.hsem.utils.recommendations import Recommendations

        cfg, live = self._cfg_and_live()
        live.ev.is_charging = True
        live.ev.force_max_discharge_power = False
        rec = _make_rec_with_energy(
            Recommendations.EVSmartCharging.value,
            batteries_discharged_kwh=1.0,
            ev_charger_calculated_power=3000.0,
        )

        written = await self._run(cfg, live, rec)

        assert written["number.max_discharge"] == 0

    @pytest.mark.asyncio
    async def test_discharge_capped_to_planned_rate_when_ev_has_permission(self):
        """With permission, the cap is the planner's own solved rate, not full max."""
        from custom_components.hsem.utils.recommendations import Recommendations

        cfg, live = self._cfg_and_live()
        live.ev.is_charging = True
        live.ev.force_max_discharge_power = True
        live.ev.max_discharge_power_w = 4000
        rec = _make_rec_with_energy(
            Recommendations.EVSmartCharging.value,
            batteries_discharged_kwh=1.0,  # 1 kWh over a 1 h slot = 1000 W
            ev_charger_calculated_power=3000.0,
        )

        written = await self._run(cfg, live, rec)

        assert written["number.max_discharge"] == 1000

    @pytest.mark.asyncio
    async def test_ev_smart_charging_uses_msc_without_held_export(self):
        """No solved export on a non-held slot: EVSmartCharging stays in MSC."""
        from custom_components.hsem.utils.recommendations import Recommendations

        cfg, live = self._cfg_and_live()
        live.ev.is_charging = True
        live.ev.force_max_discharge_power = True
        live.ev.max_discharge_power_w = 4000
        rec = _make_rec_with_energy(
            Recommendations.EVSmartCharging.value,
            batteries_charged_kwh=1.0,
            ev_charger_calculated_power=3000.0,
        )

        written = await self._run(cfg, live, rec)

        assert "sensor.tou_periods" not in written

    @pytest.mark.asyncio
    async def test_ev_smart_charging_keeps_tou_wait_with_held_export(self):
        """A held slot with a material solved export stays in TOU wait, sold."""
        from custom_components.hsem.utils.recommendations import Recommendations

        cfg, live = self._cfg_and_live()
        live.ev.is_charging = True
        live.ev.force_max_discharge_power = True
        live.ev.max_discharge_power_w = 4000
        rec = _make_rec_with_energy(
            Recommendations.EVSmartCharging.value,
            batteries_charged_kwh=0.0,
            batteries_discharged_kwh=0.0,
            grid_export_kwh=1.5,
            ev_charger_calculated_power=3000.0,
        )

        written = await self._run(cfg, live, rec)

        assert written["sensor.tou_periods:bat1"] == DEFAULT_HSEM_BATTERIES_WAIT_MODE
        assert written["select.excess_pv"] == "fed_to_grid"

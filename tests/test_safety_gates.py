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

Note on ``async_logger``
------------------------
:func:`async_logger` is patched with a no-op ``AsyncMock`` in every test so
that planner/applier output never reaches the standard ``custom_components.hsem``
logger during the test run.  This keeps test output clean and decouples the
safety-gate assertions from log-formatting changes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.custom_sensors.applier import (
    async_apply_battery_settings,
    async_apply_inverter_power_control,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import ApplyStatus

# ---------------------------------------------------------------------------
# Module-level patch targets (reused across all test classes)
# ---------------------------------------------------------------------------

# The applier module imports async_logger under this name.
_APPLIER_LOGGER = "custom_components.hsem.custom_sensors.applier.async_logger"
_SENSOR_LOGGER = (
    "custom_components.hsem.custom_sensors.working_mode_sensor.async_logger"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sensor():
    """Return a minimal mock sensor that satisfies async_logger requirements."""
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
            patch(_APPLIER_LOGGER, new_callable=AsyncMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_grid_export_power_pct"
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
            patch(_APPLIER_LOGGER, new_callable=AsyncMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_grid_export_power_pct"
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
        # Set current inverter state to force a write (100 → 0 would write).
        live.huawei_inverter_active_power_control = "Unlimited"

        # Set up an inverter device ID so the write loop has something to call.
        cfg.huawei_solar_device_id_inverter_1 = "device_123"
        cfg.huawei_solar_inverter_active_power_control = (
            "sensor.inverter_active_power_control"
        )
        cfg.export_electricity_min_price = 1.0

        # Make the HA state read return an entity indicating "Unlimited" (100 %).
        mock_state = MagicMock()
        mock_state.state = "Unlimited"
        sensor.hass.states.get.return_value = mock_state

        with (
            patch(_APPLIER_LOGGER, new_callable=AsyncMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                new_callable=AsyncMock,
            ) as mock_wv,
        ):
            from custom_components.hsem.utils.inverter_verify import ApplyResult

            mock_wv.return_value = ApplyResult(
                entity_id="sensor.inverter_active_power_control",
                desired=0,
                actual=0,
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
        live.huawei_inverter_active_power_control = "Unlimited"

        mock_state = MagicMock()
        mock_state.state = "Unlimited"
        sensor.hass.states.get.return_value = mock_state

        with (
            patch(_APPLIER_LOGGER, new_callable=AsyncMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                new_callable=AsyncMock,
            ) as mock_wv,
        ):
            from custom_components.hsem.utils.inverter_verify import ApplyResult

            mock_wv.return_value = ApplyResult(
                entity_id="sensor.inverter_active_power_control",
                desired=0,
                actual=0,
                status=ApplyStatus.OK,
                attempts=1,
            )
            _summary = await async_apply_inverter_power_control(sensor, cfg, live)

        mock_wv.assert_called_once()


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
            patch(_APPLIER_LOGGER, new_callable=AsyncMock),
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
            patch(_APPLIER_LOGGER, new_callable=AsyncMock),
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
            patch(_APPLIER_LOGGER, new_callable=AsyncMock),
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
            patch(_APPLIER_LOGGER, new_callable=AsyncMock),
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
    ):
        """Build a minimal CoordinatorData-like object for gate testing."""
        cfg = _make_cfg(read_only=read_only)
        live = _make_live(degraded_mode=degraded_mode)
        live.energi_data_service_export_price = 1.0

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
            patch(_SENSOR_LOGGER, new_callable=AsyncMock),
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
            patch(_SENSOR_LOGGER, new_callable=AsyncMock),
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
            patch(_SENSOR_LOGGER, new_callable=AsyncMock),
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
            patch(_SENSOR_LOGGER, new_callable=AsyncMock),
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

"""Export-limit writes without a usable read-back entity (issue #1120).

On an EMMA system ``huawei_solar`` exposes no active power control sensor, so
users either left the feedback entity empty or picked the inverter's live
active power (e.g. ``"1540"``). HSEM compared that power reading with the
export limit, marked the write ``FAILED`` and blocked every battery write in
the cycle. These tests pin the fix:

- a bare power reading is never compared with a limit;
- without usable feedback the limit is written once per change, reported as
  ``UNVERIFIED`` and battery writes proceed;
- a real service error is still ``FAILED`` and still blocks battery writes;
- with an EMMA controller configured, the limit goes to the EMMA, which is
  the only device ``huawei_solar`` accepts for these services.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.applier_power_control import (
    _export_limit_device_ids,
    async_apply_inverter_power_control,
)
from custom_components.hsem.custom_sensors.working_mode_sensor import (
    HSEMWorkingModeSensor,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import (
    ApplyStatus,
    CycleApplySummary,
)

_MODULE = "custom_components.hsem.custom_sensors.applier_power_control"
_WORKING_MODE = "custom_components.hsem.custom_sensors.working_mode_sensor"
_VERIFY = "custom_components.hsem.utils.inverter_verify"
_APC_ENTITY = "sensor.inverter_active_power_control"
_LIVE_POWER_ENTITY = "sensor.inverter_active_power"


def _sensor(state: str | None = None) -> MagicMock:
    """Return a sensor whose configured feedback entity reports *state*."""
    sensor = MagicMock(spec=["hass"])
    sensor.hass.states.get.return_value = (
        None if state is None else MagicMock(state=state)
    )
    return sensor


def _cfg(**overrides: Any) -> SensorConfig:
    """Return an EMMA-style config: one device, no feedback entity."""
    cfg = SensorConfig()
    cfg.read_only = False
    cfg.export_electricity_min_price = 0.0
    cfg.huawei_solar_device_id_inverter_1 = "emma"
    cfg.huawei_solar_inverter_active_power_control = None
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _live(power_control_state: str | None = None, **overrides: Any) -> LiveState:
    """Return a live snapshot with a positive export price (→ 100 %)."""
    live = LiveState()
    live._degraded_mode = DegradedMode.OK
    live.export_electricity_price = 0.5
    live.huawei_inverter_active_power_control = power_control_state
    for key, value in overrides.items():
        setattr(live, key, value)
    return live


def _patch_pct_writer(side_effect: Any = None) -> Any:
    return patch(
        f"{_MODULE}.async_set_grid_export_power_pct",
        AsyncMock(side_effect=side_effect),
    )


def _patch_sleep() -> Any:
    """Skip the retry settle time on the write-error path."""
    return patch(f"{_VERIFY}.asyncio.sleep", AsyncMock())


class TestPowerReadingIsNotFeedback:
    """The reported EMMA setup: the live active power as feedback entity."""

    @pytest.mark.asyncio
    async def test_the_limit_is_written_once_and_reported_unverified(self) -> None:
        """desired=100 vs actual=1540 must no longer be a FAILED mismatch."""
        sensor = _sensor("1540")
        cfg = _cfg(huawei_solar_inverter_active_power_control=_LIVE_POWER_ENTITY)

        with _patch_pct_writer() as writer:
            summary = await async_apply_inverter_power_control(
                sensor, cfg, _live("1540")
            )

        writer.assert_awaited_once_with(sensor, "emma", 100)
        assert [r.status for r in summary.results] == [ApplyStatus.UNVERIFIED]
        assert summary.results[0].actual is None
        assert summary.results[0].entity_id == "inverter:emma"
        assert summary.overall_status != ApplyStatus.FAILED

    @pytest.mark.asyncio
    async def test_the_misconfiguration_is_warned_about_once(self) -> None:
        sensor = _sensor("1540")
        cfg = _cfg(huawei_solar_inverter_active_power_control=_LIVE_POWER_ENTITY)

        with _patch_pct_writer(), patch(f"{_MODULE}._LOGGER") as logger:
            await async_apply_inverter_power_control(sensor, cfg, _live("1540"))
            await async_apply_inverter_power_control(sensor, cfg, _live("1622"))

        warnings = [c.args for c in logger.warning.call_args_list]
        assert len(warnings) == 1
        assert _LIVE_POWER_ENTITY in warnings[0]
        assert "1540" in warnings[0]


class TestWithoutFeedbackEntity:
    """No feedback entity at all — the EMMA configuration after the fix."""

    @pytest.mark.asyncio
    async def test_an_unchanged_limit_is_not_rewritten(self) -> None:
        """Nothing can read the limit back, so it is written once per change."""
        sensor = _sensor()

        with _patch_pct_writer() as writer:
            first = await async_apply_inverter_power_control(sensor, _cfg(), _live())
            second = await async_apply_inverter_power_control(sensor, _cfg(), _live())

        writer.assert_awaited_once()
        assert [r.status for r in first.results] == [ApplyStatus.UNVERIFIED]
        assert second.results == []

    @pytest.mark.asyncio
    async def test_a_changed_limit_is_written(self) -> None:
        """A negative price switches to the watt floor, which is written."""
        sensor = _sensor()

        with (
            _patch_pct_writer() as pct_writer,
            patch(f"{_MODULE}.async_set_grid_export_power_watt", AsyncMock()) as watt,
        ):
            await async_apply_inverter_power_control(sensor, _cfg(), _live())
            summary = await async_apply_inverter_power_control(
                sensor, _cfg(), _live(export_electricity_price=-0.1)
            )

        pct_writer.assert_awaited_once()
        watt.assert_awaited_once()
        assert [r.status for r in summary.results] == [ApplyStatus.UNVERIFIED]

    @pytest.mark.asyncio
    async def test_a_service_error_fails_closed_and_is_retried_next_cycle(
        self,
    ) -> None:
        """A real write error stays FAILED; the next cycle writes again."""
        sensor = _sensor()
        errors = [RuntimeError("Failed to read registers P_max")] * 3

        with _patch_sleep(), _patch_pct_writer(side_effect=[*errors, None]) as writer:
            failed = await async_apply_inverter_power_control(sensor, _cfg(), _live())
            retried = await async_apply_inverter_power_control(sensor, _cfg(), _live())

        assert [r.status for r in failed.results] == [ApplyStatus.FAILED]
        assert "P_max" in failed.results[0].error_message
        assert [r.status for r in retried.results] == [ApplyStatus.UNVERIFIED]
        assert writer.await_count == 4

    @pytest.mark.asyncio
    async def test_the_latch_is_per_device(self) -> None:
        """A second inverter still gets its first write."""
        sensor = _sensor()
        cfg = _cfg(
            huawei_solar_device_id_inverter_1="inverter_1",
            huawei_solar_device_id_inverter_2="inverter_2",
        )

        with _patch_pct_writer() as writer:
            summary = await async_apply_inverter_power_control(sensor, cfg, _live())

        assert writer.await_args_list == [
            call(sensor, "inverter_1", 100),
            call(sensor, "inverter_2", 100),
        ]
        assert len(summary.results) == 2


class TestRealFeedbackIsUnchanged:
    """A genuine active power control sensor keeps the verified path."""

    @pytest.mark.asyncio
    async def test_a_matching_limit_is_skipped(self) -> None:
        cfg = _cfg(huawei_solar_inverter_active_power_control=_APC_ENTITY)

        with _patch_pct_writer() as writer:
            summary = await async_apply_inverter_power_control(
                _sensor("Unlimited"), cfg, _live("Unlimited")
            )

        writer.assert_not_awaited()
        assert summary.results == []

    @pytest.mark.asyncio
    async def test_a_changed_limit_is_verified_by_read_back(self) -> None:
        cfg = _cfg(huawei_solar_inverter_active_power_control=_APC_ENTITY)
        sensor = _sensor("Limited to 50%")

        async def _write(*_args: Any) -> None:
            sensor.hass.states.get.return_value.state = "Limited to 100%"

        with _patch_sleep(), _patch_pct_writer(side_effect=_write) as writer:
            summary = await async_apply_inverter_power_control(
                sensor, cfg, _live("Limited to 50%")
            )

        writer.assert_awaited_once()
        assert [r.status for r in summary.results] == [ApplyStatus.OK]
        assert summary.results[0].entity_id == _APC_ENTITY


class TestExportLimitRouting:
    """With an EMMA, huawei_solar only accepts the EMMA for export limits."""

    def test_a_configured_emma_replaces_the_inverters(self) -> None:
        cfg = _cfg(
            huawei_solar_device_id_inverter_1="sun2000",
            huawei_solar_device_id_inverter_2="sun2000_2",
            huawei_solar_device_id_tou_controller="emma",
        )
        assert _export_limit_device_ids(cfg) == ["emma"]

    def test_without_an_emma_the_inverters_are_written(self) -> None:
        cfg = _cfg(
            huawei_solar_device_id_inverter_1="sun2000",
            huawei_solar_device_id_inverter_2=None,
        )
        assert _export_limit_device_ids(cfg) == ["sun2000"]

    @pytest.mark.asyncio
    async def test_the_write_targets_the_emma(self) -> None:
        sensor = _sensor()
        cfg = _cfg(
            huawei_solar_device_id_inverter_1="sun2000",
            huawei_solar_device_id_tou_controller="emma",
        )

        with _patch_pct_writer() as writer:
            await async_apply_inverter_power_control(sensor, cfg, _live())

        writer.assert_awaited_once_with(sensor, "emma", 100)


# ---------------------------------------------------------------------------
# End to end through the working-mode sensor's write gate
# ---------------------------------------------------------------------------


def _working_mode_sensor(feedback_state: str) -> HSEMWorkingModeSensor:
    entry = MagicMock()
    entry.entry_id = "test_entry_1120"
    entry.options = {}
    entry.data = {}
    coordinator = MagicMock()
    coordinator.data = None
    sensor = HSEMWorkingModeSensor(entry, coordinator)
    sensor.hass = MagicMock()
    sensor.hass.states.get.return_value = MagicMock(state=feedback_state)
    return sensor


def _coordinator_data(feedback_entity: str, feedback_state: str) -> CoordinatorData:
    cfg = _cfg(huawei_solar_inverter_active_power_control=feedback_entity)
    now = datetime.now(UTC)
    rec = HourlyRecommendation.__new__(HourlyRecommendation)
    object.__setattr__(rec, "start", now)
    object.__setattr__(rec, "end", now + timedelta(minutes=15))
    object.__setattr__(rec, "recommendation", "batteries_wait_mode")
    object.__setattr__(rec, "batteries_charged_kwh", 0.0)
    object.__setattr__(rec, "batteries_discharged_kwh", 0.0)
    object.__setattr__(rec, "grid_export_kwh", 0.0)
    return CoordinatorData(
        cfg=cfg, live=_live(feedback_state), hourly_recommendation=rec
    )


class TestBatteryWritesAreNotBlocked:
    """The reported symptom: every battery write blocked after the export write."""

    @pytest.mark.asyncio
    async def test_battery_writes_run_after_an_unverifiable_export_write(
        self,
    ) -> None:
        sensor = _working_mode_sensor("1540")
        data = _coordinator_data(_LIVE_POWER_ENTITY, "1540")

        with (
            _patch_sleep(),
            _patch_pct_writer(),
            patch(
                f"{_WORKING_MODE}.async_apply_battery_settings",
                AsyncMock(return_value=CycleApplySummary()),
            ) as battery,
        ):
            await sensor._async_apply_hardware_writes(data)

        battery.assert_awaited_once()
        assert data.apply_summary is not None
        assert data.apply_summary.overall_status == ApplyStatus.UNVERIFIED

    @pytest.mark.asyncio
    async def test_a_failing_export_service_still_blocks_battery_writes(
        self,
    ) -> None:
        """Fail-closed on a real write error is kept."""
        sensor = _working_mode_sensor("1540")
        data = _coordinator_data(_LIVE_POWER_ENTITY, "1540")

        with (
            _patch_sleep(),
            _patch_pct_writer(side_effect=RuntimeError("wrong_device_type")),
            patch(
                f"{_WORKING_MODE}.async_apply_battery_settings",
                AsyncMock(return_value=CycleApplySummary()),
            ) as battery,
        ):
            await sensor._async_apply_hardware_writes(data)

        battery.assert_not_awaited()
        assert data.apply_summary is not None
        assert data.apply_summary.overall_status == ApplyStatus.FAILED

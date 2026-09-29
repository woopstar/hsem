"""Applier log records reach ``hsem.log`` at their intended level (issue #1114).

The applier used to call ``_LOGGER.debug("msg", "warning")``: the record was
always DEBUG, so write failures vanished with verbose logging off, and the
stray argument made formatting raise ``TypeError`` with verbose logging on.
These tests drive the real ``HSEM_LOGGER`` through a capturing handler and
format every record, so both failure modes are caught.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.custom_sensors.applier import async_apply_battery_settings
from custom_components.hsem.custom_sensors.phase_charge_limiter import (
    PhaseAwareChargeCommands,
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
from custom_components.hsem.utils.logger import (
    HSEM_LOGGER,
    log_latched_warning,
    set_hsem_verbose,
)
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.workingmodes import WorkingModes
from tests.test_working_mode_task_lifecycle import (
    _make_minimal_coordinator_data,
    _make_sensor,
)

_MODULE = "custom_components.hsem.custom_sensors.applier"
_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

_DISCHARGE_ENTITY = "number.maxdis"
_GRID_CHARGE_ENTITY = "number.gridcharge"
_EXCESS_ENTITY = "select.excess"
_TOU_ENTITY = "sensor.tou"
_MODE_ENTITY = "select.wm"


class _Capture(logging.Handler):
    """Collect records; formatting them proves no stray ``%`` argument."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, level: int) -> list[str]:
        """Return formatted messages at exactly *level* (raises on the old bug)."""
        return [r.getMessage() for r in self.records if r.levelno == level]

    def all_messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


@pytest.fixture
def capture() -> Iterator[_Capture]:
    """Attach a capturing handler to ``HSEM_LOGGER`` and restore its level."""
    handler = _Capture()
    previous_level = HSEM_LOGGER.level
    HSEM_LOGGER.addHandler(handler)
    try:
        yield handler
    finally:
        HSEM_LOGGER.removeHandler(handler)
        HSEM_LOGGER.setLevel(previous_level)


def _sensor() -> MagicMock:
    sensor = MagicMock()
    sensor.hass = MagicMock()
    sensor._ev_zero_discharge_ceiling_warned = set()
    return sensor


def _cfg() -> SensorConfig:
    """Return a config with every write target configured."""
    cfg = SensorConfig()
    cfg.read_only = False
    cfg.huawei_solar_batteries_maximum_discharging_power = _DISCHARGE_ENTITY
    cfg.huawei_solar_batteries_grid_charge_maximum_power = _GRID_CHARGE_ENTITY
    cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou = _EXCESS_ENTITY
    cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = _TOU_ENTITY
    cfg.huawei_solar_batteries_working_mode = _MODE_ENTITY
    cfg.huawei_solar_device_id_batteries = "bat1"
    return cfg


def _live() -> LiveState:
    """Return a live snapshot that differs from every desired value."""
    live = LiveState()
    live._degraded_mode = DegradedMode.OK
    live.battery_current_capacity_kwh = 5.0
    live.huawei_batteries_rated_capacity_wh = 5000
    live.huawei_batteries_max_discharge_power_w = 0
    live.huawei_batteries_grid_charge_max_power_w = 0
    live.huawei_batteries_working_mode = WorkingModes.TimeOfUse.value
    live.huawei_batteries_excess_pv_use_in_tou = "fed_to_grid"
    return live


def _rec(recommendation: str, *, charged_kwh: float = 0.0) -> HourlyRecommendation:
    zero = 0.0
    return HourlyRecommendation(
        start=_NOW,
        end=_NOW + timedelta(minutes=60),
        recommendation=recommendation,
        avg_house_consumption_kwh=0.5,
        avg_house_consumption_1d_kwh=zero,
        avg_house_consumption_3d_kwh=zero,
        avg_house_consumption_7d_kwh=zero,
        avg_house_consumption_14d_kwh=zero,
        batteries_charged_kwh=charged_kwh,
        batteries_discharged_kwh=1.5,
        estimated_battery_capacity_kwh=1.0,
        estimated_battery_soc_pct=50.0,
        estimated_cost_currency=zero,
        estimated_net_consumption_kwh=0.5,
        export_price=0.05,
        grid_export_kwh=zero,
        grid_import_kwh=zero,
        import_price=0.20,
        solcast_pv_estimate_kwh=zero,
    )


def _verifier(fail_on: str | None) -> Any:
    async def _write_and_verify(
        entity_id: str, desired: Any, writer: Any, reader: Any, **_kwargs: Any
    ) -> ApplyResult:
        await writer()
        failed = fail_on is not None and entity_id.startswith(fail_on)
        return ApplyResult(
            entity_id=entity_id,
            desired=desired,
            actual=None if failed else desired,
            status=ApplyStatus.FAILED if failed else ApplyStatus.OK,
            attempts=1,
        )

    return _write_and_verify


async def _apply(
    cfg: SensorConfig,
    live: LiveState,
    rec: HourlyRecommendation,
    *,
    fail_on: str | None = None,
    grid_charge_w: float | None = None,
    sensor: MagicMock | None = None,
) -> CycleApplySummary:
    """Run the applier with stubbed HA writes and phase limiter."""
    commands = PhaseAwareChargeCommands(
        recommendation=rec, primary_grid_charge_power_w=grid_charge_w
    )
    with (
        patch(f"{_MODULE}.async_write_and_verify", side_effect=_verifier(fail_on)),
        patch(f"{_MODULE}.async_set_select_option", AsyncMock()),
        patch(f"{_MODULE}.async_set_number_value", AsyncMock()),
        patch(f"{_MODULE}.async_set_tou_periods", AsyncMock()),
        patch(f"{_MODULE}.async_stop_forcible_discharge", AsyncMock()),
        patch(f"{_MODULE}.build_phase_aware_charge_commands", return_value=commands),
    ):
        return await async_apply_battery_settings(
            sensor or _sensor(), cfg, live, rec, 0.0, wait_mode_reserve_kwh=None
        )


_SUFFIX = "; blocking further battery writes this cycle"


class TestFailedWriteLogsError:
    """A verified write that fails is an ERROR, whatever the verbosity."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("recommendation", "fail_on", "grid_charge_w", "expected"),
        [
            pytest.param(
                Recommendations.BatteriesDischargeMode.value,
                _MODE_ENTITY,
                None,
                f"Working mode write FAILED for {_MODE_ENTITY}",
                id="working_mode",
            ),
            pytest.param(
                Recommendations.BatteriesDischargeMode.value,
                _DISCHARGE_ENTITY,
                None,
                f"Max discharge power write FAILED for {_DISCHARGE_ENTITY}",
                id="discharge_cap",
            ),
            pytest.param(
                Recommendations.BatteriesChargeGrid.value,
                _GRID_CHARGE_ENTITY,
                3000.0,
                f"Grid-charge maximum-power write FAILED for {_GRID_CHARGE_ENTITY}",
                id="grid_charge",
            ),
            pytest.param(
                Recommendations.BatteriesDischargeMode.value,
                _EXCESS_ENTITY,
                None,
                f"Excess PV use write FAILED for {_EXCESS_ENTITY}",
                id="excess_pv",
            ),
            pytest.param(
                Recommendations.BatteriesChargeGrid.value,
                f"{_TOU_ENTITY}:",
                None,
                "TOU period write FAILED for device bat1",
                id="tou",
            ),
        ],
    )
    async def test_failed_write_is_error_with_verbose_off(
        self,
        capture: _Capture,
        recommendation: str,
        fail_on: str,
        grid_charge_w: float | None,
        expected: str,
    ) -> None:
        set_hsem_verbose(False)

        await _apply(
            _cfg(),
            _live(),
            _rec(recommendation, charged_kwh=3.0),
            fail_on=fail_on,
            grid_charge_w=grid_charge_w,
        )

        assert capture.messages(logging.ERROR) == [f"{expected}{_SUFFIX}"]

    @pytest.mark.asyncio
    async def test_verbose_on_formats_every_record(self, capture: _Capture) -> None:
        """No record carries an unconverted ``%`` argument (no logging TypeError)."""
        set_hsem_verbose(True)

        await _apply(
            _cfg(),
            _live(),
            _rec(Recommendations.BatteriesDischargeMode.value),
            fail_on=_MODE_ENTITY,
        )

        messages = capture.all_messages()  # raises TypeError on the old bug
        assert any("Working mode write FAILED" in m for m in messages)


class TestUnconfiguredEntityWarning:
    """An unconfigured entity warns once per episode rather than every cycle."""

    @pytest.mark.asyncio
    async def test_warns_once_then_debug_then_rearms(self, capture: _Capture) -> None:
        set_hsem_verbose(True)
        unconfigured = _cfg()
        unconfigured.huawei_solar_batteries_working_mode = None
        rec = _rec(Recommendations.BatteriesDischargeMode.value)
        expected = "Working mode entity not configured; skipping write"
        # Reuse one sensor across cycles: the latch lives on the entity.
        sensor = _sensor()

        await _apply(unconfigured, _live(), rec, sensor=sensor)
        await _apply(unconfigured, _live(), rec, sensor=sensor)
        assert capture.messages(logging.WARNING) == [expected]
        assert capture.messages(logging.DEBUG).count(expected) == 1

        # Configure the entity: the latch re-arms, so a regression warns again.
        await _apply(_cfg(), _live(), rec, sensor=sensor)
        await _apply(unconfigured, _live(), rec, sensor=sensor)

        assert capture.messages(logging.WARNING) == [expected, expected]


class TestLatchedWarningHelper:
    """``log_latched_warning`` semantics, independent of the applier."""

    def test_inactive_condition_without_latch_is_silent(
        self, capture: _Capture
    ) -> None:
        owner = SimpleNamespace()
        assert log_latched_warning(owner, "k", False, "msg") is False
        assert capture.records == []
        assert not hasattr(owner, "_hsem_warning_latch")

    def test_keys_latch_independently(self, capture: _Capture) -> None:
        set_hsem_verbose(False)
        owner = SimpleNamespace()
        assert log_latched_warning(owner, "a", True, "A %s", 1) is True
        assert log_latched_warning(owner, "b", True, "B %s", 2) is True
        assert log_latched_warning(owner, "a", True, "A %s", 1) is True
        # Verbose off: the repeat is DEBUG and therefore filtered out.
        assert capture.messages(logging.WARNING) == ["A 1", "B 2"]
        assert capture.messages(logging.DEBUG) == []


class TestWorkingModeSensorDegradedBlock:
    """The user-facing degraded-mode BLOCKED line is a latched WARNING."""

    @pytest.mark.asyncio
    async def test_blocked_warns_once_with_verbose_off(self, capture: _Capture) -> None:
        set_hsem_verbose(False)
        sensor = _make_sensor()
        emergency = AsyncMock(return_value=CycleApplySummary())
        sensor._async_run_error_mode_emergency_stop = emergency  # type: ignore[method-assign]  # test spy

        for _ in range(2):
            data = _make_minimal_coordinator_data()
            assert data.live is not None
            data.live._degraded_mode = DegradedMode.Error
            data.live.missing_entities_list = ["sensor.battery_soc"]
            await sensor._async_apply_hardware_writes(data)

        assert capture.messages(logging.WARNING) == [
            "Hardware writes BLOCKED — degraded mode: error; "
            "missing: ['sensor.battery_soc']"
        ]
        assert emergency.await_count == 2

    @pytest.mark.asyncio
    async def test_read_only_skip_stays_debug(self, capture: _Capture) -> None:
        set_hsem_verbose(True)
        sensor = _make_sensor()
        data = _make_minimal_coordinator_data()
        assert data.cfg is not None
        data.cfg.read_only = True

        await sensor._async_apply_hardware_writes(data)

        assert "Hardware writes SKIPPED — read_only=True" in capture.messages(
            logging.DEBUG
        )
        assert capture.messages(logging.WARNING) == []

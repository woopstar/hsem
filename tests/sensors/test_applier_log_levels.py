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
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import CycleApplySummary
from custom_components.hsem.utils.logger import (
    HSEM_LOGGER,
    log_latched_warning,
    set_hsem_verbose,
)
from custom_components.hsem.utils.recommendations import Recommendations
from tests.sensors import (
    test_applier_emma as emma,
    test_applier_write_abort_ladder as ladder,
)
from tests.sensors.test_applier_emma import (
    _EMMA_OPTIONS,
    _MODE_ENTITY,
    _apply,
    _cfg,
    _FakeState,
    _live,
)
from tests.test_working_mode_task_lifecycle import (
    _make_minimal_coordinator_data,
    _make_sensor,
)


class _Capture(logging.Handler):
    """Collect records and fail loudly if any of them cannot be formatted."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, level: int) -> list[str]:
        """Return formatted messages at exactly *level*.

        ``getMessage()`` raises ``TypeError`` for a stray ``%`` argument, so
        calling it here is what proves the verbose-mode bug is gone.
        """
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


def _mode_states() -> dict[str, _FakeState]:
    return {_MODE_ENTITY: _FakeState("time_of_use", {"options": _EMMA_OPTIONS})}


class TestFailedWriteLogsError:
    """A verified write that fails is an ERROR, whatever the verbosity."""

    @pytest.mark.asyncio
    async def test_failed_working_mode_write_is_error_with_verbose_off(
        self, capture: _Capture
    ) -> None:
        set_hsem_verbose(False)

        await _apply(
            _live("time_of_use"),
            _mode_states(),
            Recommendations.BatteriesDischargeMode.value,
            failing_entity_prefix=_MODE_ENTITY,
        )

        errors = capture.messages(logging.ERROR)
        assert errors == [
            f"Working mode write FAILED for {_MODE_ENTITY}; "
            "blocking further battery writes this cycle"
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("recommendation", "fail_on", "grid_charge_w", "expected"),
        [
            pytest.param(
                Recommendations.BatteriesDischargeMode.value,
                ladder._DISCHARGE_ENTITY,
                None,
                f"Max discharge power write FAILED for {ladder._DISCHARGE_ENTITY}",
                id="discharge_cap",
            ),
            pytest.param(
                Recommendations.BatteriesChargeGrid.value,
                ladder._GRID_CHARGE_ENTITY,
                3000.0,
                "Grid-charge maximum-power write FAILED for "
                f"{ladder._GRID_CHARGE_ENTITY}",
                id="grid_charge",
            ),
            pytest.param(
                Recommendations.BatteriesDischargeMode.value,
                ladder._EXCESS_ENTITY,
                None,
                f"Excess PV use write FAILED for {ladder._EXCESS_ENTITY}",
                id="excess_pv",
            ),
            pytest.param(
                Recommendations.BatteriesChargeGrid.value,
                f"{ladder._TOU_ENTITY}:",
                None,
                "TOU period write FAILED for device bat1",
                id="tou",
            ),
        ],
    )
    async def test_other_failed_writes_are_error_with_verbose_off(
        self,
        capture: _Capture,
        recommendation: str,
        fail_on: str,
        grid_charge_w: float | None,
        expected: str,
    ) -> None:
        set_hsem_verbose(False)

        await ladder._apply(
            ladder._cfg(),
            ladder._live(),
            ladder._rec(recommendation, charged_kwh=3.0),
            fail_on=fail_on,
            grid_charge_w=grid_charge_w,
        )

        # The applier's own abort line; async_write_and_verify is stubbed out.
        assert capture.messages(logging.ERROR) == [
            f"{expected}; blocking further battery writes this cycle"
        ]

    @pytest.mark.asyncio
    async def test_verbose_on_formats_every_record(self, capture: _Capture) -> None:
        """No record carries an unconverted ``%`` argument (no logging TypeError)."""
        set_hsem_verbose(True)

        await _apply(
            _live("time_of_use"),
            _mode_states(),
            Recommendations.BatteriesDischargeMode.value,
            failing_entity_prefix=_MODE_ENTITY,
        )

        messages = capture.all_messages()  # raises TypeError on the old bug
        assert any("Working mode write FAILED" in m for m in messages)


class TestUnconfiguredEntityWarning:
    """An unconfigured entity warns once per episode rather than every cycle."""

    @pytest.mark.asyncio
    async def test_warns_once_then_debug_then_rearms(self, capture: _Capture) -> None:
        set_hsem_verbose(True)
        cfg = _cfg()
        cfg.huawei_solar_batteries_working_mode = None
        sensor_states = _mode_states()
        rec = Recommendations.BatteriesDischargeMode.value
        expected = "Working mode entity not configured; skipping write"

        # Reuse one sensor across cycles: the latch lives on the entity.
        sensor = emma._sensor(sensor_states)
        with patch.object(emma, "_sensor", return_value=sensor):
            await _apply(_live("time_of_use"), sensor_states, rec, cfg=cfg)
            await _apply(_live("time_of_use"), sensor_states, rec, cfg=cfg)
            assert capture.messages(logging.WARNING) == [expected]
            assert capture.messages(logging.DEBUG).count(expected) == 1

            # Configure the entity: the latch re-arms …
            await _apply(_live("time_of_use"), sensor_states, rec)
            # … so a later regression warns again.
            await _apply(_live("time_of_use"), sensor_states, rec, cfg=cfg)

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

        warnings = capture.messages(logging.WARNING)
        assert warnings == [
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

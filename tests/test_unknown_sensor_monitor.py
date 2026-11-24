"""Tests for the monitor that logs why a sensor is unknown (issue #1482)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State

from custom_components.hsem.custom_sensors.battery_efficiency_sensor import (
    HSEMBatteryEfficiencySensor,
)
from custom_components.hsem.custom_sensors.monthly_energy_sensors import (
    build_monthly_energy_sensors,
)
from custom_components.hsem.models.energy_tracker import EnergyTracker
from custom_components.hsem.unknown_sensor_monitor import (
    STARTUP_CHECKS,
    STARTUP_SECONDS,
    UNEXPLAINED_CHECKS,
    UnknownSensorMonitor,
)
from custom_components.hsem.utils.efficiency_learner import EfficiencyLearner
from custom_components.hsem.utils.unknown_reason import UNKNOWN_REASONS
from tests.custom_sensors.test_monthly_energy_sensors import _day

_MODULE = "custom_components.hsem.unknown_sensor_monitor"
_T0 = datetime(2026, 11, 24, 8, 0, tzinfo=UTC)
_ENTITY_ID = "sensor.hsem_monthly_round_trip_loss"
_UNEXPLAINED_MESSAGE = (
    "%s is unknown and gives no reason. Check the entities it "
    "reads, and report it to HSEM if they are fine."
)


def _reason_call(entity_id: str, code: str) -> Any:
    """Return the log call that reports *code* for *entity_id*."""
    reason = UNKNOWN_REASONS[code]
    level = {"fault": "error", "action_needed": "warning"}.get(reason.kind, "info")
    return call(
        level,
        "%s is unknown: %s (%s). %s",
        entity_id,
        code,
        reason.kind.value,
        reason.sentence,
    )


class _World:
    """A fake Home Assistant with one clock and the states of its sensors."""

    def __init__(self, *entities: Any) -> None:
        self.states: dict[str, str | None] = {}
        self.hass = MagicMock()
        self.hass.states.get.side_effect = self._state
        self.monitor = UnknownSensorMonitor()
        self.monitor.watch(entities)
        self._now = _T0

    def _state(self, entity_id: str) -> State | None:
        state = self.states.get(entity_id)
        return None if state is None else State(entity_id, state)

    def check(self, *, seconds: float = 60.0) -> None:
        """Run one coordinator cycle's check, *seconds* after the last."""
        self._now += timedelta(seconds=seconds)
        self.monitor.check(self.hass, self._now)

    def settle(self) -> None:
        """Run the checks of a start, in which nothing is logged."""
        for _ in range(STARTUP_CHECKS):
            self.check(seconds=STARTUP_SECONDS)


def _entity(reason: str | None = None, entity_id: str = _ENTITY_ID) -> Any:
    return SimpleNamespace(entity_id=entity_id, unknown_reason=reason)


class TestReasonChanges:
    def test_one_line_per_reason_change_and_one_on_recovery(self) -> None:
        entity = _entity("throughput_below_minimum")
        world = _World(entity)
        world.states[_ENTITY_ID] = STATE_UNKNOWN

        with patch(f"{_MODULE}.async_log") as async_log:
            world.settle()
            world.check()
            world.check()
            entity.unknown_reason = "implausible_ratio"
            world.check()
            world.check()
            entity.unknown_reason = None
            world.states[_ENTITY_ID] = "7.5"
            world.check()
            world.check()

        assert async_log.call_args_list == [
            _reason_call(_ENTITY_ID, "throughput_below_minimum"),
            _reason_call(_ENTITY_ID, "implausible_ratio"),
            call("info", "%s has a value again", _ENTITY_ID),
        ]

    @pytest.mark.parametrize(
        ("code", "level"),
        [
            ("throughput_below_minimum", "info"),
            ("ac_battery_power_not_configured", "info"),
            ("ac_battery_power_unit_not_power", "warning"),
            ("implausible_ratio", "error"),
            ("a_code_nobody_mapped", "error"),
        ],
    )
    def test_the_level_follows_the_kind(self, code: str, level: str) -> None:
        world = _World(_entity(code))
        world.states[_ENTITY_ID] = STATE_UNKNOWN

        with patch(f"{_MODULE}.async_log") as async_log:
            world.settle()

        assert [logged.args[0] for logged in async_log.call_args_list] == [level]
        assert async_log.call_args.args[2:4] == (_ENTITY_ID, code)

    def test_a_sensor_with_a_value_logs_nothing(self) -> None:
        world = _World(_entity())
        world.states[_ENTITY_ID] = "7.5"

        with patch(f"{_MODULE}.async_log") as async_log:
            world.settle()
            world.check()

        async_log.assert_not_called()

    def test_an_unavailable_sensor_is_neither_reported_nor_recovered(self) -> None:
        world = _World(_entity("throughput_below_minimum"))
        world.states[_ENTITY_ID] = STATE_UNKNOWN

        with patch(f"{_MODULE}.async_log") as async_log:
            world.settle()
            world.states[_ENTITY_ID] = STATE_UNAVAILABLE
            world.check()
            world.states[_ENTITY_ID] = STATE_UNKNOWN
            world.check()

        assert async_log.call_args_list == [
            _reason_call(_ENTITY_ID, "throughput_below_minimum")
        ]

    def test_a_sensor_without_a_state_is_forgotten(self) -> None:
        """A disabled entity has no state: no recovery line, and a new start."""
        world = _World(_entity("throughput_below_minimum"))
        world.states[_ENTITY_ID] = STATE_UNKNOWN

        with patch(f"{_MODULE}.async_log") as async_log:
            world.settle()
            world.states[_ENTITY_ID] = None
            world.check()
            world.states[_ENTITY_ID] = STATE_UNKNOWN
            world.check()

        assert (
            async_log.call_args_list
            == [_reason_call(_ENTITY_ID, "throughput_below_minimum")] * 2
        )

    def test_an_entity_without_an_id_is_skipped(self) -> None:
        world = _World(_entity("learning", entity_id=""))

        with patch(f"{_MODULE}.async_log") as async_log:
            world.settle()

        async_log.assert_not_called()


class TestStartup:
    def test_nothing_is_logged_in_the_first_cycles(self) -> None:
        """Source entities are not ready: unknown sensors are expected."""
        world = _World(_entity("no_data"), _entity(entity_id="sensor.hsem_other"))
        world.states[_ENTITY_ID] = STATE_UNKNOWN
        world.states["sensor.hsem_other"] = STATE_UNKNOWN

        with patch(f"{_MODULE}.async_log") as async_log:
            for _ in range(STARTUP_CHECKS - 1):
                world.check(seconds=STARTUP_SECONDS)

        async_log.assert_not_called()

    def test_cycles_run_back_to_back_do_not_end_the_start(self) -> None:
        """State changes can run many cycles in the first seconds."""
        world = _World(_entity("not_evaluated"))
        world.states[_ENTITY_ID] = STATE_UNKNOWN

        with patch(f"{_MODULE}.async_log") as async_log:
            for _ in range(10 * STARTUP_CHECKS):
                world.check(seconds=1.0)

        async_log.assert_not_called()

    def test_a_reason_that_ended_during_the_start_is_never_logged(self) -> None:
        entity = _entity("not_evaluated")
        world = _World(entity)
        world.states[_ENTITY_ID] = STATE_UNKNOWN

        with patch(f"{_MODULE}.async_log") as async_log:
            world.check(seconds=STARTUP_SECONDS)
            entity.unknown_reason = None
            world.states[_ENTITY_ID] = "80.0"
            world.settle()
            world.check()

        async_log.assert_not_called()

    def test_the_clock_is_read_when_no_time_is_given(self) -> None:
        monitor = UnknownSensorMonitor()

        with patch(f"{_MODULE}.hsem_now", return_value=_T0) as now:
            monitor.check(MagicMock())

        now.assert_called_once_with()


class TestUnexplained:
    def test_unknown_without_a_reason_warns_once(self) -> None:
        world = _World(_entity())
        world.states[_ENTITY_ID] = STATE_UNKNOWN

        with patch(f"{_MODULE}.async_log") as async_log:
            world.settle()
            for _ in range(UNEXPLAINED_CHECKS + 5):
                world.check()

        assert async_log.call_args_list == [
            call("warning", _UNEXPLAINED_MESSAGE, _ENTITY_ID)
        ]

    def test_a_single_cycle_without_a_reason_does_not_warn(self) -> None:
        """The state of a value that just arrived is not written yet."""
        entity = _entity("throughput_below_minimum")
        world = _World(entity)
        world.states[_ENTITY_ID] = STATE_UNKNOWN

        with patch(f"{_MODULE}.async_log") as async_log:
            world.settle()
            entity.unknown_reason = None
            for _ in range(UNEXPLAINED_CHECKS - 1):
                world.check()
            entity.unknown_reason = "throughput_below_minimum"
            for _ in range(UNEXPLAINED_CHECKS):
                world.check()

        assert async_log.call_args_list == [
            _reason_call(_ENTITY_ID, "throughput_below_minimum")
        ]

    def test_it_warns_again_after_a_value(self) -> None:
        world = _World(_entity())
        world.states[_ENTITY_ID] = STATE_UNKNOWN

        with patch(f"{_MODULE}.async_log") as async_log:
            world.settle()
            for _ in range(UNEXPLAINED_CHECKS):
                world.check()
            world.states[_ENTITY_ID] = "1"
            world.check()
            world.states[_ENTITY_ID] = STATE_UNKNOWN
            for _ in range(UNEXPLAINED_CHECKS):
                world.check()

        assert async_log.call_args_list == [
            call("warning", _UNEXPLAINED_MESSAGE, _ENTITY_ID),
            call("info", "%s has a value again", _ENTITY_ID),
            call("warning", _UNEXPLAINED_MESSAGE, _ENTITY_ID),
        ]

    def test_an_entity_without_the_property_gives_no_reason(self) -> None:
        world = _World(SimpleNamespace(entity_id=_ENTITY_ID))
        world.states[_ENTITY_ID] = STATE_UNKNOWN

        with patch(f"{_MODULE}.async_log") as async_log:
            world.settle()
            for _ in range(UNEXPLAINED_CHECKS):
                world.check()

        assert async_log.call_args_list == [
            call("warning", _UNEXPLAINED_MESSAGE, _ENTITY_ID)
        ]


class TestFailure:
    def test_a_failing_entity_does_not_stop_the_others(self) -> None:
        """The monitor only reports: it must not fail a coordinator cycle."""

        class _Broken:
            entity_id = "sensor.hsem_broken"

            @property
            def unknown_reason(self) -> str | None:
                raise RuntimeError("boom")

        world = _World(_Broken(), _entity("learning"))
        world.states["sensor.hsem_broken"] = STATE_UNKNOWN
        world.states[_ENTITY_ID] = STATE_UNKNOWN

        with patch(f"{_MODULE}.async_log") as async_log:
            world.settle()

        assert async_log.call_args_list[0].args[:3] == (
            "debug",
            "Unknown-sensor check of %s failed: %s",
            "sensor.hsem_broken",
        )
        assert async_log.call_args_list[1:] == [_reason_call(_ENTITY_ID, "learning")]


class TestLiveCase:
    """The install of the issue: three unknown sensors, none of them a fault."""

    def test_two_waiting_lines_and_one_not_configured(self) -> None:
        coordinator = MagicMock()
        # 10.8 kWh charged of the 50 kWh a 10 kWh battery needs, and no
        # AC-side battery power sensor.
        coordinator._energy_tracker = EnergyTracker(
            daily_log={"2026-11-23": _day("2026-11-23", charged=10.8, ac=False)}
        )
        # 12 and 6 samples of the 20 needed are "learning" all the same.
        coordinator._efficiency_learner = EfficiencyLearner()
        coordinator.hass.config.currency = "DKK"
        entry = MagicMock()
        entry.entry_id = "test_entry_id"
        entities: list[Any] = [
            *build_monthly_energy_sensors(entry, coordinator),
            HSEMBatteryEfficiencySensor(entry, coordinator),
        ]
        world = _World(*entities)
        with patch(
            "custom_components.hsem.custom_sensors.monthly_energy_sensors.hsem_now",
            return_value=_T0,
        ):
            for entity in entities:
                value = entity.native_value
                world.states[entity.entity_id] = (
                    STATE_UNKNOWN if value is None else str(value)
                )

            with patch(f"{_MODULE}.async_log") as async_log:
                world.settle()
                world.check()
                world.check()

        assert async_log.call_args_list == [
            _reason_call(
                "sensor.hsem_monthly_round_trip_loss", "throughput_below_minimum"
            ),
            _reason_call(
                "sensor.hsem_monthly_ac_round_trip_loss",
                "ac_battery_power_not_configured",
            ),
            _reason_call("sensor.hsem_battery_efficiency_sensor", "learning"),
        ]
        assert {logged.args[0] for logged in async_log.call_args_list} == {"info"}
        assert [logged.args[4] for logged in async_log.call_args_list] == [
            "waiting",
            "not_configured",
            "waiting",
        ]

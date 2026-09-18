"""Tests for forcible battery discharge and the applier's state readers.

Forcible discharge is a hardware write, so it must be skipped whenever the
preconditions are missing, and its read-back must only report success when
the inverter actually reports an active forcible session.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.custom_sensors.applier_forcible_discharge import (
    _async_apply_forcible_discharge,
)
from custom_components.hsem.custom_sensors.applier_state_readers import (
    _read_number_state,
    _read_select_state,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.inverter_verify import ApplyStatus

_MODULE = "custom_components.hsem.custom_sensors.applier_forcible_discharge"
_VERIFY_MODULE = "custom_components.hsem.utils.inverter_verify"
_FC_ENTITY = "sensor.batteries_forcible_charge"
_ENTITY = "number.batteries_maximum_charging_power"
_ACTIVE = "Discharging at 5000W until 15.0%"
_IDLE = "Stopped"


@pytest.fixture(autouse=True)
def _no_settle_delay() -> Any:
    """Skip the ten-second inverter settle wait between write and read-back."""
    with patch(f"{_VERIFY_MODULE}.asyncio.sleep", AsyncMock()):
        yield


class _FakeState:
    """Minimal stand-in for an HA state object."""

    def __init__(self, state: str) -> None:
        self.state = state


def _accepting_sensor(cfg_entity: str = _FC_ENTITY) -> MagicMock:
    """Return a sensor whose charger reports idle until the write lands.

    ``async_write_and_verify`` reads before writing and skips the write when
    the value already matches, so an always-active charger would never be
    written to at all.
    """
    sensor = MagicMock()
    states = [_FakeState(_IDLE), _FakeState(_ACTIVE)]

    def _get(entity_id: str) -> _FakeState | None:
        if entity_id != cfg_entity:
            return None
        return states[0] if len(states) == 1 else states.pop(0)

    sensor.hass.states.get.side_effect = _get
    return sensor


def _sensor(states: dict[str, _FakeState] | None = None) -> MagicMock:
    """Return an applier sensor stand-in with the given HA states."""
    sensor = MagicMock()
    sensor.hass.states.get.side_effect = (states or {}).get
    return sensor


def _await_args(mock: AsyncMock) -> Any:
    """Return the most recent await's call object, asserting there was one."""
    call = mock.await_args
    assert call is not None
    return call


def _cfg(*, devices: bool = True) -> SensorConfig:
    """Return a config with battery devices and the forcible-charge sensor."""
    cfg = SensorConfig()
    cfg.huawei_solar_batteries_forcible_charge = _FC_ENTITY
    if devices:
        cfg.huawei_solar_device_id_batteries = "battery_1"
    return cfg


def _live(*, capacity_kwh: float = 10.0, eod_soc_pct: float = 15.0) -> LiveState:
    """Return a live snapshot with a usable battery."""
    live = LiveState()
    live.battery_usable_capacity_kwh = capacity_kwh
    live.huawei_batteries_end_of_discharge_soc_pct = eod_soc_pct
    return live


class TestPreconditions:
    """Without the preconditions no hardware write is attempted."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("cfg", "live", "required_kwh", "reason"),
        [
            pytest.param(
                _cfg(), _live(capacity_kwh=0.0), 1.0, "no capacity", id="no_capacity"
            ),
            pytest.param(
                _cfg(), _live(), -1.0, "negative requirement", id="negative_required"
            ),
            pytest.param(
                _cfg(devices=False), _live(), 1.0, "no devices", id="no_battery_device"
            ),
        ],
    )
    async def test_missing_preconditions_skip_the_write(
        self, cfg: SensorConfig, live: LiveState, required_kwh: float, reason: str
    ) -> None:
        """Nothing is written and no result is reported."""
        set_discharge = AsyncMock()

        with patch(f"{_MODULE}.async_set_forcible_discharge", set_discharge):
            results = await _async_apply_forcible_discharge(
                _sensor(), cfg, live, required_kwh, 5000
            )

        assert results == [], reason
        set_discharge.assert_not_awaited()


class TestForcibleDischargeWrite:
    """The write targets the discharge floor and verifies acceptance."""

    @pytest.mark.asyncio
    async def test_write_is_verified_against_the_charge_sensor(self) -> None:
        """A charger that starts reporting an active session verifies the write."""
        set_discharge = AsyncMock()

        with patch(f"{_MODULE}.async_set_forcible_discharge", set_discharge):
            results = await _async_apply_forcible_discharge(
                _accepting_sensor(), _cfg(), _live(), 1.0, 5000
            )

        assert [r.status for r in results] == [ApplyStatus.OK]
        set_discharge.assert_awaited_once()
        _sensor_arg, device_id, target_soc, power = _await_args(set_discharge).args
        assert device_id == "battery_1"
        # Target SoC is the configured end-of-discharge floor.
        assert target_soc == 15
        assert power == 5000

    @pytest.mark.asyncio
    async def test_second_battery_is_skipped_while_already_discharging(self) -> None:
        """Both batteries intentionally share one pack-level read-back sensor.

        Once it reports an active session the second write is unnecessary, so
        ``async_write_and_verify`` reports it as skipped rather than writing
        again.
        """
        cfg = _cfg()
        cfg.huawei_solar_device_id_batteries_2 = "battery_2"
        set_discharge = AsyncMock()

        with patch(f"{_MODULE}.async_set_forcible_discharge", set_discharge):
            results = await _async_apply_forcible_discharge(
                _accepting_sensor(), cfg, _live(), 1.0, 5000
            )

        assert [r.status for r in results] == [ApplyStatus.OK, ApplyStatus.SKIPPED]
        set_discharge.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("eod_soc_pct", "expected"),
        [
            pytest.param(0.0, 5, id="clamped_up_to_five"),
            pytest.param(150.0, 100, id="clamped_down_to_hundred"),
            pytest.param(20.4, 20, id="truncated"),
        ],
    )
    async def test_target_soc_is_clamped_for_safety(
        self, eod_soc_pct: float, expected: int
    ) -> None:
        """The discharge floor is clamped into the 5–100 % range."""
        set_discharge = AsyncMock()

        with patch(f"{_MODULE}.async_set_forcible_discharge", set_discharge):
            await _async_apply_forcible_discharge(
                _accepting_sensor(),
                _cfg(),
                _live(eod_soc_pct=eod_soc_pct),
                1.0,
                5000,
            )

        assert _await_args(set_discharge).args[2] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "state",
        [
            pytest.param(_IDLE, id="idle"),
            pytest.param("unavailable", id="unavailable"),
            pytest.param("unknown", id="unknown"),
            pytest.param("", id="blank"),
        ],
    )
    async def test_unaccepted_command_is_reported_as_failed(self, state: str) -> None:
        """A charger that never reports an active session fails verification."""
        set_discharge = AsyncMock()

        with patch(f"{_MODULE}.async_set_forcible_discharge", set_discharge):
            results = await _async_apply_forcible_discharge(
                _sensor({_FC_ENTITY: _FakeState(state)}), _cfg(), _live(), 1.0, 5000
            )

        assert results[0].status is not ApplyStatus.OK

    @pytest.mark.asyncio
    async def test_without_a_charge_sensor_the_write_cannot_be_verified(self) -> None:
        """No forcible-charge sensor configured → nothing to read back."""
        cfg = _cfg()
        cfg.huawei_solar_batteries_forcible_charge = None
        set_discharge = AsyncMock()

        with patch(f"{_MODULE}.async_set_forcible_discharge", set_discharge):
            results = await _async_apply_forcible_discharge(
                _sensor(), cfg, _live(), 1.0, 5000
            )

        assert results[0].status is ApplyStatus.UNVERIFIED
        assert results[0].entity_id.startswith("forcible_charge:")
        set_discharge.assert_awaited()

    @pytest.mark.asyncio
    async def test_an_unverified_write_stops_before_the_second_battery(self) -> None:
        """An unconfirmed write stops the remaining battery commands."""
        cfg = _cfg()
        cfg.huawei_solar_device_id_batteries_2 = "battery_2"
        set_discharge = AsyncMock()

        with patch(f"{_MODULE}.async_set_forcible_discharge", set_discharge):
            results = await _async_apply_forcible_discharge(
                _sensor({_FC_ENTITY: _FakeState(_IDLE)}), cfg, _live(), 1.0, 5000
            )

        assert [r.status for r in results] == [ApplyStatus.UNVERIFIED]
        set_discharge.assert_awaited()
        assert {call.args[1] for call in set_discharge.await_args_list} == {"battery_1"}

    @pytest.mark.asyncio
    async def test_a_skipped_first_battery_continues_to_the_second(self) -> None:
        """A pack-level active read-back skips both writes without aborting."""
        cfg = _cfg()
        cfg.huawei_solar_device_id_batteries_2 = "battery_2"
        set_discharge = AsyncMock()

        with patch(f"{_MODULE}.async_set_forcible_discharge", set_discharge):
            results = await _async_apply_forcible_discharge(
                _sensor({_FC_ENTITY: _FakeState(_ACTIVE)}), cfg, _live(), 1.0, 5000
            )

        assert [r.status for r in results] == [
            ApplyStatus.SKIPPED,
            ApplyStatus.SKIPPED,
        ]
        set_discharge.assert_not_awaited()


class TestApplierStateReaders:
    """The applier reads back numbers and selects defensively."""

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            pytest.param("5000", 5000.0, id="numeric"),
            pytest.param("5000.5", 5000.5, id="decimal"),
            pytest.param("unavailable", None, id="unavailable"),
            pytest.param("unknown", None, id="unknown"),
            pytest.param("not a number", None, id="non_numeric"),
        ],
    )
    def test_number_reads(self, state: str, expected: float | None) -> None:
        """An unusable number read is missing, never zero."""
        result = _read_number_state(_sensor({_ENTITY: _FakeState(state)}), _ENTITY)

        if expected is None:
            assert result is None
        else:
            assert result == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("entity_id", "states"),
        [
            pytest.param(None, {}, id="unconfigured"),
            pytest.param(_ENTITY, {}, id="entity_missing"),
        ],
    )
    def test_number_reads_without_an_entity(
        self, entity_id: str | None, states: dict[str, Any]
    ) -> None:
        """An unconfigured or absent entity has no reading."""
        assert _read_number_state(_sensor(states), entity_id) is None

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            pytest.param("time_of_use_luna2000", "time_of_use_luna2000", id="option"),
            pytest.param("unavailable", None, id="unavailable"),
            pytest.param("unknown", None, id="unknown"),
        ],
    )
    def test_select_reads(self, state: str, expected: str | None) -> None:
        """A select option is returned verbatim, or reported missing."""
        assert (
            _read_select_state(_sensor({_ENTITY: _FakeState(state)}), _ENTITY)
            == expected
        )

    @pytest.mark.parametrize(
        ("entity_id", "states"),
        [
            pytest.param(None, {}, id="unconfigured"),
            pytest.param(_ENTITY, {}, id="entity_missing"),
        ],
    )
    def test_select_reads_without_an_entity(
        self, entity_id: str | None, states: dict[str, Any]
    ) -> None:
        """An unconfigured or absent select has no option."""
        assert _read_select_state(_sensor(states), entity_id) is None

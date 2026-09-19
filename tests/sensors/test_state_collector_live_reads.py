"""Tests for the live-state collection pass over HA entity states.

``async_collect_live_state`` is the only place HSEM reads Home Assistant, and
every optional input it reads is behind its own configuration guard. The two
things that must hold: a configured entity actually lands on
:class:`LiveState`, and an unconfigured or unreadable one is recorded as a
missing entity rather than silently defaulting to zero.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import State

from custom_components.hsem.custom_sensors.state_collector import (
    _resolve_cached,
    async_collect_all_states,
    async_collect_live_state,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.phase_power import EV_TOPOLOGY_THREE_PHASE_SWITCHABLE

_MODULE = "custom_components.hsem.custom_sensors.state_collector"

_TOU_ENTITY = "sensor.tou"
_PERIODS = ["00:00-23:59/1234567/+"]


def _sensor(units: dict[str, str] | None = None) -> MagicMock:
    """Return a stand-in sensor whose entities report *units*."""
    sensor = MagicMock()
    sensor._config_entry.options = {}
    sensor._config_entry.data = {}

    def _state(entity_id: str) -> State | None:
        unit = (units or {}).get(entity_id)
        if unit is None:
            return None
        return State(entity_id, "1.0", {"unit_of_measurement": unit})

    sensor.hass.states.get.side_effect = _state
    return sensor


def _full_cfg() -> SensorConfig:
    """Return a config with every optional live input wired up."""
    cfg = SensorConfig()
    for charger, prefix in ((cfg.ev, "ev"), (cfg.ev_second, "ev2")):
        charger.status_entity = f"binary_sensor.{prefix}_charging"
        charger.power_entity = f"sensor.{prefix}_power"
        charger.soc_entity = f"sensor.{prefix}_soc"
        charger.connected_entity = f"binary_sensor.{prefix}_connected"
    cfg.phase_aware_charging_enabled = True
    cfg.huawei_solar_batteries_grid_charge_maximum_power = "number.grid_charge_max"
    cfg.huawei_solar_power_meter_phase_a_active_power = "sensor.phase_a"
    cfg.huawei_solar_power_meter_phase_b_active_power = "sensor.phase_b"
    cfg.huawei_solar_power_meter_phase_c_active_power = "sensor.phase_c"
    cfg.huawei_solar_batteries_charge_discharge_power = "sensor.battery_power"
    cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = _TOU_ENTITY
    cfg.grid_import_energy_entity = "sensor.grid_import"
    cfg.grid_export_energy_entity = "sensor.grid_export"
    cfg.pv_energy_entity = "sensor.pv_energy"
    return cfg


def _reader(values: dict[str, Any], default: Any = 1.0) -> Any:
    """Return an ``ha_get_entity_state_and_convert`` stub resolving *values*."""

    def _read(
        sensor: Any, entity_id: str, conv_type: str | None = None, decimals: int = 3
    ) -> Any:
        if entity_id in values:
            return values[entity_id]
        return default

    return _read


async def _collect(
    cfg: SensorConfig,
    *,
    values: dict[str, Any] | None = None,
    units: dict[str, str] | None = None,
) -> LiveState:
    """Run the live collection pass against stubbed HA reads."""
    with (
        patch(f"{_MODULE}.ha_get_entity_state_and_convert", _reader(values or {})),
        patch(
            f"{_MODULE}.async_resolve_entity_id_from_unique_id",
            AsyncMock(return_value="select.force_working_mode"),
        ),
        patch(f"{_MODULE}._register_listeners", AsyncMock(return_value=[])),
    ):
        state, _fwm, _unsubs = await async_collect_live_state(
            _sensor(units), cfg, None, set(), entry_id="test_entry"
        )
    return state


class TestOptionalLiveInputs:
    """A configured optional input is read; an unconfigured one is not."""

    @pytest.mark.asyncio
    async def test_both_ev_chargers_are_read(self) -> None:
        """Each charger's status, power, SoC and plug state land on LiveState."""
        state = await _collect(
            _full_cfg(),
            values={
                "binary_sensor.ev_charging": "on",
                "sensor.ev_power": 7.4,
                "sensor.ev_soc": 42.0,
                "binary_sensor.ev_connected": "on",
                "binary_sensor.ev2_charging": "off",
                "sensor.ev2_power": 3700.0,
                "sensor.ev2_soc": 80.0,
                "binary_sensor.ev2_connected": "off",
            },
            # The primary charger reports kW, so its reading must be scaled.
            units={"sensor.ev_power": UnitOfPower.KILO_WATT},
        )

        assert state.ev.is_charging is True
        assert state.ev.power_w == pytest.approx(7400.0)
        assert state.ev.soc_pct == pytest.approx(42.0)
        assert state.ev.is_connected is True
        assert state.ev_second.is_charging is False
        assert state.ev_second.power_w == pytest.approx(3700.0)
        assert state.ev_second.soc_pct == pytest.approx(80.0)
        assert state.ev_second.is_connected is False

    @pytest.mark.asyncio
    async def test_the_phase_limiter_inputs_are_read_when_enabled(self) -> None:
        """The per-phase meter and battery power feed the fuse limiter."""
        state = await _collect(
            _full_cfg(),
            values={
                "number.grid_charge_max": 5000.0,
                "sensor.phase_a": 1000.0,
                "sensor.phase_b": 2.0,
                "sensor.phase_c": 3000.0,
                "sensor.battery_power": -1500.0,
            },
            units={"sensor.phase_b": UnitOfPower.KILO_WATT},
        )

        assert state.huawei_batteries_grid_charge_max_power_w == pytest.approx(5000.0)
        assert state.grid_phase_power_w == (
            pytest.approx(1000.0),
            pytest.approx(2000.0),
            pytest.approx(3000.0),
        )
        assert state.huawei_batteries_charge_discharge_power_w == pytest.approx(-1500.0)

    @pytest.mark.asyncio
    async def test_the_phase_limiter_inputs_are_skipped_when_disabled(self) -> None:
        """An opt-in feature reads nothing while it is off."""
        cfg = _full_cfg()
        cfg.phase_aware_charging_enabled = False

        state = await _collect(cfg, values={"number.grid_charge_max": 5000.0})

        assert state.huawei_batteries_grid_charge_max_power_w is None

    @pytest.mark.asyncio
    async def test_switchable_ev_reads_phase_safety_without_battery_limiter(
        self,
    ) -> None:
        """A switchable EV obtains phase proof independently of battery limiting."""
        cfg = _full_cfg()
        cfg.phase_aware_charging_enabled = False
        cfg.ev_planned_load_enabled = True
        cfg.ev_planned_load_charger_phase_topology = EV_TOPOLOGY_THREE_PHASE_SWITCHABLE

        state = await _collect(
            cfg,
            values={
                "sensor.phase_a": 1000.0,
                "sensor.phase_b": 2000.0,
                "sensor.phase_c": 3000.0,
            },
        )

        assert state.grid_phase_power_w == (
            pytest.approx(1000.0),
            pytest.approx(2000.0),
            pytest.approx(3000.0),
        )
        assert state.huawei_batteries_grid_charge_max_power_w is None
        assert state.huawei_batteries_charge_discharge_power_w is None

    @pytest.mark.asyncio
    async def test_missing_switchable_phase_inputs_do_not_degrade_cycle(self) -> None:
        """Absent optional phase proof rejects only the hold, not the plan."""
        cfg = _full_cfg()
        cfg.phase_aware_charging_enabled = False
        cfg.huawei_solar_power_meter_phase_a_active_power = None
        cfg.huawei_solar_power_meter_phase_b_active_power = None
        cfg.huawei_solar_power_meter_phase_c_active_power = None
        baseline = await _collect(cfg)

        cfg.ev_planned_load_enabled = True
        cfg.ev_planned_load_charger_phase_topology = EV_TOPOLOGY_THREE_PHASE_SWITCHABLE
        state = await _collect(cfg)

        assert state.grid_phase_power_w == (None, None, None)
        assert state.missing_entities_list == baseline.missing_entities_list

    @pytest.mark.asyncio
    async def test_the_cumulative_energy_meters_are_normalised(self) -> None:
        """A meter reporting Wh is converted before the tracker sees it."""
        state = await _collect(
            _full_cfg(),
            values={
                "sensor.grid_import": 1234.0,
                "sensor.grid_export": 50.0,
                "sensor.pv_energy": 900.0,
            },
            units={"sensor.grid_import": UnitOfEnergy.WATT_HOUR},
        )

        assert state.grid_import_energy_kwh == pytest.approx(1.234)
        assert state.grid_export_energy_kwh == pytest.approx(50.0)
        assert state.pv_energy_kwh == pytest.approx(900.0)


class TestTouPeriodsRead:
    """The TOU schedule lives in the entity's attributes, not its state."""

    @pytest.mark.asyncio
    async def test_the_period_attributes_are_captured(self) -> None:
        """``Period N`` attributes become the live schedule."""
        tou_state = State(_TOU_ENTITY, "1", {"Period 1": _PERIODS[0]})

        state = await _collect(_full_cfg(), values={_TOU_ENTITY: tou_state})

        assert state.tou_periods.raw_state == "1"
        assert state.tou_periods.periods == _PERIODS

    @pytest.mark.asyncio
    async def test_a_non_state_reading_is_recorded_as_missing(self) -> None:
        """Without a ``State`` object there are no attributes to read."""
        state = await _collect(_full_cfg(), values={_TOU_ENTITY: "1"})

        assert state.missing_entities is True
        assert any(
            "not of type State" in entry for entry in state.missing_entities_list
        )

    @pytest.mark.asyncio
    async def test_an_unconfigured_tou_entity_is_recorded_as_missing(self) -> None:
        """HSEM cannot verify a schedule it cannot read."""
        cfg = _full_cfg()
        cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = None

        state = await _collect(cfg)

        assert any(
            "Missing entity: TOU periods" in entry
            for entry in state.missing_entities_list
        )


class TestUnconfiguredCriticalInputs:
    """A bare configuration names every input it is missing."""

    @pytest.mark.asyncio
    async def test_unset_entities_are_each_named(self) -> None:
        """The user needs the label of every input they still have to pick."""
        state = await _collect(SensorConfig())

        assert state.missing_entities is True
        # Labels, not bare ``None``, so the message is actionable.
        assert "Missing entity: house_consumption_power" in state.missing_entities_list
        assert "Missing entity: state_of_capacity" in state.missing_entities_list


class TestEnergyAverageResolution:
    """HSEM's own average sensors may not exist on the first cycle."""

    @pytest.mark.asyncio
    async def test_an_unresolvable_average_sensor_is_skipped(self) -> None:
        """A registry lookup that fails leaves the value out, not at zero."""
        with (
            patch(
                f"{_MODULE}.async_collect_live_state",
                AsyncMock(return_value=(LiveState(), None, [])),
            ),
            patch(
                f"{_MODULE}.async_resolve_entity_id_from_unique_id",
                AsyncMock(return_value=None),
            ),
            patch(f"{_MODULE}._LOGGER") as logger,
        ):
            snapshot, _force, _unsubs = await async_collect_all_states(
                _sensor(), SensorConfig(), None, set(), {}, entry_id="test_entry"
            )

        assert snapshot.energy_average_values == {}
        assert any(
            "not ready/found" in str(call.args[0])
            for call in logger.debug.call_args_list
        )

    @pytest.mark.asyncio
    async def test_a_resolved_entity_id_is_cached(self) -> None:
        """The registry is consulted once per unique id, then cached."""
        cache: dict[str, str] = {}
        resolve = AsyncMock(return_value="sensor.energy_avg")

        with patch(f"{_MODULE}.async_resolve_entity_id_from_unique_id", resolve):
            first = await _resolve_cached(_sensor(), cache, "uid-1")
            second = await _resolve_cached(_sensor(), cache, "uid-1")

        assert (first, second) == ("sensor.energy_avg", "sensor.energy_avg")
        resolve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failed_resolution_is_not_cached(self) -> None:
        """An unregistered sensor is retried on the next cycle."""
        cache: dict[str, str] = {}

        with (
            patch(
                f"{_MODULE}.async_resolve_entity_id_from_unique_id",
                AsyncMock(return_value=None),
            ),
            patch(f"{_MODULE}._LOGGER") as logger,
        ):
            resolved = await _resolve_cached(_sensor(), cache, "uid-1")

        assert resolved is None
        assert cache == {}
        assert any(
            "Failed to resolve" in str(call.args[0])
            for call in logger.debug.call_args_list
        )

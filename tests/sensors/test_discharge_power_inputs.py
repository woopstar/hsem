"""Tests for the two live readings of the discharging-power number entity.

The entity's state is the limit HSEM last commanded; its ``max`` attribute is
the highest value the hardware accepts (issue #1291). They land on separate
:class:`LiveState` fields, and only a missing state is a critical gap.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.core import State

from custom_components.hsem.custom_sensors.discharge_power_inputs import (
    read_max_discharge_power,
)
from custom_components.hsem.custom_sensors.state_collector import (
    async_collect_live_state,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig

_COLLECTOR = "custom_components.hsem.custom_sensors.state_collector"
_ENTITY = "number.batteries_maximum_discharging_power"


def _sensor(attributes: dict[str, Any] | None, state: str = "2000") -> MagicMock:
    """Return a sensor whose discharge entity has *attributes*, or is absent."""
    sensor = MagicMock()
    sensor._config_entry.options = {}
    sensor._config_entry.data = {}

    def _state(entity_id: str) -> State | None:
        if entity_id != _ENTITY or attributes is None:
            return None
        return State(entity_id, state, attributes)

    sensor.hass.states.get.side_effect = _state
    return sensor


def _cfg() -> SensorConfig:
    cfg = SensorConfig()
    cfg.huawei_solar_batteries_maximum_discharging_power = _ENTITY
    return cfg


def _read(cfg: SensorConfig, sensor: MagicMock, commanded: Any) -> LiveState:
    """Run the reader with a closure that returns *commanded* for the state."""
    state = LiveState()
    read_max_discharge_power(sensor, cfg, state, lambda *args, **kwargs: commanded)
    return state


class TestReadMaxDischargePower:
    """The commanded limit and the entity's own maximum are read apart."""

    def test_state_and_maximum_land_on_separate_fields(self) -> None:
        """The report: commanded 2000 W on an entity that accepts 4400 W."""
        state = _read(_cfg(), _sensor({"min": 0, "max": 4400, "step": 1}), 2000.0)

        assert state.huawei_batteries_max_discharge_power_w == pytest.approx(2000.0)
        assert state.huawei_batteries_max_discharge_power_limit_w == pytest.approx(
            4400.0
        )
        assert state.missing_entities_list == []

    @pytest.mark.parametrize("attributes", [{}, {"max": None}, {"max": "unknown"}])
    def test_unusable_maximum_is_none_and_not_a_missing_entity(
        self, attributes: dict[str, Any]
    ) -> None:
        """The capacity table still gives a limit, so the cycle is not degraded."""
        state = _read(_cfg(), _sensor(attributes), 2000.0)

        assert state.huawei_batteries_max_discharge_power_w == pytest.approx(2000.0)
        assert state.huawei_batteries_max_discharge_power_limit_w is None
        assert state.missing_entities_list == []

    def test_unreadable_state_is_still_a_critical_missing_entity(self) -> None:
        state = _read(_cfg(), _sensor({"max": 4400}), None)

        assert state.huawei_batteries_max_discharge_power_w is None
        assert state.huawei_batteries_max_discharge_power_limit_w == pytest.approx(
            4400.0
        )
        assert any(
            "battery max discharge power returned None" in item
            for item in state.missing_entities_list
        )

    def test_unconfigured_entity_has_no_maximum(self) -> None:
        cfg = SensorConfig()
        cfg.huawei_solar_batteries_maximum_discharging_power = None

        state = _read(cfg, _sensor({"max": 4400}), None)

        assert state.huawei_batteries_max_discharge_power_limit_w is None


class TestCollectedLiveState:
    """The live collection pass fills both fields."""

    @pytest.mark.asyncio
    async def test_the_maximum_reaches_live_state(self) -> None:
        def _convert(
            sensor: Any, entity_id: str, conv_type: str | None = None, decimals: int = 3
        ) -> Any:
            return 2000.0 if entity_id == _ENTITY else 1.0

        with (
            patch(f"{_COLLECTOR}.ha_get_entity_state_and_convert", _convert),
            patch(
                f"{_COLLECTOR}.async_resolve_entity_id_from_unique_id",
                AsyncMock(return_value="select.force_working_mode"),
            ),
            patch(f"{_COLLECTOR}._register_listeners", AsyncMock(return_value=[])),
        ):
            state, _fwm, _unsubs = await async_collect_live_state(
                _sensor({"min": 0, "max": 4400}), _cfg(), None, set(), entry_id="e"
            )

        assert state.huawei_batteries_max_discharge_power_w == pytest.approx(2000.0)
        assert state.huawei_batteries_max_discharge_power_limit_w == pytest.approx(
            4400.0
        )

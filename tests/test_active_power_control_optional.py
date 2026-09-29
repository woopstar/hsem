"""The export-limit feedback entity is optional (issue #1120, 6.3.x).

EMMA systems have no active power control sensor, so the Huawei step must
accept an empty field, a cleared field must stay cleared in both flows, and
an unconfigured entity must not be recorded as a missing input.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from custom_components.hsem.config_flow import HSEMConfigFlow
from custom_components.hsem.custom_sensors.state_collector import (
    async_collect_live_state,
)
from custom_components.hsem.flows.huawei_solar import (
    get_huawei_solar_step_schema,
    validate_huawei_solar_input,
)
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.options_flow import HSEMOptionsFlow

_KEY = "hsem_huawei_solar_inverter_active_power_control"
_VALIDATOR = "custom_components.hsem.utils.config_validator"
_COLLECTOR = "custom_components.hsem.custom_sensors.state_collector"
_NEXT_STEP = {"type": "form", "step_id": "battery_economics"}

_REQUIRED_ENTITIES = (
    "hsem_huawei_solar_batteries_working_mode",
    "hsem_huawei_solar_batteries_state_of_capacity",
    "hsem_huawei_solar_batteries_maximum_charging_power",
    "hsem_huawei_solar_batteries_grid_charge_cutoff_soc",
    "hsem_huawei_solar_batteries_charging_cutoff_capacity",
    "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods",
    "hsem_huawei_solar_batteries_rated_capacity",
    "hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou",
    "hsem_huawei_solar_batteries_end_of_discharge_soc",
)


def _valid_input() -> dict[str, Any]:
    user_input: dict[str, Any] = {
        field: f"sensor.{field.removeprefix('hsem_')}" for field in _REQUIRED_ENTITIES
    }
    user_input["hsem_huawei_solar_device_id_inverter_1"] = "emma"
    return user_input


def _entry(options: dict[str, Any] | None = None) -> MagicMock:
    entry = MagicMock()
    entry.options = dict(options or {})
    entry.data = {}
    return entry


class TestValidator:
    @pytest.mark.asyncio
    async def test_the_sensor_may_be_left_empty(self) -> None:
        with (
            patch(f"{_VALIDATOR}.async_entity_exists", AsyncMock(return_value=True)),
            patch(f"{_VALIDATOR}.async_device_exists", AsyncMock(return_value=True)),
        ):
            errors = await validate_huawei_solar_input(MagicMock(), _valid_input())

        assert errors == {}

    @pytest.mark.asyncio
    async def test_a_selected_sensor_must_exist(self) -> None:
        user_input = _valid_input()
        user_input[_KEY] = "sensor.missing_apc"

        async def entity_exists(_hass: Any, entity_id: str) -> bool:
            return entity_id != "sensor.missing_apc"

        with (
            patch(f"{_VALIDATOR}.async_entity_exists", entity_exists),
            patch(f"{_VALIDATOR}.async_device_exists", AsyncMock(return_value=True)),
        ):
            errors = await validate_huawei_solar_input(MagicMock(), user_input)

        assert errors == {_KEY: "entity_not_found"}


class TestSchema:
    @pytest.mark.asyncio
    async def test_the_field_is_optional_with_a_suggested_value(self) -> None:
        """A default would refill a cleared field, so only suggest the value."""
        schema = await get_huawei_solar_step_schema(_entry({_KEY: "sensor.old"}))
        marker = next(m for m in schema.schema if str(m) == _KEY)

        assert isinstance(marker, vol.Optional)
        assert marker.default is vol.UNDEFINED
        assert marker.description == {"suggested_value": "sensor.old"}


class TestClearing:
    @pytest.mark.asyncio
    async def test_config_flow_stores_a_cleared_field_as_empty(self) -> None:
        flow = HSEMConfigFlow()
        flow.hass = MagicMock()
        with (
            patch(
                "custom_components.hsem.config_flow.validate_huawei_solar_input",
                AsyncMock(return_value={}),
            ),
            patch.object(
                flow, "async_step_battery_economics", AsyncMock(return_value=_NEXT_STEP)
            ),
        ):
            await flow.async_step_huawei_solar(
                {"hsem_huawei_solar_device_id_inverter_1": "emma"}
            )

        assert flow._user_input[_KEY] == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("submitted", "expected"),
        [
            pytest.param({}, "", id="cleared"),
            pytest.param({_KEY: "sensor.apc"}, "sensor.apc", id="selected"),
        ],
    )
    async def test_options_flow_overrides_the_saved_entity(
        self, submitted: dict[str, Any], expected: str
    ) -> None:
        flow = HSEMOptionsFlow(_entry({_KEY: "sensor.old"}))
        flow.hass = MagicMock()
        with (
            patch(
                "custom_components.hsem.options_flow.validate_huawei_solar_input",
                AsyncMock(return_value={}),
            ),
            patch.object(
                flow, "async_step_battery_economics", AsyncMock(return_value=_NEXT_STEP)
            ),
        ):
            await flow.async_step_huawei_solar(
                {"hsem_huawei_solar_device_id_inverter_1": "emma", **submitted}
            )

        assert flow._user_input[_KEY] == expected


class TestLiveRead:
    async def _collect(self, cfg: SensorConfig, value: str = "Limited to 80%") -> Any:
        sensor = MagicMock()
        sensor._config_entry.options = {}
        sensor._config_entry.data = {}
        with (
            patch(
                f"{_COLLECTOR}.ha_get_entity_state_and_convert",
                lambda _s, entity_id, *_a, **_k: (
                    value if entity_id == "sensor.apc" else 1.0
                ),
            ),
            patch(
                f"{_COLLECTOR}.async_resolve_entity_id_from_unique_id",
                AsyncMock(return_value="select.force_working_mode"),
            ),
            patch(f"{_COLLECTOR}._register_listeners", AsyncMock(return_value=[])),
        ):
            state, _fwm, _unsubs = await async_collect_live_state(
                sensor, cfg, None, set(), entry_id="test_entry"
            )
        return state

    @pytest.mark.asyncio
    async def test_an_unconfigured_entity_is_not_missing(self) -> None:
        state = await self._collect(SensorConfig())

        assert state.huawei_inverter_active_power_control is None
        assert not any(
            "inverter_active_power_control" in label
            for label in state.missing_entities_list
        )

    @pytest.mark.asyncio
    async def test_a_configured_entity_is_read(self) -> None:
        cfg = SensorConfig()
        cfg.huawei_solar_inverter_active_power_control = "sensor.apc"

        state = await self._collect(cfg)

        assert state.huawei_inverter_active_power_control == "Limited to 80%"

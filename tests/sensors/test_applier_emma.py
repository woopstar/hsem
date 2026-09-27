"""Tests for EMMA-aware working-mode and TOU routing in the applier.

Covers the helpers that let HSEM drive both direct-LUNA and EMMA-managed
Huawei systems: select-option discovery, TOU device routing, and the
applier's working-mode write/skip/fail paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.custom_sensors.applier import async_apply_battery_settings
from custom_components.hsem.custom_sensors.applier_caps import _tou_device_ids
from custom_components.hsem.custom_sensors.applier_state_readers import (
    _read_select_options,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import ApplyResult, ApplyStatus
from custom_components.hsem.utils.recommendations import Recommendations

_APPLIER = "custom_components.hsem.custom_sensors.applier"
_MODE_ENTITY = "select.batteries_working_mode"
_EMMA_OPTIONS = ["time_of_use", "maximum_self_consumption", "fully_fed_to_grid"]


@dataclass
class _FakeState:
    state: str
    attributes: dict[str, Any] = field(default_factory=dict)


def _sensor(states: dict[str, _FakeState]) -> MagicMock:
    sensor = MagicMock()
    sensor.hass.states.get = MagicMock(side_effect=states.get)
    return sensor


# ---------------------------------------------------------------------------
# _read_select_options
# ---------------------------------------------------------------------------


class TestReadSelectOptions:
    """The advertised options list is returned only when well-formed."""

    def test_list_of_strings_is_returned(self) -> None:
        sensor = _sensor({_MODE_ENTITY: _FakeState("x", {"options": _EMMA_OPTIONS})})
        assert _read_select_options(sensor, _MODE_ENTITY) == _EMMA_OPTIONS

    def test_tuple_is_returned_as_list(self) -> None:
        sensor = _sensor({_MODE_ENTITY: _FakeState("x", {"options": ("a", "b")})})
        assert _read_select_options(sensor, _MODE_ENTITY) == ["a", "b"]

    @pytest.mark.parametrize(
        ("entity_id", "states"),
        [
            pytest.param(None, {}, id="unconfigured"),
            pytest.param(_MODE_ENTITY, {}, id="entity_missing"),
            pytest.param(
                _MODE_ENTITY, {_MODE_ENTITY: _FakeState("x")}, id="no_options"
            ),
            pytest.param(
                _MODE_ENTITY,
                {_MODE_ENTITY: _FakeState("x", {"options": "time_of_use"})},
                id="not_a_list",
            ),
            pytest.param(
                _MODE_ENTITY,
                {_MODE_ENTITY: _FakeState("x", {"options": ["a", 1]})},
                id="non_string_item",
            ),
        ],
    )
    def test_malformed_or_missing_returns_none(
        self, entity_id: str | None, states: dict[str, _FakeState]
    ) -> None:
        assert _read_select_options(_sensor(states), entity_id) is None


# ---------------------------------------------------------------------------
# _tou_device_ids
# ---------------------------------------------------------------------------


class TestTouDeviceIds:
    """A TOU controller replaces the battery devices; otherwise legacy routing."""

    def test_controller_replaces_battery_devices(self) -> None:
        cfg = SensorConfig()
        cfg.huawei_solar_device_id_batteries = "bat1"
        cfg.huawei_solar_device_id_batteries_2 = "bat2"
        cfg.huawei_solar_device_id_tou_controller = "emma"
        assert _tou_device_ids(cfg) == ["emma"]

    def test_without_controller_uses_battery_devices(self) -> None:
        cfg = SensorConfig()
        cfg.huawei_solar_device_id_batteries = "bat1"
        cfg.huawei_solar_device_id_batteries_2 = "bat2"
        assert _tou_device_ids(cfg) == ["bat1", "bat2"]


# ---------------------------------------------------------------------------
# async_apply_battery_settings — working-mode resolution paths
# ---------------------------------------------------------------------------


def _cfg() -> SensorConfig:
    cfg = SensorConfig()
    cfg.read_only = False
    cfg.huawei_solar_batteries_working_mode = _MODE_ENTITY
    cfg.huawei_solar_batteries_maximum_discharging_power = "number.max_discharge"
    cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou = "select.excess_pv"
    return cfg


def _live(working_mode: str) -> LiveState:
    live = LiveState()
    live._degraded_mode = DegradedMode.OK
    live.huawei_batteries_rated_capacity_wh = 10000.0
    live.huawei_batteries_working_mode = working_mode
    live.huawei_batteries_excess_pv_use_in_tou = "charge"
    live.huawei_batteries_forcible_charge_state = None
    return live


def _rec(recommendation: str) -> HourlyRecommendation:
    now = datetime.now(UTC)
    return HourlyRecommendation(
        start=now,
        end=now + timedelta(minutes=15),
        recommendation=recommendation,
        avg_house_consumption_kwh=0.5,
        avg_house_consumption_1d_kwh=0.5,
        avg_house_consumption_3d_kwh=0.5,
        avg_house_consumption_7d_kwh=0.5,
        avg_house_consumption_14d_kwh=0.5,
        batteries_charged_kwh=0.0,
        batteries_discharged_kwh=0.5,
        estimated_battery_capacity_kwh=5.0,
        estimated_battery_soc_pct=50.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.0,
        export_price=0.1,
        grid_export_kwh=0.0,
        grid_import_kwh=0.0,
        import_price=0.3,
        solcast_pv_estimate_kwh=0.0,
    )


async def _apply(
    live: LiveState,
    states: dict[str, _FakeState],
    recommendation: str,
    *,
    cfg: SensorConfig | None = None,
    tou_writer: AsyncMock | None = None,
    failing_entity_prefix: str | None = None,
) -> tuple[Any, AsyncMock]:
    sensor = _sensor(states)
    select_writer = AsyncMock()
    live.huawei_batteries_max_discharge_power_w = 5000

    async def _verify(**kwargs: Any) -> Any:
        await kwargs["writer"]()
        failed = failing_entity_prefix is not None and kwargs["entity_id"].startswith(
            failing_entity_prefix
        )
        return ApplyResult(
            entity_id=kwargs["entity_id"],
            desired=kwargs["desired"],
            actual=None,
            status=ApplyStatus.FAILED if failed else ApplyStatus.OK,
        )

    with (
        patch(f"{_APPLIER}.async_set_select_option", select_writer),
        patch(f"{_APPLIER}.async_set_number_value", AsyncMock()),
        patch(f"{_APPLIER}.async_set_tou_periods", tou_writer or AsyncMock()),
        patch(f"{_APPLIER}.async_write_and_verify", AsyncMock(side_effect=_verify)),
        patch(f"{_APPLIER}.get_max_discharge_power", return_value=5000),
    ):
        summary = await async_apply_battery_settings(
            sensor, cfg or _cfg(), live, _rec(recommendation), 0.0
        )
    return summary, select_writer


class TestApplierWorkingModeResolution:
    """The applier writes the option the selected entity actually supports."""

    @pytest.mark.asyncio
    async def test_emma_option_is_written(self) -> None:
        """An MSC intent is written as EMMA's ``maximum_self_consumption``."""
        live = _live("time_of_use")
        states = {_MODE_ENTITY: _FakeState("time_of_use", {"options": _EMMA_OPTIONS})}
        _summary, writer = await _apply(
            live, states, Recommendations.BatteriesDischargeMode.value
        )
        mode_calls = [c for c in writer.await_args_list if c.args[1] == _MODE_ENTITY]
        assert len(mode_calls) == 1
        assert mode_calls[0].args[2] == "maximum_self_consumption"

    @pytest.mark.asyncio
    async def test_emma_live_mode_already_matching_skips_write(self) -> None:
        """EMMA's live ``maximum_self_consumption`` equals the MSC intent."""
        live = _live("maximum_self_consumption")
        states = {
            _MODE_ENTITY: _FakeState(
                "maximum_self_consumption", {"options": _EMMA_OPTIONS}
            )
        }
        _summary, writer = await _apply(
            live, states, Recommendations.BatteriesDischargeMode.value
        )
        assert not any(call.args[1] == _MODE_ENTITY for call in writer.await_args_list)

    @pytest.mark.asyncio
    async def test_unsupported_mode_is_recorded_as_failed(self) -> None:
        """A select without a matching option surfaces a FAILED result."""
        live = _live("adaptive")
        states = {_MODE_ENTITY: _FakeState("adaptive", {"options": ["adaptive"]})}
        summary, writer = await _apply(
            live, states, Recommendations.BatteriesDischargeMode.value
        )
        assert not any(call.args[1] == _MODE_ENTITY for call in writer.await_args_list)
        mode_results = [r for r in summary.results if r.entity_id == _MODE_ENTITY]
        assert len(mode_results) == 1
        assert mode_results[0].status == ApplyStatus.FAILED
        assert mode_results[0].actual == "adaptive"
        assert _MODE_ENTITY in summary.failed_entities


class TestApplierTouControllerRouting:
    """TOU periods go to the controller; a failed write blocks the mode write."""

    @pytest.mark.asyncio
    async def test_failed_controller_tou_write_blocks_working_mode(self) -> None:
        cfg = _cfg()
        cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = (
            "sensor.emma_tou_periods"
        )
        cfg.huawei_solar_device_id_batteries = "bat1"
        cfg.huawei_solar_device_id_tou_controller = "emma"
        live = _live("maximum_self_consumption")
        states = {
            _MODE_ENTITY: _FakeState(
                "maximum_self_consumption", {"options": _EMMA_OPTIONS}
            )
        }
        tou_writer = AsyncMock()

        summary, select_writer = await _apply(
            live,
            states,
            Recommendations.BatteriesChargeGrid.value,
            cfg=cfg,
            tou_writer=tou_writer,
            failing_entity_prefix="sensor.emma_tou_periods",
        )

        assert [call.args[1] for call in tou_writer.await_args_list] == ["emma"]
        assert summary.failed_entities == ["sensor.emma_tou_periods:emma"]
        assert not any(
            call.args[1] == _MODE_ENTITY for call in select_writer.await_args_list
        )

"""Regression tests for managed-OCPP Huawei phase-headroom reservation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

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
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.workingmodes import WorkingModes

_MODULE = "custom_components.hsem.custom_sensors.applier"
_DISCHARGE_ENTITY = "number.maxdis"
_NOW = datetime(2026, 9, 20, 22, 45, tzinfo=UTC)


def _config(*, is_second: bool, managed: bool) -> SensorConfig:
    """Return config for one active EV path."""
    cfg = SensorConfig()
    cfg.read_only = False
    cfg.huawei_solar_batteries_maximum_discharging_power = _DISCHARGE_ENTITY
    cfg.huawei_solar_batteries_grid_charge_maximum_power = "number.gridcharge"
    cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou = "select.excess"
    cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = "sensor.tou"
    cfg.huawei_solar_batteries_working_mode = "select.mode"
    cfg.huawei_solar_device_id_batteries = "battery"
    if is_second:
        cfg.ocpp_enabled = managed
        cfg.ocpp_second_enabled = True
        cfg.ev_second_planned_load_enabled = managed
    else:
        cfg.ocpp_enabled = True
        cfg.ev_planned_load_enabled = managed
    return cfg


def _live(*, is_second: bool, managed: bool) -> LiveState:
    """Return a live 3.705 kW EV session and writable Huawei state."""
    live = LiveState()
    live._degraded_mode = DegradedMode.OK
    live.battery_current_capacity_kwh = 5.0
    live.huawei_batteries_rated_capacity_wh = 5000
    live.huawei_batteries_max_discharge_power_w = 2500
    live.huawei_batteries_grid_charge_max_power_w = 0
    live.huawei_batteries_working_mode = WorkingModes.TimeOfUse.value
    live.huawei_batteries_excess_pv_use_in_tou = "fed_to_grid"
    ev = live.ev_second if is_second else live.ev
    ev.is_charging = True
    ev.is_connected = True
    ev.power_w = 3705.0
    ev.force_max_discharge_power = True
    ev.max_discharge_power_w = 5000
    if is_second:
        live.ev_second_planned_load_smart_charging_enabled = managed
    else:
        live.ev_planned_load_smart_charging_enabled = managed
    return live


def _recommendation() -> HourlyRecommendation:
    """Return the reported 0.054 kWh discharge in a 15-minute slot."""
    return HourlyRecommendation(
        start=_NOW,
        end=_NOW + timedelta(minutes=15),
        recommendation=Recommendations.BatteriesDischargeMode.value,
        avg_house_consumption_kwh=0.054,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=0.0,
        batteries_discharged_kwh=0.054,
        estimated_battery_capacity_kwh=5.0,
        estimated_battery_soc_pct=55.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.054,
        export_price=0.0,
        grid_export_kwh=0.0,
        grid_import_kwh=0.0,
        import_price=1.0,
        solcast_pv_estimate_kwh=0.0,
        ev_charger_calculated_power=0.0,
        ev_second_charger_calculated_power=0.0,
    )


async def _verifier(
    entity_id: str,
    desired: Any,
    writer: Any,
    reader: Any,
    **_kwargs: Any,
) -> ApplyResult:
    """Record the desired value without touching Home Assistant."""
    return ApplyResult(
        entity_id=entity_id,
        desired=desired,
        actual=desired,
        status=ApplyStatus.OK,
        attempts=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_second", "managed", "expected_cap_w"),
    ((False, False, 216), (False, True, 0), (True, False, 216), (True, True, 0)),
)
async def test_only_managed_ocpp_draw_reserves_phase_headroom(
    is_second: bool,
    managed: bool,
    expected_cap_w: int,
) -> None:
    """External EV draw preserves house discharge; managed ramp-down reserves it."""
    rec = _recommendation()
    commands = PhaseAwareChargeCommands(
        recommendation=rec,
        primary_grid_charge_power_w=None,
    )
    with (
        patch(_MODULE + ".async_write_and_verify", side_effect=_verifier),
        patch(
            _MODULE + ".build_phase_aware_charge_commands",
            return_value=commands,
        ),
    ):
        summary: CycleApplySummary = await async_apply_battery_settings(
            MagicMock(),
            _config(is_second=is_second, managed=managed),
            _live(is_second=is_second, managed=managed),
            rec,
            0.0,
        )

    cap_write = next(
        result for result in summary.results if result.entity_id == _DISCHARGE_ENTITY
    )
    assert cap_write.desired == expected_cap_w


@pytest.mark.asyncio
async def test_second_ocpp_requires_the_primary_master_switch() -> None:
    """EV2 is externally controlled when the OCPP master switch is off."""
    cfg = _config(is_second=True, managed=True)
    cfg.ocpp_enabled = False
    live = _live(is_second=True, managed=True)
    rec = _recommendation()
    commands = PhaseAwareChargeCommands(
        recommendation=rec,
        primary_grid_charge_power_w=None,
    )
    with (
        patch(_MODULE + ".async_write_and_verify", side_effect=_verifier),
        patch(
            _MODULE + ".build_phase_aware_charge_commands",
            return_value=commands,
        ),
    ):
        summary = await async_apply_battery_settings(MagicMock(), cfg, live, rec, 0.0)

    cap_write = next(
        result for result in summary.results if result.entity_id == _DISCHARGE_ENTITY
    )
    assert cap_write.desired == 216

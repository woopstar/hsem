"""Tests for the battery applier's write ladder and its abort conditions.

``async_apply_battery_settings`` issues up to five hardware writes in a fixed
order: discharge cap, grid-charge cap, excess-PV mode, TOU periods, working
mode. Every step is fail-closed — a failed write or a missing entity stops the
cycle there rather than leaving the inverter half-configured, e.g. in
``FullyFedToGrid`` with a stale TOU schedule. Each test drives the ladder to
one step and checks it stops.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.const import (
    DEFAULT_HSEM_BATTERIES_WAIT_MODE,
    DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE,
)
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
_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

_DISCHARGE_ENTITY = "number.maxdis"
_GRID_CHARGE_ENTITY = "number.gridcharge"
_EXCESS_ENTITY = "select.excess"
_TOU_ENTITY = "sensor.tou"
_MODE_ENTITY = "select.wm"


def _sensor() -> MagicMock:
    """Return a stand-in working-mode sensor with the applier's collaborators."""
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


def _rec(
    recommendation: str | None,
    *,
    discharged_kwh: float = 1.5,
    charged_kwh: float = 0.0,
    ev_power_w: float = 0.0,
) -> HourlyRecommendation:
    """Return a one-hour recommendation slot."""
    zero = 0.0
    return HourlyRecommendation(
        start=_NOW,
        end=_NOW + timedelta(hours=1),
        recommendation=recommendation,
        avg_house_consumption_kwh=0.5,
        avg_house_consumption_1d_kwh=zero,
        avg_house_consumption_3d_kwh=zero,
        avg_house_consumption_7d_kwh=zero,
        avg_house_consumption_14d_kwh=zero,
        batteries_charged_kwh=charged_kwh,
        batteries_discharged_kwh=discharged_kwh,
        estimated_battery_capacity_kwh=1.0,
        estimated_battery_soc_pct=50.0,
        estimated_cost_currency=zero,
        estimated_net_consumption_kwh=0.5,
        export_price=0.05,
        grid_export_kwh=zero,
        grid_import_kwh=zero,
        import_price=0.20,
        solcast_pv_estimate_kwh=zero,
        ev_charger_calculated_power=ev_power_w,
    )


def _verifier(fail_on: str | None = None) -> Any:
    """Return an ``async_write_and_verify`` stub failing on *fail_on*."""

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
            sensor or _sensor(),
            cfg,
            live,
            rec,
            0.0,
            wait_mode_reserve_kwh=None,
        )


def _written(summary: CycleApplySummary) -> list[str]:
    """Return the entity IDs the applier attempted, in order."""
    return [r.entity_id for r in summary.results]


class TestDischargeCapStep:
    """The discharge cap is the first write and gates everything after it."""

    @pytest.mark.asyncio
    async def test_an_unconfigured_uncapped_write_is_skipped_silently(self) -> None:
        """The historical rated-max write needs its entity to exist at all."""
        cfg = _cfg()
        cfg.huawei_solar_batteries_maximum_discharging_power = None

        summary = await _apply(
            cfg, _live(), _rec(Recommendations.BatteriesDischargeMode.value)
        )

        # No cap condition applies, so there is nothing to enforce and the
        # cycle stops before the working-mode write.
        assert _written(summary) == []

    @pytest.mark.asyncio
    async def test_a_failed_cap_write_blocks_the_rest_of_the_cycle(self) -> None:
        """A cap that did not land must not be followed by a mode change."""
        summary = await _apply(
            _cfg(),
            _live(),
            _rec(Recommendations.BatteriesDischargeMode.value),
            fail_on=_DISCHARGE_ENTITY,
        )

        assert _written(summary) == [_DISCHARGE_ENTITY]
        assert summary.overall_status is ApplyStatus.FAILED

    @pytest.mark.asyncio
    async def test_a_live_ev_draw_reserves_phase_headroom_from_the_cap(self) -> None:
        """An EV that has not ramped down yet eats the whole discharge cap."""
        live = _live()
        # Start at the rated cap so the reservation-driven 0 W is a real change.
        live.huawei_batteries_max_discharge_power_w = 2500
        live.ev.is_charging = True
        live.ev.is_connected = True
        live.ev.power_w = 7400.0
        live.ev.force_max_discharge_power = True
        live.ev.max_discharge_power_w = 5000

        with patch(f"{_MODULE}._LOGGER") as logger:
            summary = await _apply(
                _cfg(),
                live,
                # Planned EV power 0 W while the charger still draws 7.4 kW.
                _rec(Recommendations.BatteriesDischargeMode.value, ev_power_w=0.0),
            )

        cap_write = next(r for r in summary.results if r.entity_id == _DISCHARGE_ENTITY)
        assert cap_write.desired == 0
        assert any(
            "phase-headroom reservation reduced cap" in str(call.args[0])
            for call in logger.debug.call_args_list
        )


class TestWorkingModeSelection:
    """Each recommendation maps to exactly one inverter working mode."""

    @pytest.mark.asyncio
    async def test_force_export_selects_fully_fed_to_grid(self) -> None:
        """An export slot sells everything rather than self-consuming."""
        summary = await _apply(_cfg(), _live(), _rec(Recommendations.ForceExport.value))

        mode_write = next(r for r in summary.results if r.entity_id == _MODE_ENTITY)
        assert mode_write.desired == WorkingModes.FullyFedToGrid.value

    @pytest.mark.asyncio
    async def test_an_unrecognised_recommendation_writes_nothing(self) -> None:
        """An unknown label is not guessed at — the cycle ends."""
        live = _live()
        # Match the rated cap so the discharge step is a no-op.
        live.huawei_batteries_max_discharge_power_w = 2500

        summary = await _apply(_cfg(), live, _rec("some_future_mode"))

        assert _written(summary) == []

    @pytest.mark.asyncio
    async def test_a_failed_working_mode_write_is_the_last_thing_tried(self) -> None:
        """The working-mode write is the final step of the ladder."""
        summary = await _apply(
            _cfg(),
            _live(),
            _rec(Recommendations.BatteriesDischargeMode.value),
            fail_on=_MODE_ENTITY,
        )

        assert _written(summary)[-1] == _MODE_ENTITY
        assert summary.overall_status is ApplyStatus.FAILED


class TestGridChargeStep:
    """The phase-aware grid-charge cap protects the main fuse."""

    @pytest.mark.asyncio
    async def test_a_failed_grid_charge_write_blocks_the_rest_of_the_cycle(
        self,
    ) -> None:
        """Charging must not be enabled at an unverified power limit."""
        summary = await _apply(
            _cfg(),
            _live(),
            _rec(Recommendations.BatteriesChargeGrid.value, charged_kwh=3.0),
            fail_on=_GRID_CHARGE_ENTITY,
            grid_charge_w=3000.0,
        )

        assert _written(summary)[-1] == _GRID_CHARGE_ENTITY
        assert summary.overall_status is ApplyStatus.FAILED

    @pytest.mark.asyncio
    async def test_an_unconfigured_grid_charge_entity_blocks_the_cycle(self) -> None:
        """Without the cap entity the limiter has nothing to enforce with."""
        cfg = _cfg()
        cfg.huawei_solar_batteries_grid_charge_maximum_power = None
        live = _live()
        live.huawei_batteries_max_discharge_power_w = 2500

        summary = await _apply(
            cfg,
            live,
            _rec(Recommendations.BatteriesChargeGrid.value, charged_kwh=3.0),
            grid_charge_w=3000.0,
        )

        assert _written(summary) == []


class TestExcessPvStep:
    """The excess-PV mode decides whether surplus is sold or stored."""

    @pytest.mark.asyncio
    async def test_a_failed_excess_pv_write_blocks_the_rest_of_the_cycle(self) -> None:
        """A stale fed-to-grid setting must not outlive a failed correction."""
        summary = await _apply(
            _cfg(),
            _live(),
            _rec(Recommendations.BatteriesDischargeMode.value),
            fail_on=_EXCESS_ENTITY,
        )

        assert _written(summary)[-1] == _EXCESS_ENTITY
        assert summary.overall_status is ApplyStatus.FAILED


class TestTouStep:
    """TOU periods are written per battery device and verified by re-read."""

    @pytest.mark.asyncio
    async def test_a_failed_tou_write_blocks_the_working_mode_write(self) -> None:
        """TimeOfUse must never be selected on top of a stale schedule."""
        summary = await _apply(
            _cfg(),
            _live(),
            _rec(Recommendations.BatteriesChargeGrid.value, charged_kwh=3.0),
            fail_on=f"{_TOU_ENTITY}:",
        )

        assert _written(summary)[-1] == f"{_TOU_ENTITY}:bat1"
        assert summary.overall_status is ApplyStatus.FAILED

    @pytest.mark.asyncio
    async def test_a_missing_battery_device_id_blocks_the_cycle(self) -> None:
        """Without a device ID there is no service target for the schedule."""
        cfg = _cfg()
        cfg.huawei_solar_device_id_batteries = None
        live = _live()
        live.huawei_batteries_max_discharge_power_w = 2500
        live.huawei_batteries_excess_pv_use_in_tou = "charge"

        summary = await _apply(
            cfg, live, _rec(Recommendations.BatteriesChargeGrid.value, charged_kwh=3.0)
        )

        assert _written(summary) == []

    @pytest.mark.asyncio
    async def test_a_grid_charge_slot_writes_the_force_charge_schedule(self) -> None:
        """The whole day is a charge window while charging from the grid."""
        summary = await _apply(
            _cfg(),
            _live(),
            _rec(Recommendations.BatteriesChargeGrid.value, charged_kwh=3.0),
        )

        tou_write = next(
            r for r in summary.results if r.entity_id.startswith(_TOU_ENTITY)
        )
        assert tou_write.desired == list(DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE)

    @pytest.mark.asyncio
    async def test_a_wait_slot_writes_the_wait_schedule(self) -> None:
        """An unheld wait slot keeps the battery idle via a 1-minute TOU window."""
        summary = await _apply(
            _cfg(),
            _live(),
            _rec(Recommendations.BatteriesWaitMode.value, discharged_kwh=1.0),
        )

        tou_write = next(
            r for r in summary.results if r.entity_id.startswith(_TOU_ENTITY)
        )
        assert tou_write.desired == list(DEFAULT_HSEM_BATTERIES_WAIT_MODE)

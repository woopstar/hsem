"""The applier never commands more discharge power than the entity accepts.

The Huawei ``maximum_discharging_power`` number entity takes its ``max`` from
what the hardware reports. HSEM derived the uncapped limit from the rated
capacity alone (10 kWh -> 5000 W), so on an installation whose entity stops at
4400 W Home Assistant rejected every write, the applier reported ``failed``
and the entity stayed on the last lower cap (issue #1291).

The uncapped limit is now bounded by the entity's ``max``, and every cap
derived from it (EV, wait-mode reserve, forcible discharge) inherits the bound.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.custom_sensors.applier import (
    async_apply_battery_settings,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import ApplyResult, ApplyStatus
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.wait_mode_behavior import WaitModeBehavior
from custom_components.hsem.utils.workingmodes import WorkingModes

_APPLIER = "custom_components.hsem.custom_sensors.applier"
_FORCIBLE = "custom_components.hsem.custom_sensors.applier_forcible_discharge"
_DISCHARGE_ENTITY = "number.maxdis"
_NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)

#: The reported installation: a 10 kWh battery whose entity accepts 4400 W.
_RATED_WH = 10000
_ENTITY_MAX_W = 4400.0
#: What the capacity table gives for 10 kWh.
_TABLE_W = 5000


def _sensor() -> MagicMock:
    sensor = MagicMock()
    sensor.hass = MagicMock()
    sensor._ev_zero_discharge_ceiling_warned = set()
    return sensor


def _cfg() -> SensorConfig:
    cfg = SensorConfig()
    cfg.read_only = False
    cfg.huawei_solar_batteries_working_mode = "select.wm"
    cfg.huawei_solar_batteries_maximum_discharging_power = _DISCHARGE_ENTITY
    cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou = "select.excess"
    cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = "sensor.tou"
    cfg.huawei_solar_device_id_batteries = "bat1"
    return cfg


def _live(*, entity_max_w: float | None, commanded_w: float = 2000.0) -> LiveState:
    """Return the live state of the report: the entity sits on a 2000 W cap."""
    live = LiveState()
    live._degraded_mode = DegradedMode.OK
    live.battery_current_capacity_kwh = 5.0
    live.battery_usable_capacity_kwh = 10.0
    live.huawei_batteries_rated_capacity_wh = _RATED_WH
    live.huawei_batteries_max_discharge_power_w = commanded_w
    live.huawei_batteries_max_discharge_power_limit_w = entity_max_w
    live.huawei_batteries_working_mode = WorkingModes.MaximizeSelfConsumption.value
    live.huawei_batteries_excess_pv_use_in_tou = "charge"
    return live


def _rec(recommendation: str, *, discharged_kwh: float = 1.0) -> HourlyRecommendation:
    """Return a one-hour slot with the given label and planned discharge."""
    return HourlyRecommendation(
        start=_NOW,
        end=_NOW + timedelta(hours=1),
        recommendation=recommendation,
        avg_house_consumption_kwh=0.5,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=0.0,
        batteries_discharged_kwh=discharged_kwh,
        estimated_battery_capacity_kwh=1.0,
        estimated_battery_soc_pct=50.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.5,
        export_price=0.05,
        grid_export_kwh=0.0,
        grid_import_kwh=0.0,
        import_price=0.20,
        solcast_pv_estimate_kwh=0.0,
    )


async def _apply(
    cfg: SensorConfig,
    live: LiveState,
    rec: HourlyRecommendation,
    *,
    wait_mode_reserve_kwh: float | None = None,
) -> tuple[list[float], AsyncMock]:
    """Run one apply cycle.

    Returns:
        The values written to the discharge-power entity, and the mock of the
        forcible-discharge service call.
    """
    discharge_writes: list[float] = []

    async def _write_and_verify(
        entity_id: str, desired: Any, writer: Any, reader: Any, **kwargs: Any
    ) -> ApplyResult:
        await writer()
        if entity_id == _DISCHARGE_ENTITY:
            discharge_writes.append(desired)
        return ApplyResult(
            entity_id=entity_id,
            desired=desired,
            actual=desired,
            status=ApplyStatus.OK,
            attempts=1,
        )

    with (
        patch(f"{_APPLIER}.async_write_and_verify", side_effect=_write_and_verify),
        patch(f"{_APPLIER}.async_set_select_option", new_callable=AsyncMock),
        patch(f"{_APPLIER}.async_set_number_value", new_callable=AsyncMock),
        patch(f"{_FORCIBLE}.async_write_and_verify", side_effect=_write_and_verify),
        patch(
            f"{_FORCIBLE}.async_set_forcible_discharge", new_callable=AsyncMock
        ) as mock_forcible,
    ):
        await async_apply_battery_settings(
            _sensor(), cfg, live, rec, 0.0, wait_mode_reserve_kwh=wait_mode_reserve_kwh
        )
    return discharge_writes, mock_forcible


class TestUncappedLimit:
    """A plain discharge slot writes the highest value the entity accepts."""

    @pytest.mark.asyncio
    async def test_entity_maximum_below_the_table_is_written(self) -> None:
        """The reported case: 4400 W is written instead of a rejected 5000 W."""
        writes, _ = await _apply(
            _cfg(),
            _live(entity_max_w=_ENTITY_MAX_W),
            _rec(Recommendations.BatteriesDischargeMode.value),
        )

        assert writes == pytest.approx([_ENTITY_MAX_W])

    @pytest.mark.asyncio
    async def test_entity_already_at_its_maximum_is_not_rewritten(self) -> None:
        """Once the cap is lifted, the next cycle has nothing left to write."""
        writes, _ = await _apply(
            _cfg(),
            _live(entity_max_w=_ENTITY_MAX_W, commanded_w=_ENTITY_MAX_W),
            _rec(Recommendations.BatteriesDischargeMode.value),
        )

        assert writes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entity_max_w", [None, 0.0, 5000.0, 6000.0])
    async def test_unusable_or_higher_entity_maximum_keeps_the_table_value(
        self, entity_max_w: float | None
    ) -> None:
        writes, _ = await _apply(
            _cfg(),
            _live(entity_max_w=entity_max_w),
            _rec(Recommendations.BatteriesDischargeMode.value),
        )

        assert writes == pytest.approx([_TABLE_W])


class TestDerivedCapsStayWithinTheEntityMaximum:
    """Every cap that starts from the uncapped limit inherits the bound."""

    @pytest.mark.asyncio
    async def test_ev_cap(self) -> None:
        """A planned 6 kW discharge beside an opted-in EV stops at 4400 W."""
        live = _live(entity_max_w=_ENTITY_MAX_W)
        live.ev.is_charging = True
        live.ev.is_connected = True
        live.ev.force_max_discharge_power = True
        live.ev.max_discharge_power_w = 6000

        writes, _ = await _apply(
            _cfg(),
            live,
            _rec(Recommendations.BatteriesDischargeMode.value, discharged_kwh=6.0),
        )

        assert writes == pytest.approx([_ENTITY_MAX_W])

    @pytest.mark.asyncio
    async def test_wait_mode_reserve_cap(self) -> None:
        """Self-consumption above the reserve runs at the bounded limit."""
        cfg = _cfg()
        cfg.batteries_wait_mode_behavior = WaitModeBehavior.SelfConsumptionWithReserve

        writes, _ = await _apply(
            cfg,
            _live(entity_max_w=_ENTITY_MAX_W),
            _rec(Recommendations.BatteriesWaitMode.value, discharged_kwh=0.0),
            wait_mode_reserve_kwh=1.0,
        )

        assert writes == pytest.approx([_ENTITY_MAX_W])

    @pytest.mark.asyncio
    async def test_forcible_discharge_power(self) -> None:
        """The forcible-discharge command carries the bounded power."""
        cfg = _cfg()
        cfg.huawei_solar_batteries_forcible_charge = "sensor.batteries_forcible_charge"

        writes, mock_forcible = await _apply(
            cfg,
            _live(entity_max_w=_ENTITY_MAX_W),
            _rec(Recommendations.ForceBatteriesDischarge.value),
        )

        assert writes == pytest.approx([_ENTITY_MAX_W])
        mock_forcible.assert_awaited_once()
        assert mock_forcible.await_args is not None
        assert mock_forcible.await_args.args[3] == pytest.approx(_ENTITY_MAX_W)

"""Regression tests for the batteries_discharge_window_mode recommendation (issue #1005).

A slot inside a configured discharge window that the MILP solved with zero
discharge should be published as ``batteries_discharge_window_mode`` rather
than ``batteries_discharge_mode``.  The inverter execution profile is
identical (MaximizeSelfConsumption with the same discharge-ceiling rules),
but the dashboard no longer claims the battery is actively discharging.

Covers:
- ``apply_optimization_strategy`` assigns the new mode to seasonal discharge-window slots.
- ``simulate_soc`` promotes it to ``batteries_discharge_mode`` when the battery
  actually discharges, and preserves it when the solved discharge is zero.
- ``async_apply_battery_settings`` maps the new mode to ``MaximizeSelfConsumption``.
- ``applier_caps._primary_battery_cap_hold`` exempts the new mode from a 0 W cap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.const import DEFAULT_HSEM_BATTERIES_WAIT_MODE
from custom_components.hsem.custom_sensors.applier import async_apply_battery_settings
from custom_components.hsem.custom_sensors.applier_caps import (
    _primary_battery_cap_hold,
    _primary_battery_hold,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner.discharge_scheduler import (
    apply_optimization_strategy,
)
from custom_components.hsem.planner.soc_simulation import simulate_soc
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import ApplyResult, ApplyStatus
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import DISCHARGE_RECS, Recommendations
from custom_components.hsem.utils.workingmodes import WorkingModes

_LOGGER_PATCH = "custom_components.hsem.utils.logger.HSEM_LOGGER.debug"
_NOW = datetime(2026, 9, 12, 19, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_planned_slot(
    *,
    recommendation: str | None = None,
    net_consumption_kwh: float = 0.8,
    charged_kwh: float = 0.0,
    discharged_kwh: float = 0.0,
    month: int = 9,
) -> PlannedSlot:
    """Create a single future PlannedSlot for scheduler/simulation tests."""
    start = datetime(2026, month, 12, 19, 0, tzinfo=UTC)
    return PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=1.95, export_price=0.05),
        recommendation=recommendation,
        estimated_net_consumption_kwh=net_consumption_kwh,
        avg_house_consumption_kwh=max(net_consumption_kwh, 0.0),
        solcast_pv_estimate_kwh=0.0
        if net_consumption_kwh >= 0
        else abs(net_consumption_kwh),
        batteries_charged_kwh=charged_kwh,
        batteries_discharged_kwh=discharged_kwh,
    )


def _sensor() -> MagicMock:
    sensor = MagicMock()
    sensor.hass = MagicMock()
    return sensor


def _cfg() -> SensorConfig:
    cfg = SensorConfig()
    cfg.read_only = False
    cfg.batteries_wait_mode_behavior = "strict"
    cfg.huawei_solar_batteries_working_mode = "select.wm"
    cfg.huawei_solar_batteries_maximum_discharging_power = "number.maxdis"
    cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou = "select.excess"
    cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = "sensor.tou"
    cfg.huawei_solar_device_id_batteries = "bat1"
    return cfg


def _live(*, max_discharge_power_w: int = 2500) -> LiveState:
    live = LiveState()
    live._degraded_mode = DegradedMode.OK
    live.battery_current_capacity_kwh = 3.0
    live.huawei_batteries_rated_capacity_wh = 5000
    live.huawei_batteries_max_discharge_power_w = max_discharge_power_w
    live.huawei_batteries_working_mode = WorkingModes.MaximizeSelfConsumption.value
    live.huawei_batteries_excess_pv_use_in_tou = "charge"
    live.tou_periods.periods = list(DEFAULT_HSEM_BATTERIES_WAIT_MODE)
    return live


def _rec(
    *,
    recommendation: str = Recommendations.BatteriesDischargeWindowMode.value,
    discharged_kwh: float = 0.0,
) -> HourlyRecommendation:
    """Build an HourlyRecommendation for applier tests."""
    return HourlyRecommendation(
        start=_NOW,
        end=_NOW + timedelta(hours=1),
        recommendation=recommendation,
        avg_house_consumption_kwh=0.8,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=0.0,
        batteries_discharged_kwh=discharged_kwh,
        estimated_battery_capacity_kwh=3.0,
        estimated_battery_soc_pct=60.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.8,
        export_price=0.05,
        grid_export_kwh=0.0,
        grid_import_kwh=0.8,
        import_price=1.95,
        solcast_pv_estimate_kwh=0.0,
    )


async def _write_and_verify_ok(entity_id, desired, writer, reader, **kwargs):  # type: ignore[no-untyped-def]
    await writer()
    return ApplyResult(
        entity_id=entity_id,
        desired=desired,
        actual=desired,
        status=ApplyStatus.OK,
        attempts=1,
    )


def _apply_patches():
    return (
        patch(_LOGGER_PATCH, new_callable=MagicMock),
        patch(
            "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
            side_effect=_write_and_verify_ok,
        ),
        patch(
            "custom_components.hsem.custom_sensors.applier.async_set_select_option",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.hsem.custom_sensors.applier.async_set_number_value",
            new_callable=AsyncMock,
        ),
    )


async def _apply(
    cfg: SensorConfig,
    live: LiveState,
    rec: HourlyRecommendation,
) -> AsyncMock:
    sensor = _sensor()
    logger_p, verify_p, select_p, number_p = _apply_patches()
    with logger_p, verify_p, select_p, number_p as patched:
        await async_apply_battery_settings(sensor, cfg, live, rec, 0.0)
    mock_number: AsyncMock = patched
    return mock_number


def _discharge_cap_writes(mock_number: AsyncMock) -> list[int]:
    return [
        call.args[2]
        for call in mock_number.await_args_list
        if call.args[1] == "number.maxdis"
    ]


# ---------------------------------------------------------------------------
# Scheduler assignment
# ---------------------------------------------------------------------------


class TestDischargeSchedulerAssignment:
    """apply_optimization_strategy labels idle discharge-window slots correctly."""

    def test_summer_non_pv_slot_gets_discharge_window_mode(self) -> None:
        slot = _make_planned_slot(recommendation=None, net_consumption_kwh=0.8, month=6)
        apply_optimization_strategy(
            [slot],
            now=_NOW,
            current_capacity=5.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
        )
        assert slot.recommendation == Recommendations.BatteriesDischargeWindowMode.value
        assert slot.recommendation in DISCHARGE_RECS

    def test_winter_non_pv_slot_stays_wait(self) -> None:
        slot = _make_planned_slot(recommendation=None, net_consumption_kwh=0.8, month=1)
        apply_optimization_strategy(
            [slot],
            now=_NOW,
            current_capacity=5.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
        )
        assert slot.recommendation == Recommendations.BatteriesWaitMode.value

    def test_summer_pv_surplus_slot_gets_solar_charge(self) -> None:
        slot = _make_planned_slot(
            recommendation=None, net_consumption_kwh=-0.8, month=6
        )
        apply_optimization_strategy(
            [slot],
            now=_NOW,
            current_capacity=5.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
        )
        assert slot.recommendation == Recommendations.BatteriesChargeSolar.value


# ---------------------------------------------------------------------------
# SoC simulation promotion / preservation
# ---------------------------------------------------------------------------


class TestSocSimulationWindowMode:
    """simulate_soc promotes the label on active discharge and preserves it on hold."""

    def test_window_mode_with_actual_discharge_promoted_to_active(self) -> None:
        slot = _make_planned_slot(
            recommendation=Recommendations.BatteriesDischargeWindowMode.value,
            net_consumption_kwh=0.8,
        )
        simulate_soc(
            [slot],
            now=_NOW,
            current_kwh=5.0,
            usable_kwh=9.0,
            max_capacity_kwh=9.0,
            max_charge_per_slot=5.0,
            max_discharge_per_slot=None,
        )
        assert slot.recommendation == Recommendations.BatteriesDischargeMode.value
        assert slot.batteries_discharged_kwh > 1e-9

    def test_window_mode_with_zero_solved_discharge_is_preserved(self) -> None:
        slot = _make_planned_slot(
            recommendation=Recommendations.BatteriesDischargeWindowMode.value,
            net_consumption_kwh=0.8,
            discharged_kwh=0.0,
        )
        simulate_soc(
            [slot],
            now=_NOW,
            current_kwh=5.0,
            usable_kwh=9.0,
            max_capacity_kwh=9.0,
            max_charge_per_slot=5.0,
            max_discharge_per_slot=None,
            milp_prepopulated=True,
        )
        assert slot.recommendation == Recommendations.BatteriesDischargeWindowMode.value
        assert slot.batteries_discharged_kwh <= 1e-9


class TestSocSimulationDemotion:
    """simulate_soc demotes an undispatched discharge label (issue #1026).

    The relabel must work in both directions: a discharge label is only valid
    while the slot actually dispatches the battery, mirroring the
    charge-direction rule from issue #989.
    """

    def _simulate(
        self,
        slot: PlannedSlot,
        *,
        current_kwh: float,
        milp_prepopulated: bool = False,
    ) -> None:
        simulate_soc(
            [slot],
            now=_NOW,
            current_kwh=current_kwh,
            usable_kwh=9.0,
            max_capacity_kwh=9.0,
            max_charge_per_slot=5.0,
            max_discharge_per_slot=None,
            milp_prepopulated=milp_prepopulated,
        )

    def test_milp_prepopulated_zero_discharge_is_demoted(self) -> None:
        """The #1026 case: the MILP labelled the slot, the plan dispatches nothing."""
        slot = _make_planned_slot(
            recommendation=Recommendations.BatteriesDischargeMode.value,
            net_consumption_kwh=0.8,
            discharged_kwh=0.0,
        )
        self._simulate(slot, current_kwh=0.0, milp_prepopulated=True)
        assert slot.recommendation == Recommendations.BatteriesDischargeWindowMode.value

    def test_rederived_discharge_is_never_demoted(self) -> None:
        """Without the LP flag, ``discharge`` is re-derived and is not plan intent.

        A slot whose plan intended arbitrage export has ``net_demand <= 0``, so
        the re-derivation yields 0 even though the LP planned a real discharge.
        Demoting there would destroy a legitimate label, so the demotion is
        gated on ``milp_prepopulated``.
        """
        slot = _make_planned_slot(
            recommendation=Recommendations.BatteriesDischargeMode.value,
            net_consumption_kwh=-0.5,
        )
        self._simulate(slot, current_kwh=0.0)
        assert slot.recommendation == Recommendations.BatteriesDischargeMode.value

    def test_discharge_mode_with_real_discharge_keeps_its_label(self) -> None:
        slot = _make_planned_slot(
            recommendation=Recommendations.BatteriesDischargeMode.value,
            net_consumption_kwh=0.8,
            discharged_kwh=0.5,
        )
        self._simulate(slot, current_kwh=5.0, milp_prepopulated=True)
        assert slot.recommendation == Recommendations.BatteriesDischargeMode.value
        assert slot.batteries_discharged_kwh > 1e-9

    def test_demotion_target_is_not_wait_mode(self) -> None:
        """Wait would suppress the self-consumption a discharge window permits."""
        slot = _make_planned_slot(
            recommendation=Recommendations.BatteriesDischargeMode.value,
            net_consumption_kwh=0.8,
            discharged_kwh=0.0,
        )
        self._simulate(slot, current_kwh=0.0, milp_prepopulated=True)
        assert slot.recommendation != Recommendations.BatteriesWaitMode.value
        assert slot.recommendation in DISCHARGE_RECS

    def test_force_labels_still_demote_to_wait_mode(self) -> None:
        """The pre-existing forced-action demotion is unchanged."""
        for forced in (
            Recommendations.ForceBatteriesDischarge.value,
            Recommendations.ForceExport.value,
        ):
            slot = _make_planned_slot(recommendation=forced, net_consumption_kwh=0.8)
            self._simulate(slot, current_kwh=0.0)
            assert slot.recommendation == Recommendations.BatteriesWaitMode.value

    def test_relabel_is_idempotent_across_repeated_simulation(self) -> None:
        """The two branches are mutually exclusive, so labels must not oscillate."""
        slot = _make_planned_slot(
            recommendation=Recommendations.BatteriesDischargeMode.value,
            net_consumption_kwh=0.8,
        )
        seen = []
        for _ in range(3):
            self._simulate(slot, current_kwh=0.0, milp_prepopulated=True)
            seen.append(slot.recommendation)
        assert seen == [Recommendations.BatteriesDischargeWindowMode.value] * 3

    def test_demotion_does_not_disturb_energy_fields(self) -> None:
        """Only the label changes — the LP's solved energy flows are preserved."""
        slot = _make_planned_slot(
            recommendation=Recommendations.BatteriesDischargeMode.value,
            net_consumption_kwh=0.8,
        )
        slot.grid_import_kwh = 0.8
        slot.grid_export_kwh = 0.0
        self._simulate(slot, current_kwh=0.0, milp_prepopulated=True)
        assert slot.recommendation == Recommendations.BatteriesDischargeWindowMode.value
        assert slot.batteries_discharged_kwh == pytest.approx(0.0)
        assert slot.batteries_charged_kwh == pytest.approx(0.0)
        assert slot.grid_import_kwh == pytest.approx(0.8)
        assert slot.grid_export_kwh == pytest.approx(0.0)

    def test_soc_stays_within_bounds_after_demotion(self) -> None:
        slot = _make_planned_slot(
            recommendation=Recommendations.BatteriesDischargeMode.value,
            net_consumption_kwh=0.8,
        )
        self._simulate(slot, current_kwh=0.0, milp_prepopulated=True)
        assert 0.0 <= slot.estimated_battery_soc_pct <= 100.0


# ---------------------------------------------------------------------------
# Applier mapping and cap hold
# ---------------------------------------------------------------------------


class TestApplierWindowModeMapping:
    """The applier treats batteries_discharge_window_mode like batteries_discharge_mode."""

    @pytest.mark.asyncio
    async def test_window_mode_maps_to_maximize_self_consumption(self) -> None:
        sensor = _sensor()
        live = _live(max_discharge_power_w=2500)
        live.huawei_batteries_working_mode = WorkingModes.TimeOfUse.value
        logger_p, verify_p, select_p, number_p = _apply_patches()
        with logger_p, verify_p, select_p as mock_select, number_p:
            await async_apply_battery_settings(sensor, _cfg(), live, _rec(), 0.0)
        mock_select.assert_any_await(
            sensor, "select.wm", WorkingModes.MaximizeSelfConsumption.value
        )

    @pytest.mark.asyncio
    async def test_window_mode_near_zero_discharge_does_not_write_zero_cap(
        self,
    ) -> None:
        live = _live(max_discharge_power_w=2500)
        mock_number = await _apply(_cfg(), live, _rec(discharged_kwh=0.001))
        assert _discharge_cap_writes(mock_number) == []

    @pytest.mark.asyncio
    async def test_window_mode_zero_cap_hardware_restored_to_rated_max(self) -> None:
        live = _live(max_discharge_power_w=0)
        mock_number = await _apply(_cfg(), live, _rec())
        assert _discharge_cap_writes(mock_number) == [2500]


class TestPrimaryBatteryCapHoldWindowMode:
    """_primary_battery_cap_hold exempts the new window label."""

    def test_window_mode_with_near_zero_energy_is_not_a_cap_hold(self) -> None:
        rec = _rec()
        assert _primary_battery_hold(rec) is True
        assert _primary_battery_cap_hold(rec) is False

    def test_active_discharge_mode_still_exempt(self) -> None:
        rec = _rec(recommendation=Recommendations.BatteriesDischargeMode.value)
        assert _primary_battery_cap_hold(rec) is False

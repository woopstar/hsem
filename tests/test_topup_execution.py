"""Sub-slot planned battery energy is executed proportionally (issue #1364).

On 2026-11-03 a planned 1.02 / 0.05 / 0.28 kWh sequence ran as +1.16 / −0.51 /
+1.09 kWh because a ``batteries_charge_grid`` slot charged at rated power for
the whole slot, a ``batteries_discharge_mode`` slot served the whole house load
whatever its planned 0.053 kWh, and window hysteresis held the charge label
after the next solve planned nothing.

Covers:
- ``topup_charge_power_w`` — slot-average rate, floor, rated restore
- ``async_apply_battery_settings`` — the charging-power write and its gates
- ``apply_executable_discharge_minimum`` — the live-slot discharge rule
- ``_resolve_max_charge_power_w`` — the planner plans against the entity's
  ``max`` attribute while the scaling owns the commanded value
- ``current_slot_held`` on the working-mode sensor
- The config option's validation and default
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator_builder import (
    _resolve_max_charge_power_w,
    build_planner_input,
)
from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.applier import async_apply_battery_settings
from custom_components.hsem.custom_sensors.applier_charge_power import (
    topup_charge_power_w,
)
from custom_components.hsem.custom_sensors.working_mode_attributes import (
    build_working_mode_attributes,
)
from custom_components.hsem.flows.batteries_wait_mode import (
    validate_batteries_wait_mode_input,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner.executable_minimum import (
    EXECUTABLE_DISCHARGE_FRACTION,
    apply_executable_discharge_minimum,
    deliverable_discharge_kwh,
    executable_discharge_minimum_kwh,
)
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import ApplyResult, ApplyStatus
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.workingmodes import WorkingModes

_NOW = datetime(2026, 11, 3, 14, 4, tzinfo=UTC)
_SLOT_START = datetime(2026, 11, 3, 14, 0, tzinfo=UTC)
_SLOT_END = _SLOT_START + timedelta(minutes=15)
_RATED_W = 5000.0
_CHARGE_ENTITY = "number.maxchg"
_LOGGER_PATCH = "custom_components.hsem.utils.logger.HSEM_LOGGER.debug"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _rec(
    recommendation: str = Recommendations.BatteriesChargeGrid.value,
    *,
    charged_kwh: float = 0.0,
    discharged_kwh: float = 0.0,
) -> HourlyRecommendation:
    return HourlyRecommendation(
        start=_SLOT_START,
        end=_SLOT_END,
        recommendation=recommendation,
        avg_house_consumption_kwh=0.4,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=charged_kwh,
        batteries_discharged_kwh=discharged_kwh,
        estimated_battery_capacity_kwh=7.5,
        estimated_battery_soc_pct=55.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.4,
        export_price=1.7,
        grid_export_kwh=0.0,
        grid_import_kwh=0.4,
        import_price=2.56,
        solcast_pv_estimate_kwh=0.0,
    )


def _cfg(
    *, topup_pct: float = 10.0, charge_entity: str | None = _CHARGE_ENTITY
) -> SensorConfig:
    cfg = SensorConfig()
    cfg.read_only = False
    cfg.batteries_wait_mode_behavior = "strict"
    cfg.batteries_topup_min_power_pct = topup_pct
    cfg.huawei_solar_batteries_working_mode = "select.wm"
    cfg.huawei_solar_batteries_maximum_discharging_power = "number.maxdis"
    cfg.huawei_solar_batteries_maximum_charging_power = charge_entity
    cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou = "select.excess"
    cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = "sensor.tou"
    cfg.huawei_solar_device_id_batteries = "bat1"
    return cfg


def _live(
    *, commanded_charge_w: float | None = _RATED_W, limit_w: float | None = _RATED_W
) -> LiveState:
    live = LiveState()
    live._degraded_mode = DegradedMode.OK
    live.battery_current_capacity_kwh = 7.5
    live.huawei_batteries_rated_capacity_wh = 15000
    live.huawei_batteries_max_discharge_power_w = 5000
    live.huawei_batteries_max_charge_power_w = commanded_charge_w
    live.huawei_batteries_max_charge_power_limit_w = limit_w
    live.huawei_batteries_working_mode = WorkingModes.TimeOfUse.value
    live.huawei_batteries_excess_pv_use_in_tou = "charge"
    return live


async def _write_and_verify_ok(entity_id, desired, writer, reader, **kwargs):  # type: ignore[no-untyped-def]  # local test shim mirrors async_write_and_verify signature
    await writer()
    return ApplyResult(
        entity_id=entity_id, desired=desired, actual=desired, status=ApplyStatus.OK
    )


async def _apply(
    cfg: SensorConfig, live: LiveState, rec: HourlyRecommendation
) -> tuple[AsyncMock, Any]:
    """Run one apply cycle; return the charge-power number mock and the summary."""
    sensor = MagicMock()
    sensor.hass = MagicMock()
    with (
        patch(_LOGGER_PATCH, new_callable=MagicMock),
        patch(
            "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
            side_effect=_write_and_verify_ok,
        ),
        patch(
            "custom_components.hsem.custom_sensors.applier_charge_power.async_write_and_verify",
            side_effect=_write_and_verify_ok,
        ),
        patch(
            "custom_components.hsem.custom_sensors.applier.async_set_select_option",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.hsem.custom_sensors.applier.async_set_tou_periods",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.hsem.custom_sensors.applier.async_set_number_value",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.hsem.custom_sensors.applier_charge_power.async_set_number_value",
            new_callable=AsyncMock,
        ) as charge_number,
    ):
        summary = await async_apply_battery_settings(sensor, cfg, live, rec, 0.0)
    return charge_number, summary


def _charge_power_writes(mock_number: AsyncMock) -> list[int]:
    return [
        call.args[2]
        for call in mock_number.await_args_list
        if call.args[1] == _CHARGE_ENTITY
    ]


# ---------------------------------------------------------------------------
# topup_charge_power_w
# ---------------------------------------------------------------------------


class TestTopupChargePowerW:
    """The live grid-charge slot runs at the slot-average rate of its plan."""

    def test_planned_energy_sets_the_slot_average_rate(self) -> None:
        rec = _rec(charged_kwh=0.284)
        assert topup_charge_power_w(rec, rated_w=_RATED_W, min_power_pct=10) == 1136

    def test_full_slot_plan_runs_at_rated_power(self) -> None:
        rec = _rec(charged_kwh=1.25)
        assert topup_charge_power_w(rec, rated_w=_RATED_W, min_power_pct=10) == 5000

    def test_more_than_rated_is_clamped(self) -> None:
        rec = _rec(charged_kwh=2.0)
        assert topup_charge_power_w(rec, rated_w=_RATED_W, min_power_pct=10) == 5000

    def test_tiny_plan_lands_on_the_floor(self) -> None:
        rec = _rec(charged_kwh=0.05)  # 200 W average, below the 10 % floor
        assert topup_charge_power_w(rec, rated_w=_RATED_W, min_power_pct=10) == 500

    def test_held_label_with_no_planned_energy_lands_on_the_floor(self) -> None:
        """Window hysteresis keeps the label while the solve planned 0 kWh."""
        rec = _rec(charged_kwh=0.0)
        assert topup_charge_power_w(rec, rated_w=_RATED_W, min_power_pct=10) == 500

    def test_zero_floor_with_no_planned_energy_is_zero(self) -> None:
        rec = _rec(charged_kwh=0.0)
        assert topup_charge_power_w(rec, rated_w=_RATED_W, min_power_pct=0) == 0

    @pytest.mark.parametrize(
        "recommendation",
        [
            Recommendations.BatteriesWaitMode.value,
            Recommendations.BatteriesDischargeMode.value,
            Recommendations.BatteriesChargeSolar.value,
            Recommendations.EVSmartCharging.value,
            Recommendations.ForceExport.value,
        ],
    )
    def test_other_labels_restore_the_rated_power(self, recommendation: str) -> None:
        rec = _rec(recommendation, charged_kwh=0.3)
        assert topup_charge_power_w(rec, rated_w=_RATED_W, min_power_pct=10) == 5000

    def test_floor_is_capped_at_the_rated_power(self) -> None:
        rec = _rec(charged_kwh=0.0)
        assert topup_charge_power_w(rec, rated_w=_RATED_W, min_power_pct=250) == 5000

    def test_zero_length_slot_runs_at_rated_power(self) -> None:
        rec = _rec(charged_kwh=0.3)
        rec.end = rec.start
        assert topup_charge_power_w(rec, rated_w=_RATED_W, min_power_pct=10) == 5000


# ---------------------------------------------------------------------------
# async_apply_battery_settings
# ---------------------------------------------------------------------------


class TestApplierChargePowerWrite:
    """The applier writes the scaled power and records it in the summary."""

    @pytest.mark.asyncio
    async def test_charge_grid_slot_writes_the_scaled_power(self) -> None:
        number, summary = await _apply(_cfg(), _live(), _rec(charged_kwh=0.284))
        assert _charge_power_writes(number) == [1136]
        assert any(
            r.entity_id == _CHARGE_ENTITY and r.desired == 1136 for r in summary.results
        )

    @pytest.mark.asyncio
    async def test_other_label_restores_the_entity_maximum(self) -> None:
        live = _live(commanded_charge_w=1136.0)
        number, _ = await _apply(
            _cfg(), live, _rec(Recommendations.BatteriesWaitMode.value)
        )
        assert _charge_power_writes(number) == [5000]

    @pytest.mark.asyncio
    async def test_unchanged_power_is_not_rewritten(self) -> None:
        live = _live(commanded_charge_w=1136.0)
        number, summary = await _apply(_cfg(), live, _rec(charged_kwh=0.284))
        assert _charge_power_writes(number) == []
        assert not any(r.entity_id == _CHARGE_ENTITY for r in summary.results)

    @pytest.mark.asyncio
    async def test_floor_zero_never_touches_the_entity(self) -> None:
        live = _live(commanded_charge_w=3000.0)
        number, _ = await _apply(_cfg(topup_pct=0.0), live, _rec(charged_kwh=0.284))
        assert _charge_power_writes(number) == []

    @pytest.mark.asyncio
    async def test_unknown_max_attribute_skips_the_write(self) -> None:
        """Without the hardware limit there is no safe rated reference."""
        number, _ = await _apply(_cfg(), _live(limit_w=None), _rec(charged_kwh=0.284))
        assert _charge_power_writes(number) == []

    @pytest.mark.asyncio
    async def test_unconfigured_entity_keeps_todays_behaviour(self) -> None:
        number, summary = await _apply(
            _cfg(charge_entity=None), _live(), _rec(charged_kwh=0.284)
        )
        assert number.await_args_list == []
        assert all(r.entity_id != _CHARGE_ENTITY for r in summary.results)

    @pytest.mark.asyncio
    async def test_failed_write_blocks_the_rest_of_the_cycle(self) -> None:
        """A verified write that exhausts its retries aborts like the others."""

        async def _failed(entity_id, desired, writer, reader, **kwargs):  # type: ignore[no-untyped-def]  # local test shim mirrors async_write_and_verify signature
            return ApplyResult(
                entity_id=entity_id,
                desired=desired,
                actual=None,
                status=ApplyStatus.FAILED,
                error_message="boom",
            )

        sensor = MagicMock()
        sensor.hass = MagicMock()
        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier_charge_power.async_write_and_verify",
                side_effect=_failed,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                side_effect=_write_and_verify_ok,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_select_option",
                new_callable=AsyncMock,
            ) as select,
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_tou_periods",
                new_callable=AsyncMock,
            ) as tou,
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_number_value",
                new_callable=AsyncMock,
            ),
        ):
            summary = await async_apply_battery_settings(
                sensor, _cfg(), _live(), _rec(charged_kwh=0.284), 0.0
            )
        assert [r.status for r in summary.results if r.entity_id == _CHARGE_ENTITY] == [
            ApplyStatus.FAILED
        ]
        assert summary.results[-1].entity_id == _CHARGE_ENTITY
        tou.assert_not_awaited()
        select.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_afternoon_replay_writes_once_per_label_change(self) -> None:
        """14:00 charge 1.016 kWh → 14:15 wait → 15:00 charge 0.284 → held 0."""
        cfg = _cfg()
        live = _live()
        writes: list[int] = []
        for rec in (
            _rec(charged_kwh=1.016),
            _rec(charged_kwh=1.016),
            _rec(Recommendations.BatteriesWaitMode.value),
            _rec(charged_kwh=0.284),
            _rec(charged_kwh=0.0),  # held by hysteresis, latest solve = 0
        ):
            number, _ = await _apply(cfg, live, rec)
            for value in _charge_power_writes(number):
                writes.append(value)
                live.huawei_batteries_max_charge_power_w = float(value)
        assert writes == [4064, 5000, 1136, 500]


# ---------------------------------------------------------------------------
# apply_executable_discharge_minimum
# ---------------------------------------------------------------------------


def _slot(
    start: datetime,
    recommendation: str,
    discharged_kwh: float,
    *,
    net_load_kwh: float = 0.4,
) -> PlannedSlot:
    return PlannedSlot(
        start=start,
        end=start + timedelta(minutes=15),
        recommendation=recommendation,
        batteries_discharged_kwh=discharged_kwh,
        avg_house_consumption_kwh=net_load_kwh,
        estimated_net_consumption_kwh=net_load_kwh,
    )


def _apply_minimum(
    slots: list[PlannedSlot], *, available_kwh: float = 7.5
) -> PlannedSlot | None:
    return apply_executable_discharge_minimum(
        slots, _NOW, max_discharge_per_slot_kwh=1.25, available_kwh=available_kwh
    )


class TestExecutableDischargeMinimum:
    """A live-slot discharge far below what self-consumption would deliver is
    cleared and relabelled; a plan that discharges the deliverable amount is
    kept however small."""

    def test_deliverable_is_the_smallest_of_the_three_bounds(self) -> None:
        slot = _slot(_SLOT_START, Recommendations.BatteriesDischargeMode.value, 0.1)
        assert deliverable_discharge_kwh(
            slot, max_discharge_per_slot_kwh=1.25, available_kwh=7.5
        ) == pytest.approx(0.4)
        assert deliverable_discharge_kwh(
            slot, max_discharge_per_slot_kwh=0.3, available_kwh=7.5
        ) == pytest.approx(0.3)
        assert deliverable_discharge_kwh(
            slot, max_discharge_per_slot_kwh=1.25, available_kwh=0.05
        ) == pytest.approx(0.05)
        surplus = _slot(
            _SLOT_START,
            Recommendations.BatteriesDischargeMode.value,
            0.1,
            net_load_kwh=-0.2,
        )
        assert deliverable_discharge_kwh(
            surplus, max_discharge_per_slot_kwh=1.25, available_kwh=7.5
        ) == pytest.approx(0.0)

    def test_threshold_is_a_fraction_of_the_deliverable_amount(self) -> None:
        assert executable_discharge_minimum_kwh(0.4) == pytest.approx(
            0.4 * EXECUTABLE_DISCHARGE_FRACTION
        )
        assert executable_discharge_minimum_kwh(0.0) == pytest.approx(0.0)

    def test_small_live_discharge_is_cleared_and_relabelled(self) -> None:
        """The reporter's 14:45 slot: 0.053 kWh planned, 0.39 kWh house load."""
        live = _slot(_SLOT_START, Recommendations.BatteriesDischargeMode.value, 0.053)
        later = _slot(_SLOT_END, Recommendations.BatteriesDischargeMode.value, 0.053)
        assert _apply_minimum([live, later]) is live
        assert live.recommendation == Recommendations.BatteriesWaitMode.value
        assert live.batteries_discharged_kwh == pytest.approx(0.0)
        # Future slots are re-solved before they execute: untouched.
        assert later.recommendation == Recommendations.BatteriesDischargeMode.value
        assert later.batteries_discharged_kwh == pytest.approx(0.053)

    def test_window_mode_is_covered_too(self) -> None:
        live = _slot(
            _SLOT_START, Recommendations.BatteriesDischargeWindowMode.value, 0.05
        )
        assert _apply_minimum([live]) is live
        assert live.recommendation == Recommendations.BatteriesWaitMode.value

    @pytest.mark.parametrize("discharged_kwh", [0.1, 0.25, 0.4])
    def test_executable_discharge_is_kept(self, discharged_kwh: float) -> None:
        live = _slot(
            _SLOT_START, Recommendations.BatteriesDischargeMode.value, discharged_kwh
        )
        assert _apply_minimum([live]) is None
        assert live.recommendation == Recommendations.BatteriesDischargeMode.value
        assert live.batteries_discharged_kwh == pytest.approx(discharged_kwh)

    def test_small_house_load_fully_covered_is_kept(self) -> None:
        """A low-load house discharges little per slot; that is the whole load."""
        live = _slot(
            _SLOT_START,
            Recommendations.BatteriesDischargeMode.value,
            0.08,
            net_load_kwh=0.08,
        )
        assert _apply_minimum([live]) is None
        assert live.recommendation == Recommendations.BatteriesDischargeMode.value

    def test_last_energy_above_the_floor_is_kept(self) -> None:
        """The battery is nearly empty: the plan discharges what is left."""
        live = _slot(_SLOT_START, Recommendations.BatteriesDischargeMode.value, 0.05)
        assert _apply_minimum([live], available_kwh=0.05) is None
        assert live.recommendation == Recommendations.BatteriesDischargeMode.value

    def test_zero_discharge_is_left_alone(self) -> None:
        live = _slot(_SLOT_START, Recommendations.BatteriesDischargeMode.value, 0.0)
        assert _apply_minimum([live]) is None
        assert live.recommendation == Recommendations.BatteriesDischargeMode.value

    @pytest.mark.parametrize(
        "recommendation",
        [
            Recommendations.ForceBatteriesDischarge.value,
            Recommendations.ForceExport.value,
            Recommendations.EVSmartCharging.value,
            Recommendations.BatteriesWaitMode.value,
        ],
    )
    def test_other_labels_are_never_changed(self, recommendation: str) -> None:
        live = _slot(_SLOT_START, recommendation, 0.053)
        assert _apply_minimum([live]) is None
        assert live.recommendation == recommendation
        assert live.batteries_discharged_kwh == pytest.approx(0.053)

    def test_pv_surplus_slot_disables_the_rule(self) -> None:
        live = _slot(
            _SLOT_START,
            Recommendations.BatteriesDischargeMode.value,
            0.053,
            net_load_kwh=-0.3,
        )
        assert _apply_minimum([live]) is None
        assert live.recommendation == Recommendations.BatteriesDischargeMode.value

    def test_no_live_slot_changes_nothing(self) -> None:
        later = _slot(_SLOT_END, Recommendations.BatteriesDischargeMode.value, 0.053)
        assert _apply_minimum([later]) is None


# ---------------------------------------------------------------------------
# Planner input: the rated reference while the scaling owns the entity
# ---------------------------------------------------------------------------


class TestResolveMaxChargePowerW:
    """A scaled write must not become next cycle's planning ceiling."""

    def test_scaling_on_plans_against_the_entity_maximum(self) -> None:
        live = _live(commanded_charge_w=1136.0, limit_w=5000.0)
        assert _resolve_max_charge_power_w(_cfg(), live) == pytest.approx(5000.0)

    def test_scaling_off_honours_the_commanded_value(self) -> None:
        live = _live(commanded_charge_w=3000.0, limit_w=5000.0)
        assert _resolve_max_charge_power_w(_cfg(topup_pct=0.0), live) == pytest.approx(
            3000.0
        )

    @pytest.mark.parametrize("limit_w", [None, 0.0])
    def test_unusable_maximum_falls_back_to_the_commanded_value(
        self, limit_w: float | None
    ) -> None:
        live = _live(commanded_charge_w=3000.0, limit_w=limit_w)
        assert _resolve_max_charge_power_w(_cfg(), live) == pytest.approx(3000.0)

    def test_nothing_known_defaults_to_5000(self) -> None:
        live = _live(commanded_charge_w=None, limit_w=None)
        assert _resolve_max_charge_power_w(_cfg(), live) == pytest.approx(5000.0)

    def test_planner_input_carries_the_entity_maximum(self) -> None:
        live = _live(commanded_charge_w=1136.0, limit_w=5000.0)
        planner_input = build_planner_input(
            cfg=_cfg(),
            live=live,
            hourly_recommendations=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
        )
        assert planner_input.battery_max_charge_power_w == pytest.approx(5000.0)


# ---------------------------------------------------------------------------
# current_slot_held on the working-mode sensor
# ---------------------------------------------------------------------------


class TestCurrentSlotHeldAttribute:
    """The sensor says when the label is a hysteresis hold."""

    @pytest.mark.parametrize("held", [False, True])
    def test_attribute_mirrors_coordinator_data(self, held: bool) -> None:
        data = CoordinatorData(current_slot_held=held)
        attrs = build_working_mode_attributes(
            data,
            _cfg(),
            _live(),
            unique_id="u",
            config_entry=MagicMock(),
            primary_grid_charge_owned=False,
        )
        assert attrs["current_slot_held"] is held


# ---------------------------------------------------------------------------
# Config option
# ---------------------------------------------------------------------------


class TestTopupOption:
    """The floor is a 0–100 % option with a 10 % default."""

    def test_default_is_ten_percent(self) -> None:
        assert SensorConfig().batteries_topup_min_power_pct == pytest.approx(10.0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pct", [0, 10, 100])
    async def test_valid_percentages_pass(self, pct: int) -> None:
        errors = await validate_batteries_wait_mode_input(
            {
                "hsem_batteries_wait_mode_behavior": "strict",
                "hsem_batteries_topup_min_power_pct": pct,
            }
        )
        assert errors == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pct", [-1, 101, "abc"])
    async def test_out_of_range_is_rejected(self, pct: object) -> None:
        errors = await validate_batteries_wait_mode_input(
            {
                "hsem_batteries_wait_mode_behavior": "strict",
                "hsem_batteries_topup_min_power_pct": pct,
            }
        )
        assert errors == {
            "hsem_batteries_topup_min_power_pct": "invalid_topup_min_power_pct"
        }

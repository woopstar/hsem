"""Tests for the ``batteries_discharge_mode`` discharge-cap exemption (issue #983).

A schedule discharge window keeps its ``batteries_discharge_mode`` label even
when the simulated discharge is zero (``planner/soc_simulation.py`` relabels
only the two force modes).  The derived
``applier_caps._primary_battery_hold()`` therefore read such a slot as an
explicit hold and wrote a 0 W Huawei discharge cap, which oscillated against
the rated maximum every time the re-solved current slot crossed the 0.001 kWh
materiality boundary — while the published recommendation, and therefore every
existing hysteresis layer, never moved.

Covers:
- ``_primary_battery_cap_hold()`` — the cap-scoped hold wrapper
- ``async_apply_battery_settings`` — no 0 W cap on a discharge slot, and no
  repeated writes across replans
- The 0 W paths that must keep immediate precedence (EV permission,
  solar-charge-only, wait-mode hold, SoC reserve guard)
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
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import ApplyResult, ApplyStatus
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.workingmodes import WorkingModes

_LOGGER_PATCH = "custom_components.hsem.utils.logger.HSEM_LOGGER.debug"
_NOW = datetime(2026, 9, 12, 19, 0, tzinfo=UTC)

# Below ``PLANNED_ENERGY_ROUNDING_KWH`` — what the solver rounds a marginal
# discharge down to, and the exact value that used to trigger the 0 W cap.
_NEAR_ZERO_KWH = 0.001
_MATERIAL_KWH = 0.5


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    """Live snapshot for the reporter's setup: LUNA2000 5 kWh, 2500 W rated."""
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
    recommendation: str = Recommendations.BatteriesDischargeMode.value,
    charged_kwh: float = 0.0,
    discharged_kwh: float = _NEAR_ZERO_KWH,
    grid_export_kwh: float = 0.0,
) -> HourlyRecommendation:
    """An evening schedule discharge slot with a near-zero solved discharge."""
    return HourlyRecommendation(
        start=_NOW,
        end=_NOW + timedelta(hours=1),
        recommendation=recommendation,
        avg_house_consumption_kwh=0.8,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=charged_kwh,
        batteries_discharged_kwh=discharged_kwh,
        estimated_battery_capacity_kwh=3.0,
        estimated_battery_soc_pct=60.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.8,
        export_price=0.05,
        grid_export_kwh=grid_export_kwh,
        grid_import_kwh=0.8,
        import_price=1.95,
        solcast_pv_estimate_kwh=0.0,
    )


async def _write_and_verify_ok(entity_id, desired, writer, reader, **kwargs):  # type: ignore[no-untyped-def]  # local test shim mirrors async_write_and_verify signature
    await writer()
    return ApplyResult(
        entity_id=entity_id,
        desired=desired,
        actual=desired,
        status=ApplyStatus.OK,
        attempts=1,
    )


# ---------------------------------------------------------------------------
# _primary_battery_cap_hold unit tests
# ---------------------------------------------------------------------------


class TestPrimaryBatteryCapHold:
    """The cap-scoped wrapper exempts ``batteries_discharge_mode`` only."""

    def test_discharge_mode_with_near_zero_energy_is_not_a_cap_hold(self):
        """The regression: the derived hold is True, the cap hold is not."""
        rec = _rec()
        assert _primary_battery_hold(rec) is True
        assert _primary_battery_cap_hold(rec) is False

    def test_discharge_mode_with_material_discharge_is_not_a_cap_hold(self):
        rec = _rec(discharged_kwh=_MATERIAL_KWH)
        assert _primary_battery_cap_hold(rec) is False

    def test_wait_mode_with_near_zero_energy_is_still_a_cap_hold(self):
        rec = _rec(recommendation=Recommendations.BatteriesWaitMode.value)
        assert _primary_battery_cap_hold(rec) is True

    def test_ev_smart_charging_relabel_is_still_a_cap_hold(self):
        """Relabelling never touches the energy fields, so the hold survives."""
        rec = _rec(recommendation=Recommendations.EVSmartCharging.value)
        assert _primary_battery_cap_hold(rec) is True

    def test_solar_charge_with_material_charge_is_not_a_cap_hold(self):
        rec = _rec(
            recommendation=Recommendations.BatteriesChargeSolar.value,
            charged_kwh=_MATERIAL_KWH,
        )
        assert _primary_battery_cap_hold(rec) is False

    def test_wrapper_does_not_change_the_underlying_derivation(self):
        """``_primary_battery_hold()`` itself keeps its meaning (held export)."""
        rec = _rec()
        assert _primary_battery_hold(rec) is True


# ---------------------------------------------------------------------------
# async_apply_battery_settings integration
# ---------------------------------------------------------------------------


def _apply_patches():
    """Patch context for one ``async_apply_battery_settings`` call."""
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
    required_battery_kwh: float = 0.0,
) -> AsyncMock:
    """Run one apply cycle and return the number-write mock."""
    sensor = _sensor()
    logger_p, verify_p, select_p, number_p = _apply_patches()
    with logger_p, verify_p, select_p, number_p as patched:
        await async_apply_battery_settings(sensor, cfg, live, rec, required_battery_kwh)
    mock_number: AsyncMock = patched
    return mock_number


def _discharge_cap_writes(mock_number: AsyncMock) -> list[int]:
    """Return the values written to the max-discharge-power entity."""
    return [
        call.args[2]
        for call in mock_number.await_args_list
        if call.args[1] == "number.maxdis"
    ]


class TestDischargeModeKeepsItsCap:
    """A discharge window must never be capped to 0 W by the derived hold."""

    @pytest.mark.asyncio
    async def test_near_zero_discharge_does_not_write_zero_cap(self):
        """The reporter's case: no EV, near-zero solved discharge, cap stays."""
        live = _live(max_discharge_power_w=2500)
        mock_number = await _apply(_cfg(), live, _rec())
        assert _discharge_cap_writes(mock_number) == []

    @pytest.mark.asyncio
    async def test_zero_cap_left_by_the_bug_is_restored_to_rated_max(self):
        """Hardware stuck at 0 W must be raised back to the rated maximum."""
        live = _live(max_discharge_power_w=0)
        mock_number = await _apply(_cfg(), live, _rec())
        assert _discharge_cap_writes(mock_number) == [2500]

    @pytest.mark.asyncio
    async def test_material_discharge_is_unchanged(self):
        live = _live(max_discharge_power_w=0)
        mock_number = await _apply(_cfg(), live, _rec(discharged_kwh=_MATERIAL_KWH))
        assert _discharge_cap_writes(mock_number) == [2500]

    @pytest.mark.asyncio
    async def test_discharge_slot_still_runs_maximize_self_consumption(self):
        """The 0 W cap used to disable the very mode the slot executes in."""
        sensor = _sensor()
        live = _live(max_discharge_power_w=2500)
        live.huawei_batteries_working_mode = WorkingModes.TimeOfUse.value
        logger_p, verify_p, select_p, number_p = _apply_patches()
        with logger_p, verify_p, select_p as mock_select, number_p:
            await async_apply_battery_settings(sensor, _cfg(), live, _rec(), 0.0)
        mock_select.assert_any_await(
            sensor, "select.wm", WorkingModes.MaximizeSelfConsumption.value
        )


class TestNoOscillationAcrossReplans:
    """The oscillation regression (issue #983).

    Replays the reporter's timeline: the same discharge slot is re-applied on
    every replan while the re-solved ``batteries_discharged_kwh`` flips across
    the materiality boundary.  Before the fix this wrote 0 W / 2500 W / 0 W …;
    now the register is written once and then left alone.
    """

    @pytest.mark.asyncio
    async def test_flipping_solved_discharge_writes_the_cap_only_once(self):
        cfg = _cfg()
        # Hardware starts where the bug left it, so the first cycle legitimately
        # writes once; every later cycle must be a no-op.
        live = _live(max_discharge_power_w=0)
        writes: list[int] = []

        for cycle in range(8):
            discharged = _NEAR_ZERO_KWH if cycle % 2 == 0 else _MATERIAL_KWH
            mock_number = await _apply(cfg, live, _rec(discharged_kwh=discharged))
            cycle_writes = _discharge_cap_writes(mock_number)
            writes.extend(cycle_writes)
            # The applier verifies its writes, so reflect them in live state.
            if cycle_writes:
                live.huawei_batteries_max_discharge_power_w = cycle_writes[-1]

        assert writes == [2500]
        assert live.huawei_batteries_max_discharge_power_w == 2500


class TestZeroCapPathsKeepPrecedence:
    """Every other 0 W path is independent of the exemption."""

    @pytest.mark.asyncio
    async def test_ev_without_permission_still_forces_zero(self):
        """Issue #797: an unpermitted EV blocks discharge, discharge slot or not."""
        cfg = _cfg()
        live = _live(max_discharge_power_w=2500)
        live.ev.is_charging = True
        live.ev.force_max_discharge_power = False
        mock_number = await _apply(cfg, live, _rec())
        assert _discharge_cap_writes(mock_number) == [0]

    @pytest.mark.asyncio
    async def test_permitted_ev_gets_the_planned_rate_cap(self):
        """Issue #797: an opted-in EV clamps the cap to the solved rate."""
        cfg = _cfg()
        live = _live(max_discharge_power_w=2500)
        live.ev.is_charging = True
        live.ev.force_max_discharge_power = True
        live.ev.max_discharge_power_w = 3000
        # 1.0 kWh over a 1 h slot -> 1000 W, below both ceilings.
        mock_number = await _apply(cfg, live, _rec(discharged_kwh=1.0))
        assert _discharge_cap_writes(mock_number) == [1000]

    @pytest.mark.asyncio
    async def test_soc_reserve_guard_still_forces_zero(self):
        """Issue #592: the EV-path reserve guard is unaffected."""
        cfg = _cfg()
        live = _live(max_discharge_power_w=2500)
        live.battery_current_capacity_kwh = 1.0
        live.ev.is_charging = True
        live.ev.force_max_discharge_power = True
        live.ev.max_discharge_power_w = 3000
        mock_number = await _apply(
            cfg, live, _rec(discharged_kwh=1.0), required_battery_kwh=2.0
        )
        assert _discharge_cap_writes(mock_number) == [0]

    @pytest.mark.asyncio
    async def test_solar_charge_only_still_forces_zero(self):
        """Issue #922: a solar-charge slot keeps its unconditional 0 W cap."""
        cfg = _cfg()
        live = _live(max_discharge_power_w=2500)
        rec = _rec(
            recommendation=Recommendations.BatteriesChargeSolar.value,
            charged_kwh=_MATERIAL_KWH,
            discharged_kwh=0.0,
        )
        mock_number = await _apply(cfg, live, rec)
        assert _discharge_cap_writes(mock_number) == [0]

    @pytest.mark.asyncio
    async def test_genuine_wait_slot_still_forces_zero(self):
        """Issue #797/#914: a held Wait slot keeps the hold cap."""
        cfg = _cfg()
        live = _live(max_discharge_power_w=2500)
        rec = _rec(
            recommendation=Recommendations.BatteriesWaitMode.value,
            discharged_kwh=0.0,
        )
        mock_number = await _apply(cfg, live, rec)
        assert _discharge_cap_writes(mock_number) == [0]

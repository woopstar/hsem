"""Tests for battery wait-mode self-consumption feature (issue #742).

Covers:
- Default constant value for wait-mode behaviour in ``const.py``
- Input validation in ``flows/batteries_wait_mode.py``
- Discharge cap helper in ``custom_sensors/applier.py``
- The ``wait_mode_reserve_kwh`` fallback-to-strict-Wait and
  reserve-gated self-consumption integration in ``async_apply_battery_settings``
  (issue #914)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.const import (
    DEFAULT_CONFIG_VALUES,
    DEFAULT_HSEM_BATTERIES_WAIT_MODE,
)
from custom_components.hsem.custom_sensors.applier import (
    _wait_mode_self_consumption_cap_w,
    async_apply_battery_settings,
)
from custom_components.hsem.flows.batteries_wait_mode import (
    validate_batteries_wait_mode_input,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import ApplyResult, ApplyStatus
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.workingmodes import WorkingModes

_LOGGER_PATCH = "custom_components.hsem.utils.logger.HSEM_LOGGER.debug"
_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Default constant value tests
# ---------------------------------------------------------------------------


class TestWaitModeDefaults:
    """Verify the wait-mode behaviour default is safe."""

    def test_wait_mode_strict_by_default(self):
        """Wait mode must default to strict to preserve existing behaviour."""
        assert DEFAULT_CONFIG_VALUES["hsem_batteries_wait_mode_behavior"] == "strict"


# ---------------------------------------------------------------------------
# _wait_mode_self_consumption_cap_w tests
# ---------------------------------------------------------------------------


class TestWaitModeSelfConsumptionCapW:
    """Unit tests for the reserve-preserving discharge cap helper."""

    def test_no_surplus_returns_zero(self):
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=2.0,
            required_capacity_kwh=2.0,
            slot_hours=0.25,
            max_discharge_power_w=5000,
        )
        assert cap == 0

    def test_below_reserve_returns_zero(self):
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=1.5,
            required_capacity_kwh=2.0,
            slot_hours=0.25,
            max_discharge_power_w=5000,
        )
        assert cap == 0

    def test_surplus_converted_to_power(self):
        """1 kWh surplus over a 1-hour slot -> 1000 W cap."""
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=3.0,
            required_capacity_kwh=2.0,
            slot_hours=1.0,
            max_discharge_power_w=5000,
        )
        assert cap == 1000

    def test_surplus_over_short_slot(self):
        """1 kWh surplus over a 15-minute slot -> 4000 W cap."""
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=3.0,
            required_capacity_kwh=2.0,
            slot_hours=0.25,
            max_discharge_power_w=5000,
        )
        assert cap == 4000

    def test_cap_limited_by_max_discharge_power(self):
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=10.0,
            required_capacity_kwh=0.0,
            slot_hours=0.25,
            max_discharge_power_w=2500,
        )
        assert cap == 2500

    def test_zero_slot_hours_returns_zero(self):
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=5.0,
            required_capacity_kwh=0.0,
            slot_hours=0.0,
            max_discharge_power_w=5000,
        )
        assert cap == 0


# ---------------------------------------------------------------------------
# validate_batteries_wait_mode_input tests
# ---------------------------------------------------------------------------


class TestValidateBatteriesWaitModeInput:
    """Unit tests for the wait-mode config-flow input validator."""

    @pytest.mark.asyncio
    async def test_strict_value_is_valid(self):
        errors = await validate_batteries_wait_mode_input(
            {"hsem_batteries_wait_mode_behavior": "strict"}
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_self_consumption_value_is_valid(self):
        errors = await validate_batteries_wait_mode_input(
            {"hsem_batteries_wait_mode_behavior": "self_consumption_with_reserve"}
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_invalid_value_is_rejected(self):
        errors = await validate_batteries_wait_mode_input(
            {"hsem_batteries_wait_mode_behavior": "something_else"}
        )
        assert "hsem_batteries_wait_mode_behavior" in errors

    @pytest.mark.asyncio
    async def test_missing_field_is_rejected(self):
        errors = await validate_batteries_wait_mode_input({})
        assert "hsem_batteries_wait_mode_behavior" in errors


# ---------------------------------------------------------------------------
# async_apply_battery_settings — wait_mode_reserve_kwh integration (issue #914)
# ---------------------------------------------------------------------------


def _sensor() -> MagicMock:
    sensor = MagicMock()
    sensor.hass = MagicMock()
    return sensor


def _cfg() -> SensorConfig:
    cfg = SensorConfig()
    cfg.read_only = False
    cfg.batteries_wait_mode_behavior = "self_consumption_with_reserve"
    cfg.huawei_solar_batteries_working_mode = "select.wm"
    cfg.huawei_solar_batteries_maximum_discharging_power = "number.maxdis"
    cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou = "select.excess"
    cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = "sensor.tou"
    cfg.huawei_solar_device_id_batteries = "bat1"
    return cfg


def _live(*, working_mode: str) -> LiveState:
    live = LiveState()
    live._degraded_mode = DegradedMode.OK
    live.battery_current_capacity_kwh = 2.0
    live.huawei_batteries_rated_capacity_wh = 5000
    # Matches get_max_discharge_power(5000) so the unconditional first write
    # (independent of wait-mode) is a no-op and doesn't interfere.
    live.huawei_batteries_max_discharge_power_w = 2500
    live.huawei_batteries_working_mode = working_mode
    live.huawei_batteries_excess_pv_use_in_tou = "charge"
    live.tou_periods.periods = list(DEFAULT_HSEM_BATTERIES_WAIT_MODE)
    return live


def _wait_rec() -> HourlyRecommendation:
    """A non-held BatteriesWaitMode slot (material planned discharge)."""
    return HourlyRecommendation(
        start=_NOW,
        end=_NOW + timedelta(hours=1),
        recommendation=Recommendations.BatteriesWaitMode.value,
        avg_house_consumption_kwh=0.5,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=0.0,
        # Material (non-near-zero) so _primary_battery_hold() is False and
        # the self_consumption_with_reserve branch is actually reached.
        batteries_discharged_kwh=0.05,
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


async def _write_and_verify_ok(entity_id, desired, writer, reader, **kwargs):  # type: ignore[no-untyped-def]  # local test shim mirrors async_write_and_verify signature
    await writer()
    return ApplyResult(
        entity_id=entity_id,
        desired=desired,
        actual=desired,
        status=ApplyStatus.OK,
        attempts=1,
    )


class TestWaitModeReserveNoneFallsBackToStrictWait:
    """``wait_mode_reserve_kwh=None`` must force strict TOU wait (issue #914).

    Even with ``self_consumption_with_reserve`` configured, a reserve that
    could not be reliably derived must never be treated as "no reserve
    needed" (which would let the battery discharge freely) — the applier
    must fall back to the same strict TOU wait behaviour as
    ``batteries_wait_mode_behavior == "strict"``.
    """

    @pytest.mark.asyncio
    async def test_none_reserve_writes_strict_tou_wait(self):
        sensor = _sensor()
        cfg = _cfg()
        live = _live(working_mode=WorkingModes.MaximizeSelfConsumption.value)
        rec = _wait_rec()

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                side_effect=_write_and_verify_ok,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_select_option",
                new_callable=AsyncMock,
            ) as mock_select,
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_number_value",
                new_callable=AsyncMock,
            ) as mock_number,
        ):
            await async_apply_battery_settings(
                sensor, cfg, live, rec, 5.0, wait_mode_reserve_kwh=None
            )

        mock_select.assert_any_await(sensor, "select.wm", WorkingModes.TimeOfUse.value)
        # No wait-mode self-consumption discharge cap write — the max
        # discharge power number entity is never touched (the unconditional
        # first write is a no-op because live already matches).
        mock_number.assert_not_awaited()


class TestWaitModeReserveGatesSelfConsumption:
    """A valid ``wait_mode_reserve_kwh`` gates MSC + the reserve-preserving cap."""

    @pytest.mark.asyncio
    async def test_surplus_above_reserve_enables_msc_with_capped_discharge(self):
        sensor = _sensor()
        cfg = _cfg()
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        rec = _wait_rec()

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                side_effect=_write_and_verify_ok,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_select_option",
                new_callable=AsyncMock,
            ) as mock_select,
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_number_value",
                new_callable=AsyncMock,
            ) as mock_number,
        ):
            await async_apply_battery_settings(
                sensor, cfg, live, rec, 5.0, wait_mode_reserve_kwh=1.0
            )

        mock_select.assert_any_await(
            sensor, "select.wm", WorkingModes.MaximizeSelfConsumption.value
        )
        # capacity=2.0, reserve=1.0 -> surplus=1.0 kWh over a 1h slot -> 1000 W.
        mock_number.assert_any_await(sensor, "number.maxdis", 1000)

    @pytest.mark.asyncio
    async def test_capacity_at_reserve_falls_back_to_strict_wait(self):
        """No surplus above the reserve -> strict TOU wait, same as ``strict`` mode."""
        sensor = _sensor()
        cfg = _cfg()
        live = _live(working_mode=WorkingModes.MaximizeSelfConsumption.value)
        live.battery_current_capacity_kwh = 1.0  # equals the reserve -> no surplus
        rec = _wait_rec()

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                side_effect=_write_and_verify_ok,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_select_option",
                new_callable=AsyncMock,
            ) as mock_select,
        ):
            await async_apply_battery_settings(
                sensor, cfg, live, rec, 5.0, wait_mode_reserve_kwh=1.0
            )

        mock_select.assert_any_await(sensor, "select.wm", WorkingModes.TimeOfUse.value)

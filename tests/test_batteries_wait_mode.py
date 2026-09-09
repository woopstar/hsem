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
    """Unit tests for the reserve-preserving discharge cap helper (issue #942).

    The cap is an SoC-floor stop-discharge gate, not a rate spread over the
    slot: any surplus above the reserve unlocks the full rated/configured
    discharge rate, so a real load spike is served from the battery instead
    of the grid; at or below the reserve, discharge is 0.
    """

    def test_no_surplus_returns_zero(self):
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=2.0,
            required_capacity_kwh=2.0,
            max_discharge_power_w=5000,
        )
        assert cap == 0

    def test_below_reserve_returns_zero(self):
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=1.5,
            required_capacity_kwh=2.0,
            max_discharge_power_w=5000,
        )
        assert cap == 0

    def test_small_surplus_unlocks_full_rated_max(self):
        """A small surplus still unlocks the full rate, not a scaled-down value."""
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=2.01,
            required_capacity_kwh=2.0,
            max_discharge_power_w=5000,
        )
        assert cap == 5000

    def test_large_surplus_still_capped_at_rated_max(self):
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=10.0,
            required_capacity_kwh=0.0,
            max_discharge_power_w=2500,
        )
        assert cap == 2500


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
    """A genuine (held) BatteriesWaitMode slot — the realistic case (issue #954).

    ``batteries_charged_kwh``/``batteries_discharged_kwh`` are both near-zero,
    matching what ``planner/soc_simulation.py`` actually produces for a "Wait"
    slot, so ``_primary_battery_hold()`` is ``True`` here — exactly the
    real-world case the #954 fix targets (the reserve-floor decision must run
    regardless of hold status, not only for the synthetic non-held slot the
    tests used to construct).
    """
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
        batteries_discharged_kwh=0.0,
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
    """``wait_mode_reserve_kwh=None`` must fall back to the same behaviour
    ``batteries_wait_mode_behavior == "strict"`` would produce for this slot
    (issue #914) — a reserve that could not be reliably derived must never be
    treated as "no reserve needed" (which would let the battery discharge
    freely).

    For a genuine held Wait slot, "strict" behaviour is ``MaximizeSelfConsumption``
    with the discharge cap held at 0 W (issue #797/#922) — not literal
    ``TimeOfUse`` wait, which only applies to the rarer unheld case (issue #954).
    """

    @pytest.mark.asyncio
    async def test_none_reserve_on_held_slot_falls_back_to_msc_with_zero_cap(self):
        """Genuine held slot + reserve=None -> same as strict mode: MSC, 0 W cap."""
        sensor = _sensor()
        cfg = _cfg()
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        rec = (
            _wait_rec()
        )  # held: batteries_charged_kwh == batteries_discharged_kwh == 0

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

        mock_select.assert_any_await(
            sensor, "select.wm", WorkingModes.MaximizeSelfConsumption.value
        )
        mock_number.assert_any_await(sensor, "number.maxdis", 0)

    @pytest.mark.asyncio
    async def test_none_reserve_on_unheld_slot_falls_back_to_strict_tou_wait(self):
        """Rarer unheld slot + reserve=None -> strict TOU wait (issue #914)."""
        sensor = _sensor()
        cfg = _cfg()
        live = _live(working_mode=WorkingModes.MaximizeSelfConsumption.value)
        rec = _wait_rec()
        # Material (non-near-zero) so _primary_battery_hold() is False —
        # the rarer unheld-slot edge case.
        rec.batteries_discharged_kwh = 0.05

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
    async def test_surplus_above_reserve_enables_msc_at_full_rate(self):
        """Any surplus above the reserve restores the full rated rate (issue #942).

        Regression guard: the old formula computed ``surplus_kwh / slot_hours``,
        producing a low, load-averaged wattage (e.g. 1000 W here) that could not
        track a real household load spike. Starting from a stale low cap (as the
        old formula would have written) proves the fix actively restores the
        full rated/configured discharge rate instead.

        Also the issue #954 regression guard: ``rec`` here is a *genuine held*
        Wait slot (``_primary_battery_hold()`` is ``True``) — the realistic
        production case. Before #954, the hold check ran first and forced the
        cap to 0 W unconditionally, so this reserve-floor branch never actually
        overrode it for a real Wait slot at all.
        """
        sensor = _sensor()
        cfg = _cfg()
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        live.huawei_batteries_max_discharge_power_w = 380
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
        # capacity=2.0 kWh > reserve=1.0 kWh -> full rated max for a 5000 Wh
        # pack (2500 W), not surplus/slot_hours (which would have been 1000 W).
        mock_number.assert_any_await(sensor, "number.maxdis", 2500)

    @pytest.mark.asyncio
    async def test_capacity_at_reserve_falls_back_to_strict_wait(self):
        """No surplus above the reserve -> strict TOU wait, same as ``strict`` mode.

        Also asserts the discharge cap is explicitly written to 0 W (issue #954):
        the reserve-floor decision now runs ahead of the hold check, so this is a
        real hardware write, not just an implicit side effect of the mode switch.
        """
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
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_number_value",
                new_callable=AsyncMock,
            ) as mock_number,
        ):
            await async_apply_battery_settings(
                sensor, cfg, live, rec, 5.0, wait_mode_reserve_kwh=1.0
            )

        mock_select.assert_any_await(sensor, "select.wm", WorkingModes.TimeOfUse.value)
        mock_number.assert_any_await(sensor, "number.maxdis", 0)

    @pytest.mark.asyncio
    async def test_full_soc_held_slot_uses_full_rate_not_zero(self):
        """Exact issue #954 reproduction: 100% SoC, held Wait slot, reserve well
        below capacity -> full rated max, not 0 W.

        Before this fix, ``_primary_battery_hold()`` was ``True`` for this
        genuine Wait slot and forced the cap to 0 W before the
        ``self_consumption_with_reserve`` reserve-floor logic ever ran, even
        though the battery held 4x the reserve in usable surplus.
        """
        sensor = _sensor()
        cfg = _cfg()
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        live.battery_current_capacity_kwh = 5.0  # 100% of a 5000 Wh pack
        # Simulate the stale 0 W cap the pre-fix hold check would have forced.
        live.huawei_batteries_max_discharge_power_w = 0
        rec = (
            _wait_rec()
        )  # held: batteries_charged_kwh == batteries_discharged_kwh == 0
        assert rec.batteries_charged_kwh == 0.0
        assert rec.batteries_discharged_kwh == 0.0

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
                sensor, cfg, live, rec, 0.0, wait_mode_reserve_kwh=1.0
            )

        mock_select.assert_any_await(
            sensor, "select.wm", WorkingModes.MaximizeSelfConsumption.value
        )
        mock_number.assert_any_await(sensor, "number.maxdis", 2500)

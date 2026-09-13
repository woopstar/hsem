"""Tests for the zero-ceiling EV discharge-permission warning (issue #991).

Enabling ``hsem_ev_charger_force_max_discharge_power`` while leaving
``hsem_ev_charger_max_discharge_power`` at its 0 default silently changes
nothing — planner and applier both fail closed on a zero ceiling. The
config flow rejects the combination on submit, but config entries saved
before that check exist keep the incoherent state; for those, the applier
emits one clear, latched warning naming both settings instead of per-cycle
spam.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from custom_components.hsem.utils.workingmodes import WorkingModes

_WARNING_PATCH = "custom_components.hsem.utils.logger.HSEM_LOGGER.warning"
_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _sensor() -> MagicMock:
    sensor = MagicMock()
    sensor.hass = MagicMock()
    # The latch the applier uses to warn once per incoherent-EV episode.
    sensor._ev_zero_discharge_ceiling_warned = set()
    return sensor


def _cfg() -> SensorConfig:
    cfg = SensorConfig()
    cfg.read_only = False
    cfg.huawei_solar_batteries_working_mode = "select.wm"
    cfg.huawei_solar_batteries_maximum_discharging_power = "number.maxdis"
    cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou = "select.excess"
    cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = "sensor.tou"
    cfg.huawei_solar_device_id_batteries = "bat1"
    return cfg


def _live(*, ev_ceiling_w: int) -> LiveState:
    live = LiveState()
    live._degraded_mode = DegradedMode.OK
    live.battery_current_capacity_kwh = 2.0
    live.huawei_batteries_rated_capacity_wh = 5000
    # Matches get_max_discharge_power(5000) so the unconditional first write
    # is a no-op and doesn't interfere.
    live.huawei_batteries_max_discharge_power_w = 2500
    live.huawei_batteries_working_mode = WorkingModes.MaximizeSelfConsumption.value
    live.huawei_batteries_excess_pv_use_in_tou = "charge"
    # EV actively charging, permission ON, ceiling as parameterised.
    live.ev.is_charging = True
    live.ev.is_connected = True
    live.ev.force_max_discharge_power = True
    live.ev.max_discharge_power_w = ev_ceiling_w
    return live


def _discharge_rec() -> HourlyRecommendation:
    """A genuine discharge slot while the EV is planned/active."""
    return HourlyRecommendation(
        start=_NOW,
        end=_NOW + timedelta(hours=1),
        recommendation=Recommendations.BatteriesDischargeMode.value,
        avg_house_consumption_kwh=0.5,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=0.0,
        batteries_discharged_kwh=1.5,
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


async def _apply(sensor, cfg, live, rec):
    with (
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
        ) as mock_number,
        patch(_WARNING_PATCH, new_callable=MagicMock) as mock_warning,
    ):
        await async_apply_battery_settings(
            sensor, cfg, live, rec, 0.0, wait_mode_reserve_kwh=None
        )
    return mock_number, mock_warning


class TestZeroCeilingWarning:
    """The applier surfaces the incoherent permission-on/ceiling-0 config."""

    @pytest.mark.asyncio
    async def test_zero_ceiling_caps_to_zero_and_warns_once(self):
        """Permission on + ceiling 0 → 0 W cap, one warning naming both settings."""
        sensor = _sensor()
        cfg = _cfg()
        live = _live(ev_ceiling_w=0)
        rec = _discharge_rec()

        mock_number, mock_warning = await _apply(sensor, cfg, live, rec)

        # Precondition: the 0 W cap was actually applied — the trap is live.
        mock_number.assert_any_await(sensor, "number.maxdis", 0)
        warnings = [str(call) for call in mock_warning.call_args_list]
        matching = [
            w
            for w in warnings
            if "hsem_ev_charger_force_max_discharge_power" in w
            and "hsem_ev_charger_max_discharge_power" in w
        ]
        assert len(matching) == 1, f"expected exactly one naming warning: {warnings}"

        # A second cycle with the same config must not re-warn.
        _, mock_warning2 = await _apply(sensor, cfg, live, rec)
        mock_warning2.assert_not_called()

    @pytest.mark.asyncio
    async def test_positive_ceiling_does_not_warn(self):
        """Coherent config (permission on, ceiling > 0) never warns."""
        sensor = _sensor()
        cfg = _cfg()
        live = _live(ev_ceiling_w=2000)
        rec = _discharge_rec()

        mock_number, mock_warning = await _apply(sensor, cfg, live, rec)

        mock_warning.assert_not_called()
        # And the cap actually follows the ceiling, not zero.
        mock_number.assert_any_await(sensor, "number.maxdis", 1500)

    @pytest.mark.asyncio
    async def test_warning_rearms_after_config_is_fixed_and_breaks_again(self):
        """Fixing the ceiling clears the latch; breaking it again re-warns."""
        sensor = _sensor()
        cfg = _cfg()
        rec = _discharge_rec()

        _, mock_warning = await _apply(sensor, cfg, _live(ev_ceiling_w=0), rec)
        assert mock_warning.call_count == 1

        # User fixes the ceiling — no warning, latch re-arms.
        _, mock_warning = await _apply(sensor, cfg, _live(ev_ceiling_w=2000), rec)
        mock_warning.assert_not_called()

        # Config broken again (e.g. options flow edit) — warn once more.
        _, mock_warning = await _apply(sensor, cfg, _live(ev_ceiling_w=0), rec)
        assert mock_warning.call_count == 1

    @pytest.mark.asyncio
    async def test_second_ev_zero_ceiling_names_second_settings(self):
        """The second EV's warning names its own hsem_ev_second_* keys."""
        sensor = _sensor()
        cfg = _cfg()
        live = _live(ev_ceiling_w=2000)
        live.ev.is_charging = False  # only the second EV is active
        live.ev_second.is_charging = True
        live.ev_second.is_connected = True
        live.ev_second.force_max_discharge_power = True
        live.ev_second.max_discharge_power_w = 0
        rec = _discharge_rec()

        _, mock_warning = await _apply(sensor, cfg, live, rec)

        warnings = [str(call) for call in mock_warning.call_args_list]
        matching = [
            w
            for w in warnings
            if "hsem_ev_second_charger_force_max_discharge_power" in w
            and "hsem_ev_second_charger_max_discharge_power" in w
        ]
        assert len(matching) == 1, f"expected one second-EV warning: {warnings}"

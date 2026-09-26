"""Regression tests for issue #1103 — force charge survives the load-forecast hold.

While the house-load forecast is not ready, HSEM publishes a strict storage
hold. That hold used to zero the EV charger command, so a managed EV got an
enforced 0 A OCPP profile and ``switch.hsem_ev_force_charge_now`` could not
override it. Force charge is an explicit user override: the hold keeps the
home battery idle but must no longer zero a user-forced EV command.

Three paths are covered:

- :func:`apply_force_charge_overrides` applied after the strict hold.
- The planner phase when the hold fires inside it (``consumption_ok=False``
  and ``live_demand_contradicts_zero_profile``).
- The full update cycle when the forecast is not ready, so the planner phase
  is skipped entirely, including the OCPP target it pushes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.coordinator_helpers import (
    apply_force_charge_overrides,
    apply_load_forecast_hold,
    live_demand_contradicts_zero_profile,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.utils.recommendations import Recommendations
from tests.coordinator_fixtures import make_real_coordinator
from tests.test_ha_mock_integration import (
    _BASE_ENTITY_STATES,
    _patch_all_ha_helpers,
    make_fake_config_entry,
    make_fake_hass,
)

_SLOT = timedelta(minutes=15)
_SLOT_START = datetime(2026, 9, 25, 21, 0, tzinfo=UTC)
_NOW = _SLOT_START + timedelta(minutes=5)
_WAIT = Recommendations.BatteriesWaitMode.value
_EV = Recommendations.EVSmartCharging.value
_CHARGER_KW = 11.0
_CHARGER_W = _CHARGER_KW * 1000.0
# Command stability publishes whole amps: 15 A x 3 x 230 V.
_CHARGER_WHOLE_AMP_W = 15 * 3 * 230.0
_PLANNER_MODULE = "custom_components.hsem.coordinator_planner_phase"
_CYCLE_MODULE = "custom_components.hsem.coordinator_cycle"

# 3 x 25 A x 230 V = 17.25 kW fuse budget, comfortably above one 11 kW charger.
_EV_OPTIONS: dict[str, Any] = {
    "hsem_ev_planned_load_enabled": True,
    "hsem_ev_smart_charging": True,
    "hsem_ev_planned_load_charger_power_kw": _CHARGER_KW,
    "hsem_ev_planned_load_charger_phase_topology": "three_phase_balanced",
    "hsem_main_fuse_amps": 25,
    "hsem_main_fuse_phases": 3,
}


def _rec(start: datetime, load_kwh: float = 0.0) -> HourlyRecommendation:
    """Return a 15-minute recommendation slot with a flat load profile."""
    return HourlyRecommendation(
        start=start,
        end=start + _SLOT,
        recommendation=Recommendations.BatteriesChargeGrid.value,
        avg_house_consumption_kwh=load_kwh,
        avg_house_consumption_1d_kwh=load_kwh,
        avg_house_consumption_3d_kwh=load_kwh,
        avg_house_consumption_7d_kwh=load_kwh,
        avg_house_consumption_14d_kwh=load_kwh,
        batteries_charged_kwh=1.0,
        batteries_discharged_kwh=0.0,
        estimated_battery_capacity_kwh=0.0,
        estimated_battery_soc_pct=0.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.0,
        export_price=0.0,
        grid_export_kwh=0.0,
        grid_import_kwh=1.0,
        import_price=2.0,
        solcast_pv_estimate_kwh=0.0,
    )


def _config_entry(force: bool, *, second_force: bool = False) -> MagicMock:
    """Return a config entry with a managed primary EV and a real options dict."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {
        **_EV_OPTIONS,
        "hsem_ev_force_charge_now": force,
        "hsem_ev_second_force_charge_now": second_force,
    }
    entry.data = {}
    return entry


def _connected_live(**kwargs: Any) -> LiveState:
    """Return live state for an automatic-mode site with the EV plugged in."""
    live = LiveState(force_working_mode_state="auto", **kwargs)
    live.ev.is_connected = True
    live.ev_planned_load_connected = True
    return live


class TestForceChargeAfterStrictHold:
    """The helper re-publishes only the forced EV command after the hold."""

    def _hold_then_force(
        self, entry: MagicMock, live: LiveState
    ) -> HourlyRecommendation:
        recs = [_rec(_SLOT_START, 0.2), _rec(_SLOT_START + _SLOT, 0.2)]
        held = apply_load_forecast_hold(recs, live, _NOW, load_forecast_ready=False)
        assert held is recs[0]
        apply_force_charge_overrides(
            hass=MagicMock(),
            config_entry=entry,
            hourly_recommendations=recs,
            ev_plan=None,
            ev_second_plan=None,
            now=_NOW,
            live=live,
            was_connected=True,
            was_second_connected=None,
        )
        return recs[0]

    def test_forced_ev_charges_at_max_while_battery_stays_held(self) -> None:
        current = self._hold_then_force(_config_entry(True), _connected_live())

        assert current.ev_charger_calculated_power == pytest.approx(_CHARGER_W)
        assert current.ev_second_charger_calculated_power == pytest.approx(0.0)
        assert current.recommendation == _EV
        # The home battery stays held: no plan-derived charge/discharge.
        assert current.batteries_charged_kwh == pytest.approx(0.0)
        assert current.batteries_discharged_kwh == pytest.approx(0.0)
        # EV load accounting follows the forced power for the remaining 10 min.
        forced_kwh = _CHARGER_W * (10.0 / 60.0) / 1000.0
        assert current.ev_total_planned_load_kwh == pytest.approx(forced_kwh, abs=1e-3)
        assert current.ev_planned_load_kwh + current.ev_accounted_load_kwh == (
            pytest.approx(current.ev_total_planned_load_kwh, abs=1e-3)
        )

    def test_force_off_keeps_the_enforced_zero(self) -> None:
        current = self._hold_then_force(_config_entry(False), _connected_live())

        assert current.recommendation == _WAIT
        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        assert current.ev_total_planned_load_kwh == pytest.approx(0.0)
        assert current.grid_import_kwh == pytest.approx(0.0)

    def test_second_ev_force_charge_survives_the_hold(self) -> None:
        entry = _config_entry(False, second_force=True)
        entry.options["hsem_ev_second_planned_load_charger_power_kw"] = 7.0
        live = _connected_live()
        live.ev_second.is_connected = True

        current = self._hold_then_force(entry, live)

        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        assert current.ev_second_charger_calculated_power == pytest.approx(7000.0)

    def test_disconnect_resets_the_switch_and_keeps_zero(self) -> None:
        entry = _config_entry(True)
        hass = MagicMock()
        hass.config_entries.async_update_entry.side_effect = lambda e, *, options: (
            setattr(e, "options", options)
        )
        live = _connected_live()
        live.ev.is_connected = False
        recs = [_rec(_SLOT_START, 0.2)]
        apply_load_forecast_hold(recs, live, _NOW, load_forecast_ready=False)

        apply_force_charge_overrides(
            hass=hass,
            config_entry=entry,
            hourly_recommendations=recs,
            ev_plan=None,
            ev_second_plan=None,
            now=_NOW,
            live=live,
            was_connected=True,
            was_second_connected=None,
        )

        assert entry.options["hsem_ev_force_charge_now"] is False
        assert recs[0].ev_charger_calculated_power == pytest.approx(0.0)
        assert recs[0].recommendation == _WAIT


def _planner_coordinator(
    tmp_path: Path, *, force: bool
) -> tuple[HSEMDataUpdateCoordinator, list[HourlyRecommendation]]:
    """Return a real coordinator whose planner yields a zero-EV grid-charge plan."""
    output = PlannerOutput(
        slots=[
            PlannedSlot(
                start=_SLOT_START + i * _SLOT,
                end=_SLOT_START + (i + 1) * _SLOT,
                recommendation=Recommendations.BatteriesChargeGrid.value,
                batteries_charged_kwh=1.0,
            )
            for i in range(2)
        ]
    )
    hass = MagicMock()
    hass.config.config_dir = str(tmp_path)
    hass.async_add_executor_job = AsyncMock(return_value=output)
    coordinator = make_real_coordinator(config_entry=_config_entry(force), hass=hass)
    coordinator._last_plan_ev_connected = True
    recs = [_rec(_SLOT_START, 0.0), _rec(_SLOT_START + _SLOT, 0.0)]
    coordinator._hourly_recommendations = recs
    return coordinator, recs


async def _run_planner_phase(
    coordinator: HSEMDataUpdateCoordinator,
    live: LiveState,
    *,
    consumption_ok: bool,
) -> str | None:
    with patch(
        f"{_PLANNER_MODULE}.build_planner_input", MagicMock(return_value=PlannerInput())
    ):
        state, _fresh, _output = await coordinator._run_planner_phase(
            _NOW, live, coordinator._cfg, None, consumption_ok, 0
        )
    return state


class TestPlannerPhaseHold:
    """Inside the planner phase, force charge now runs after the hold."""

    @pytest.mark.asyncio
    async def test_unready_forecast_keeps_the_forced_command(
        self, tmp_path: Path
    ) -> None:
        coordinator, _ = _planner_coordinator(tmp_path, force=True)

        state = await _run_planner_phase(
            coordinator, _connected_live(), consumption_ok=False
        )

        current = coordinator._hourly_recommendation
        assert current is not None
        assert state == _EV
        assert current.ev_charger_calculated_power == pytest.approx(
            _CHARGER_WHOLE_AMP_W
        )
        assert current.batteries_charged_kwh == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_live_demand_contradiction_keeps_the_forced_command(
        self, tmp_path: Path
    ) -> None:
        """An all-zero profile disproved by live demand still honours force.

        ``assess_load_forecast`` reports ``zero_forecast_with_live_demand`` as
        not ready, so the coordinator passes ``consumption_ok=False`` together
        with a live demand that contradicts the all-zero future profile.
        """
        coordinator, recs = _planner_coordinator(tmp_path, force=True)
        live = _connected_live(house_consumption_power_w=900.0)
        assert live_demand_contradicts_zero_profile(recs, live, _NOW)

        state = await _run_planner_phase(coordinator, live, consumption_ok=False)

        current = coordinator._hourly_recommendation
        assert current is not None
        assert state == _EV
        assert current.ev_charger_calculated_power == pytest.approx(
            _CHARGER_WHOLE_AMP_W
        )
        assert current.batteries_charged_kwh == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_force_off_hold_still_enforces_zero(self, tmp_path: Path) -> None:
        coordinator, _ = _planner_coordinator(tmp_path, force=False)

        state = await _run_planner_phase(
            coordinator, _connected_live(), consumption_ok=False
        )

        current = coordinator._hourly_recommendation
        assert current is not None
        assert state == _WAIT
        assert current.ev_charger_calculated_power == pytest.approx(0.0)


class TestNonPlannerCycleHold:
    """With the forecast not ready the planner is skipped, yet force still wins."""

    @staticmethod
    def _coordinator(
        force: bool,
    ) -> tuple[HSEMDataUpdateCoordinator, list[CoordinatorData], MagicMock]:
        entry = make_fake_config_entry(
            {
                "hsem_read_only": True,
                "hsem_ocpp_enabled": True,
                **_EV_OPTIONS,
                "hsem_ev_force_charge_now": force,
            }
        )
        coordinator = make_real_coordinator(
            hass=make_fake_hass(dict(_BASE_ENTITY_STATES)), config_entry=entry
        )
        published: list[CoordinatorData] = []
        coordinator.async_set_updated_data = published.append  # type: ignore[method-assign, assignment]  # test monkey-patch
        server = MagicMock()
        server.charger_sessions = {}
        server.is_listening = True
        server.last_requested_current_a = None
        server.anti_flap_state = "idle"
        server.is_stalled = False
        server.update_charge_target = AsyncMock()
        coordinator._ocpp_server = server
        return coordinator, published, server

    async def _run_unready_cycle(self, coordinator: HSEMDataUpdateCoordinator) -> None:
        with (
            _patch_all_ha_helpers(),
            patch(
                f"{_CYCLE_MODULE}.populate_avg_house_consumption_from_snapshot",
                return_value=False,
            ),
        ):
            await coordinator._async_run_update_cycle()

    @pytest.mark.asyncio
    async def test_forced_ev_gets_a_positive_ocpp_target(self) -> None:
        coordinator, published, server = self._coordinator(force=True)

        await self._run_unready_cycle(coordinator)

        data = published[-1]
        assert data.plan_explanation.winner_name == "safety_hold"
        current = data.hourly_recommendation
        assert current is not None
        assert current.ev_charger_calculated_power == pytest.approx(_CHARGER_W)
        assert current.batteries_charged_kwh == pytest.approx(0.0)
        assert current.batteries_discharged_kwh == pytest.approx(0.0)
        assert data.state == _EV
        server.update_charge_target.assert_awaited_once()
        call = server.update_charge_target.await_args
        assert call is not None
        assert call.args[1] == pytest.approx(_CHARGER_KW)
        assert call.kwargs["max_current_a"] > 0
        assert call.kwargs["managed"] is True

    @pytest.mark.asyncio
    async def test_force_off_keeps_the_enforced_zero_ocpp_target(self) -> None:
        coordinator, published, server = self._coordinator(force=False)

        await self._run_unready_cycle(coordinator)

        data = published[-1]
        assert data.state == _WAIT
        current = data.hourly_recommendation
        assert current is not None
        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        call = server.update_charge_target.await_args
        assert call is not None
        assert call.args[1] == pytest.approx(0.0)
        assert call.kwargs["max_current_a"] == 0
        assert call.kwargs["managed"] is True

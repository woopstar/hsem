"""Regression tests for issue #1325 — auto-full EV on a negative price.

``hsem_ev_auto_full_negative_price`` charges the primary EV at full power
while the import price is at or below zero. It had two bugs:

- It fired on an *unavailable* price, because the state collector stores
  ``0.0`` for an unavailable sensor and auto-full only checked ``<= 0.0``.
- The load-forecast hold zeroed its command, and the non-planner hold path
  never re-applied it.

Auto-full now runs inside :func:`apply_force_charge_overrides`, after the
hold on both paths, and only on an available price.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.coordinator_ev_overrides import (
    apply_force_charge_overrides,
)
from custom_components.hsem.coordinator_helpers import apply_load_forecast_hold
from custom_components.hsem.custom_sensors.hourly_data_populator.consumption import (
    ConsumptionPopulation,
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
# 11.0 kW three-phase is the 16 A whole-amp nameplate (issue #1112).
_CHARGER_W = 16 * 3 * 230.0
_REMAINING_H = 10.0 / 60.0
_PRICE_SENSOR = "sensor.energi_data_service"
_PLANNER_MODULE = "custom_components.hsem.coordinator_planner_phase"
_LOAD_FORECAST_MODULE = "custom_components.hsem.coordinator_load_forecast"

# 3 x 25 A x 230 V = 17.25 kW fuse budget, above one 11 kW charger.
_EV_OPTIONS: dict[str, Any] = {
    "hsem_ev_planned_load_enabled": True,
    "hsem_ev_smart_charging": True,
    "hsem_ev_planned_load_charger_power_kw": 11.0,
    "hsem_ev_planned_load_charger_phase_topology": "three_phase_balanced",
    "hsem_main_fuse_amps": 25,
    "hsem_main_fuse_phases": 3,
}


def _rec(start: datetime, *, import_price: float = 2.0) -> HourlyRecommendation:
    """Return a 15-minute grid-charge slot with no EV command."""
    return HourlyRecommendation(
        start=start,
        end=start + _SLOT,
        recommendation=Recommendations.BatteriesChargeGrid.value,
        avg_house_consumption_kwh=0.2,
        avg_house_consumption_1d_kwh=0.2,
        avg_house_consumption_3d_kwh=0.2,
        avg_house_consumption_7d_kwh=0.2,
        avg_house_consumption_14d_kwh=0.2,
        batteries_charged_kwh=1.0,
        batteries_discharged_kwh=0.0,
        estimated_battery_capacity_kwh=0.0,
        estimated_battery_soc_pct=0.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.0,
        export_price=0.0,
        grid_export_kwh=0.0,
        grid_import_kwh=1.0,
        import_price=import_price,
        solcast_pv_estimate_kwh=0.0,
    )


def _config_entry(*, auto_full: bool = True, force: bool = False) -> MagicMock:
    """Return a config entry with a managed primary EV."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {
        **_EV_OPTIONS,
        "hsem_ev_auto_full_negative_price": auto_full,
        "hsem_ev_force_charge_now": force,
        "hsem_ev_second_force_charge_now": False,
    }
    entry.data = {}
    return entry


def _live(price: float, *, available: bool = True) -> LiveState:
    """Return live state with the primary EV plugged in at *price*."""
    live = LiveState(
        force_working_mode_state="auto",
        import_electricity_price=price,
        import_electricity_price_available=available,
    )
    live.ev.is_connected = True
    live.ev_planned_load_connected = True
    return live


def _override(
    entry: MagicMock,
    live: LiveState,
    *,
    hold: bool,
    import_price: float = 2.0,
) -> HourlyRecommendation:
    """Optionally apply the strict hold, then the overrides; return the slot."""
    recs = [_rec(_SLOT_START, import_price=import_price)]
    if hold:
        assert (
            apply_load_forecast_hold(recs, live, _NOW, load_forecast_ready=False)
            is recs[0]
        )
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


class TestAutoFullOverride:
    """The helper applied by both hold sites."""

    @pytest.mark.parametrize("price", [0.0, -0.25])
    def test_available_non_positive_price_charges_at_nameplate(
        self, price: float
    ) -> None:
        current = _override(_config_entry(), _live(price), hold=False)

        assert current.ev_charger_calculated_power == pytest.approx(_CHARGER_W)
        assert current.ev_second_charger_calculated_power == pytest.approx(0.0)
        assert current.recommendation == _EV

    @pytest.mark.parametrize("hold", [False, True])
    def test_unavailable_price_writes_no_command(self, hold: bool) -> None:
        """An unavailable price reads 0.0 but never fires auto-full."""
        current = _override(_config_entry(), _live(0.0, available=False), hold=hold)

        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        assert current.ev_total_planned_load_kwh == pytest.approx(0.0)
        assert current.recommendation != _EV

    def test_positive_price_does_nothing(self) -> None:
        current = _override(_config_entry(), _live(0.01), hold=False)

        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        assert current.recommendation == Recommendations.BatteriesChargeGrid.value
        assert current.grid_import_kwh == pytest.approx(1.0)

    def test_switch_off_does_nothing(self) -> None:
        current = _override(_config_entry(auto_full=False), _live(-1.0), hold=False)

        assert current.ev_charger_calculated_power == pytest.approx(0.0)

    def test_disconnected_ev_is_never_energised(self) -> None:
        live = _live(-1.0)
        live.ev.is_connected = False

        current = _override(_config_entry(), live, hold=False)

        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        assert current.ev_total_planned_load_kwh == pytest.approx(0.0)

    def test_survives_the_hold_with_the_battery_held(self) -> None:
        current = _override(_config_entry(), _live(-0.1), hold=True, import_price=-0.1)

        assert current.ev_charger_calculated_power == pytest.approx(_CHARGER_W)
        assert current.recommendation == _EV
        assert current.batteries_charged_kwh == pytest.approx(0.0)
        assert current.batteries_discharged_kwh == pytest.approx(0.0)

    def test_slot_accounting_stays_coherent(self) -> None:
        """Invariant 13: EV energy, grid import and cost move with the command."""
        current = _override(_config_entry(), _live(-0.1), hold=True, import_price=-0.1)

        ev_kwh = _CHARGER_W * _REMAINING_H / 1000.0
        assert current.ev_total_planned_load_kwh == pytest.approx(ev_kwh, abs=1e-3)
        assert current.ev_planned_load_kwh + current.ev_accounted_load_kwh == (
            pytest.approx(current.ev_total_planned_load_kwh, abs=1e-3)
        )
        # The hold cleared grid flow, so the grid now carries only the EV.
        assert current.grid_import_kwh == pytest.approx(
            current.ev_planned_load_kwh, abs=1e-3
        )
        assert current.estimated_net_consumption_kwh == pytest.approx(
            current.avg_house_consumption_kwh
            + current.ev_planned_load_kwh
            - current.solcast_pv_estimate_kwh,
            abs=1e-3,
        )
        assert current.estimated_cost_currency == pytest.approx(
            current.grid_import_kwh * current.import_price, abs=1e-4
        )

    def test_with_force_charge_gives_the_same_command(self) -> None:
        both = _override(_config_entry(force=True), _live(-0.1), hold=False)
        force_only = _override(
            _config_entry(auto_full=False, force=True), _live(-0.1), hold=False
        )

        assert both.ev_charger_calculated_power == pytest.approx(_CHARGER_W)
        assert both.ev_charger_calculated_power == pytest.approx(
            force_only.ev_charger_calculated_power
        )
        assert both.ev_total_planned_load_kwh == pytest.approx(
            force_only.ev_total_planned_load_kwh
        )


def _planner_coordinator(tmp_path: Path) -> HSEMDataUpdateCoordinator:
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
    coordinator = make_real_coordinator(config_entry=_config_entry(), hass=hass)
    coordinator._last_plan_ev_connected = True
    coordinator._hourly_recommendations = [
        _rec(_SLOT_START),
        _rec(_SLOT_START + _SLOT),
    ]
    return coordinator


class TestPlannerPhaseHold:
    """Inside the planner phase, auto-full now runs after the hold (8d)."""

    @pytest.mark.asyncio
    async def test_unready_forecast_keeps_the_auto_full_command(
        self, tmp_path: Path
    ) -> None:
        coordinator = _planner_coordinator(tmp_path)

        with patch(
            f"{_PLANNER_MODULE}.build_planner_input",
            MagicMock(return_value=PlannerInput()),
        ):
            state, _fresh, _output = await coordinator._run_planner_phase(
                _NOW, _live(-0.1), coordinator._cfg, None, False, 0
            )

        current = coordinator._hourly_recommendation
        assert current is not None
        assert state == _EV
        assert current.ev_charger_calculated_power == pytest.approx(_CHARGER_W)
        assert current.batteries_charged_kwh == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_unavailable_price_holds_at_zero(self, tmp_path: Path) -> None:
        coordinator = _planner_coordinator(tmp_path)

        with patch(
            f"{_PLANNER_MODULE}.build_planner_input",
            MagicMock(return_value=PlannerInput()),
        ):
            state, _fresh, _output = await coordinator._run_planner_phase(
                _NOW, _live(0.0, available=False), coordinator._cfg, None, False, 0
            )

        current = coordinator._hourly_recommendation
        assert current is not None
        assert state == _WAIT
        assert current.ev_charger_calculated_power == pytest.approx(0.0)


class TestNonPlannerCycleHold:
    """With the forecast not ready the planner is skipped; auto-full still wins."""

    @staticmethod
    async def _run_unready_cycle(
        price_state: str,
    ) -> tuple[CoordinatorData, MagicMock]:
        entry = make_fake_config_entry(
            {
                "hsem_read_only": True,
                "hsem_ocpp_enabled": True,
                **_EV_OPTIONS,
                "hsem_ev_auto_full_negative_price": True,
            }
        )
        coordinator = make_real_coordinator(
            hass=make_fake_hass({**_BASE_ENTITY_STATES, _PRICE_SENSOR: price_state}),
            config_entry=entry,
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
        with (
            _patch_all_ha_helpers(),
            patch(
                f"{_LOAD_FORECAST_MODULE}.populate_avg_house_consumption_from_snapshot",
                return_value=ConsumptionPopulation(ok=False),
            ),
        ):
            await coordinator._async_run_update_cycle()
        return published[-1], server

    @pytest.mark.asyncio
    async def test_negative_price_gets_a_positive_ocpp_target(self) -> None:
        data, server = await self._run_unready_cycle("-0.10")

        assert data.plan_explanation.winner_name == "safety_hold"
        current = data.hourly_recommendation
        assert current is not None
        assert data.state == _EV
        assert current.ev_charger_calculated_power == pytest.approx(_CHARGER_W)
        assert current.batteries_charged_kwh == pytest.approx(0.0)
        assert current.batteries_discharged_kwh == pytest.approx(0.0)
        call = server.update_charge_target.await_args
        assert call is not None
        assert call.args[1] == pytest.approx(_CHARGER_W / 1000.0)

    @pytest.mark.asyncio
    async def test_unavailable_price_keeps_the_enforced_zero(self) -> None:
        data, server = await self._run_unready_cycle("unavailable")

        assert data.plan_explanation.winner_name == "safety_hold"
        current = data.hourly_recommendation
        assert current is not None
        assert data.state == _WAIT
        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        call = server.update_charge_target.await_args
        assert call is not None
        assert call.args[1] == pytest.approx(0.0)

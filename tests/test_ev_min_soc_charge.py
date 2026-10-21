"""Tests for issue #1323 — charge immediately below a minimum EV SoC.

With ``hsem_ev_min_soc_charge_enabled`` on, an EV whose SoC is below
``hsem_ev_min_soc_charge_soc`` charges at its full fuse-limited nameplate in
the current slot, whatever the price, deadline, target or smart-charging
switch. It is a runtime override applied by
:func:`apply_force_charge_overrides`, after the load-forecast hold on both
the planner path and the non-planner hold path.
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
from custom_components.hsem.coordinator_helpers import (
    apply_load_forecast_hold,
    ev_site_power_budget_w,
)
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
_GRID = Recommendations.BatteriesChargeGrid.value
# 11.0 kW three-phase is the 16 A whole-amp nameplate (issue #1112).
_CHARGER_W = 16 * 3 * 230.0
# 7.0 kW three-phase is the 10 A whole-amp nameplate.
_SECOND_CHARGER_W = 10 * 3 * 230.0
_REMAINING_H = 10.0 / 60.0
_SOC_SENSOR = "sensor.ev_battery_level"
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
_SECOND_EV_OPTIONS: dict[str, Any] = {
    "hsem_ev_second_planned_load_enabled": True,
    "hsem_ev_second_smart_charging": True,
    "hsem_ev_second_planned_load_charger_power_kw": 7.0,
    "hsem_ev_second_planned_load_charger_phase_topology": "three_phase_balanced",
}


def _rec(start: datetime) -> HourlyRecommendation:
    """Return a 15-minute grid-charge slot with no EV command."""
    return HourlyRecommendation(
        start=start,
        end=start + _SLOT,
        recommendation=_GRID,
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
        import_price=2.0,
        solcast_pv_estimate_kwh=0.0,
    )


def _config_entry(
    *,
    enabled: bool = True,
    threshold: int = 20,
    second: bool = False,
    second_enabled: bool = False,
    second_threshold: int = 20,
    **extra: Any,
) -> MagicMock:
    """Return a config entry with a managed primary (and optional second) EV."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {
        **_EV_OPTIONS,
        **(_SECOND_EV_OPTIONS if second else {}),
        "hsem_ev_min_soc_charge_enabled": enabled,
        "hsem_ev_min_soc_charge_soc": threshold,
        "hsem_ev_second_min_soc_charge_enabled": second_enabled,
        "hsem_ev_second_min_soc_charge_soc": second_threshold,
        **extra,
    }
    entry.data = {}
    return entry


def _live(
    soc: float | None,
    *,
    effective_soc: float | None = None,
    second_soc: float | None = None,
) -> LiveState:
    """Return live state for an automatic-mode site with the EV(s) plugged in."""
    live = LiveState(force_working_mode_state="auto")
    live.ev.is_connected = True
    live.ev_planned_load_connected = True
    live.ev_planned_load_current_soc_pct = soc
    live.ev.effective_soc_pct = effective_soc
    live.ev_second.is_connected = True
    live.ev_second_planned_load_connected = True
    live.ev_second_planned_load_current_soc_pct = second_soc
    return live


def _override(
    entry: MagicMock, live: LiveState, *, hold: bool = False
) -> HourlyRecommendation:
    """Optionally apply the strict hold, then the overrides; return the slot."""
    recs = [_rec(_SLOT_START)]
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
        was_second_connected=True,
    )
    return recs[0]


class TestLivePlanningSoc:
    """The SoC the override reads is the one the planner reads."""

    def test_effective_soc_wins_over_the_raw_reading(self) -> None:
        live = _live(10.0, effective_soc=25.0)

        assert live.ev_planning_soc_pct(is_second=False) == pytest.approx(25.0)

    def test_raw_reading_is_the_fallback(self) -> None:
        live = _live(10.0, second_soc=30.0)

        assert live.ev_planning_soc_pct(is_second=False) == pytest.approx(10.0)
        assert live.ev_planning_soc_pct(is_second=True) == pytest.approx(30.0)

    def test_unknown_stays_none(self) -> None:
        assert _live(None).ev_planning_soc_pct(is_second=False) is None


class TestMinSocOverride:
    """The override applied by both hold sites."""

    @pytest.mark.parametrize("soc", [0.0, 5.0, 19.9])
    def test_below_threshold_charges_at_nameplate(self, soc: float) -> None:
        current = _override(_config_entry(), _live(soc))

        assert current.ev_charger_calculated_power == pytest.approx(_CHARGER_W)
        assert current.ev_second_charger_calculated_power == pytest.approx(0.0)
        assert current.recommendation == _EV

    @pytest.mark.parametrize("soc", [20.0, 20.5, 80.0])
    def test_at_or_above_threshold_does_nothing(self, soc: float) -> None:
        current = _override(_config_entry(), _live(soc))

        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        assert current.recommendation == _GRID
        assert current.grid_import_kwh == pytest.approx(1.0)

    def test_effective_soc_at_threshold_stops_a_stale_raw_reading(self) -> None:
        """The delivered-energy credit stops it even if the car API lags."""
        current = _override(_config_entry(), _live(12.0, effective_soc=20.0))

        assert current.ev_charger_calculated_power == pytest.approx(0.0)

    def test_unknown_soc_never_fires(self) -> None:
        current = _override(_config_entry(), _live(None))

        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        assert current.recommendation == _GRID

    def test_disconnected_ev_is_never_energised(self) -> None:
        live = _live(5.0)
        live.ev.is_connected = False

        current = _override(_config_entry(), live)

        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        assert current.ev_total_planned_load_kwh == pytest.approx(0.0)

    def test_non_numeric_threshold_never_fires(self) -> None:
        entry = _config_entry(hsem_ev_min_soc_charge_soc="not a number")

        current = _override(entry, _live(5.0))

        assert current.ev_charger_calculated_power == pytest.approx(0.0)

    def test_feature_off_does_nothing(self) -> None:
        current = _override(_config_entry(enabled=False), _live(5.0))

        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        assert current.recommendation == _GRID

    def test_needs_the_planned_load_feature(self) -> None:
        entry = _config_entry(hsem_ev_planned_load_enabled=False)

        current = _override(entry, _live(5.0))

        assert current.ev_charger_calculated_power == pytest.approx(0.0)

    def test_fires_with_smart_charging_off(self) -> None:
        entry = _config_entry(hsem_ev_smart_charging=False)
        live = _live(5.0)
        live.ev_planned_load_smart_charging_enabled = False

        current = _override(entry, live)

        assert current.ev_charger_calculated_power == pytest.approx(_CHARGER_W)

    def test_second_ev_only(self) -> None:
        entry = _config_entry(enabled=False, second=True, second_enabled=True)

        current = _override(entry, _live(5.0, second_soc=10.0))

        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        assert current.ev_second_charger_calculated_power == pytest.approx(
            _SECOND_CHARGER_W
        )

    def test_each_ev_uses_its_own_threshold(self) -> None:
        entry = _config_entry(
            threshold=20, second=True, second_enabled=True, second_threshold=50
        )

        current = _override(entry, _live(30.0, second_soc=30.0))

        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        assert current.ev_second_charger_calculated_power == pytest.approx(
            _SECOND_CHARGER_W
        )

    def test_both_evs_share_the_fuse_budget(self) -> None:
        """11 040 W + 6 900 W exceeds the 17 250 W fuse; the second EV is clamped."""
        entry = _config_entry(second=True, second_enabled=True)
        live = _live(5.0, second_soc=5.0)

        current = _override(entry, live)

        budget_w = ev_site_power_budget_w(entry, live)
        assert budget_w < _CHARGER_W + _SECOND_CHARGER_W
        assert current.ev_charger_calculated_power == pytest.approx(_CHARGER_W)
        assert current.ev_second_charger_calculated_power == pytest.approx(
            round(budget_w - _CHARGER_W)
        )

    def test_survives_the_hold_with_coherent_accounting(self) -> None:
        """Invariant 13 on a held slot: EV energy, grid and cost move together."""
        current = _override(_config_entry(), _live(5.0), hold=True)

        assert current.ev_charger_calculated_power == pytest.approx(_CHARGER_W)
        assert current.recommendation == _EV
        assert current.batteries_charged_kwh == pytest.approx(0.0)
        assert current.batteries_discharged_kwh == pytest.approx(0.0)
        ev_kwh = _CHARGER_W * _REMAINING_H / 1000.0
        assert current.ev_total_planned_load_kwh == pytest.approx(ev_kwh, abs=1e-3)
        assert current.grid_import_kwh == pytest.approx(
            current.ev_planned_load_kwh, abs=1e-3
        )
        assert current.estimated_net_consumption_kwh == pytest.approx(
            current.avg_house_consumption_kwh + current.ev_planned_load_kwh,
            abs=1e-3,
        )
        assert current.estimated_cost_currency == pytest.approx(
            current.grid_import_kwh * current.import_price, abs=1e-4
        )

    def test_with_force_charge_gives_the_same_command(self) -> None:
        both = _override(_config_entry(hsem_ev_force_charge_now=True), _live(5.0))
        force_only = _override(
            _config_entry(enabled=False, hsem_ev_force_charge_now=True), _live(5.0)
        )

        assert both.ev_charger_calculated_power == pytest.approx(
            force_only.ev_charger_calculated_power
        )


def _planner_coordinator(tmp_path: Path) -> HSEMDataUpdateCoordinator:
    """Return a real coordinator whose planner yields a zero-EV grid-charge plan."""
    output = PlannerOutput(
        slots=[
            PlannedSlot(
                start=_SLOT_START + i * _SLOT,
                end=_SLOT_START + (i + 1) * _SLOT,
                recommendation=_GRID,
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
    coordinator._hourly_recommendations = [_rec(_SLOT_START), _rec(_SLOT_START + _SLOT)]
    return coordinator


class TestPlannerPhase:
    """Inside the planner phase the override runs at 8d, after the hold."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("consumption_ok", "expected_state"), [(True, _EV), (False, _EV)]
    )
    async def test_below_threshold_charges(
        self, tmp_path: Path, consumption_ok: bool, expected_state: str
    ) -> None:
        coordinator = _planner_coordinator(tmp_path)

        with patch(
            f"{_PLANNER_MODULE}.build_planner_input",
            MagicMock(return_value=PlannerInput()),
        ):
            state, _fresh, _output = await coordinator._run_planner_phase(
                _NOW, _live(5.0), coordinator._cfg, None, consumption_ok, 0
            )

        current = coordinator._hourly_recommendation
        assert current is not None
        assert state == expected_state
        assert current.ev_charger_calculated_power == pytest.approx(_CHARGER_W)

    @pytest.mark.asyncio
    async def test_unknown_soc_holds_at_zero(self, tmp_path: Path) -> None:
        coordinator = _planner_coordinator(tmp_path)

        with patch(
            f"{_PLANNER_MODULE}.build_planner_input",
            MagicMock(return_value=PlannerInput()),
        ):
            state, _fresh, _output = await coordinator._run_planner_phase(
                _NOW, _live(None), coordinator._cfg, None, False, 0
            )

        current = coordinator._hourly_recommendation
        assert current is not None
        assert state == _WAIT
        assert current.ev_charger_calculated_power == pytest.approx(0.0)


class TestNonPlannerCycleHold:
    """With the forecast not ready the planner is skipped; the override still wins."""

    @staticmethod
    async def _run_unready_cycle(soc_state: str) -> tuple[CoordinatorData, MagicMock]:
        entry = make_fake_config_entry(
            {
                "hsem_read_only": True,
                "hsem_ocpp_enabled": True,
                **_EV_OPTIONS,
                "hsem_ev_soc": _SOC_SENSOR,
                "hsem_ev_min_soc_charge_enabled": True,
                "hsem_ev_min_soc_charge_soc": 20,
            }
        )
        coordinator = make_real_coordinator(
            hass=make_fake_hass({**_BASE_ENTITY_STATES, _SOC_SENSOR: soc_state}),
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
    async def test_below_threshold_gets_a_positive_ocpp_target(self) -> None:
        data, server = await self._run_unready_cycle("8")

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
    @pytest.mark.parametrize("soc_state", ["unavailable", "45"])
    async def test_unknown_or_high_soc_keeps_the_enforced_zero(
        self, soc_state: str
    ) -> None:
        data, server = await self._run_unready_cycle(soc_state)

        assert data.plan_explanation.winner_name == "safety_hold"
        current = data.hourly_recommendation
        assert current is not None
        assert data.state == _WAIT
        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        call = server.update_charge_target.await_args
        assert call is not None
        assert call.args[1] == pytest.approx(0.0)

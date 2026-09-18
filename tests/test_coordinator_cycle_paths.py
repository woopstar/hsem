"""Tests for branches of the coordinator update cycle (``coordinator_cycle``).

These run real cycles against the mock HA harness from
``test_ha_mock_integration`` on a coordinator built through its real
``__init__``. They cover the timed-override expiry, the forced-mode and
not-ready-consumption branches, OCPP state packaging and target pushes, and
the stale/failed cycle guards that must leave the accepted plan untouched.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.coordinator_helpers import LoadForecastReadiness
from custom_components.hsem.custom_sensors.ocpp_flap_state import FlapState
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.utils.datetime_utils import now as hsem_now
from custom_components.hsem.utils.recommendations import Recommendations
from tests.coordinator_fixtures import make_real_coordinator
from tests.test_ha_mock_integration import (
    _BASE_ENTITY_STATES,
    _patch_all_ha_helpers,
    make_fake_config_entry,
    make_fake_hass,
)

_MODULE = "custom_components.hsem.coordinator_cycle"
_FORCE_MODE_ENTITY = "select.hsem_force_working_mode"


def _services_mock(coordinator: HSEMDataUpdateCoordinator) -> MagicMock:
    """Return the fake ``hass.services`` the harness installed."""
    return cast(MagicMock, coordinator.hass.services)


def _coordinator(
    overrides: dict[str, Any] | None = None,
    *,
    states: dict[str, str | dict] | None = None,
) -> tuple[HSEMDataUpdateCoordinator, list[CoordinatorData]]:
    """Return a real coordinator plus the list of snapshots it publishes."""
    config_entry = make_fake_config_entry({"hsem_read_only": True, **(overrides or {})})
    hass = make_fake_hass({**_BASE_ENTITY_STATES, **(states or {})})
    coordinator = make_real_coordinator(hass=hass, config_entry=config_entry)
    published: list[CoordinatorData] = []
    coordinator.async_set_updated_data = published.append  # type: ignore[method-assign, assignment]  # test monkey-patch
    return coordinator, published


async def _run_cycle(coordinator: HSEMDataUpdateCoordinator) -> None:
    """Run one full update cycle with the HA helpers patched out."""
    with _patch_all_ha_helpers():
        await coordinator._async_run_update_cycle()


class TestTimedOverrideExpiry:
    """A timed manual override clears itself once it expires (issue #317)."""

    @pytest.mark.asyncio
    async def test_expired_override_resets_the_select_to_auto(self) -> None:
        """The select entity is written back to ``auto`` exactly once."""
        coordinator, _published = _coordinator(
            states={_FORCE_MODE_ENTITY: "batteries_charge_grid"}
        )
        coordinator._force_working_mode_entity = _FORCE_MODE_ENTITY
        coordinator._override_expiry = hsem_now() - timedelta(minutes=1)

        await _run_cycle(coordinator)

        _services_mock(coordinator).async_call.assert_awaited_once_with(
            "select",
            "select_option",
            {"entity_id": _FORCE_MODE_ENTITY, "option": "auto"},
            blocking=True,
        )
        assert coordinator._override_expiry is None
        assert coordinator._live is not None
        assert coordinator._live.force_working_mode_state == "auto"

    @pytest.mark.asyncio
    async def test_override_cleared_by_hand_drops_the_expiry(self) -> None:
        """Switching back to ``auto`` early stops the expiry being tracked."""
        coordinator, _published = _coordinator(states={_FORCE_MODE_ENTITY: "auto"})
        coordinator._force_working_mode_entity = _FORCE_MODE_ENTITY
        coordinator._override_expiry = hsem_now() + timedelta(hours=1)

        await _run_cycle(coordinator)

        assert coordinator._override_expiry is None
        _services_mock(coordinator).async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unexpired_override_is_kept(self) -> None:
        """A still-forced override survives the cycle untouched."""
        coordinator, _published = _coordinator(
            states={_FORCE_MODE_ENTITY: "batteries_charge_grid"}
        )
        coordinator._force_working_mode_entity = _FORCE_MODE_ENTITY
        expiry = hsem_now() + timedelta(hours=1)
        coordinator._override_expiry = expiry

        await _run_cycle(coordinator)

        assert coordinator._override_expiry == expiry
        _services_mock(coordinator).async_call.assert_not_awaited()


class TestWorkingStateSelection:
    """The published state reflects forced mode and data readiness."""

    @pytest.mark.asyncio
    async def test_forced_mode_is_published_verbatim(self) -> None:
        """A user-forced working mode bypasses the planner."""
        coordinator, published = _coordinator(
            states={_FORCE_MODE_ENTITY: "batteries_charge_grid"}
        )
        coordinator._force_working_mode_entity = _FORCE_MODE_ENTITY

        await _run_cycle(coordinator)

        assert published[-1].state == "batteries_charge_grid"

    @pytest.mark.asyncio
    async def test_unready_consumption_publishes_a_strict_hold(self) -> None:
        """An unusable load forecast publishes a safety hold, not a plan."""
        coordinator, published = _coordinator()

        with patch(
            f"{_MODULE}.populate_avg_house_consumption_from_snapshot",
            return_value=False,
        ):
            await _run_cycle(coordinator)

        assert published[-1].state == Recommendations.BatteriesWaitMode.value
        assert coordinator._last_load_forecast_readiness_reason is not None
        assert coordinator._load_forecast_recovery_replan_pending is True
        assert published[-1].plan_explanation.winner_name == "safety_hold"

    @pytest.mark.asyncio
    async def test_recovered_forecast_is_logged_once(self) -> None:
        """Recovery from a hold is announced so the replan is traceable."""
        coordinator, _published = _coordinator()
        coordinator._last_load_forecast_readiness_reason = "zero_forecast"
        log = MagicMock()
        ready = LoadForecastReadiness(ready=True, reason=None, signature=())

        with (
            patch(f"{_MODULE}.assess_load_forecast", return_value=ready),
            patch(f"{_MODULE}.async_log", log),
        ):
            await _run_cycle(coordinator)

        assert any(
            call.args[1] == "[load] Forecast recovered (%s); a fresh plan is required."
            for call in log.call_args_list
        )
        assert coordinator._last_load_forecast_readiness_reason is None


class TestOcppStatePackaging:
    """Running OCPP servers surface their state on every snapshot."""

    @staticmethod
    def _server(port_label: str) -> MagicMock:
        """Return a mock OCPP server with distinguishable state."""
        server = MagicMock()
        server.charger_sessions = {f"charger_{port_label}": object()}
        server.is_listening = True
        server.last_requested_current_a = 16
        server.anti_flap_state = FlapState.Charging.value
        server.is_stalled = True
        server.update_charge_target = AsyncMock()
        return server

    @pytest.mark.asyncio
    async def test_no_servers_publishes_idle_defaults(self) -> None:
        """Without OCPP the snapshot carries inert defaults."""
        coordinator, published = _coordinator()

        await _run_cycle(coordinator)

        data = published[-1]
        assert data.ocpp_chargers is None
        assert data.ocpp_listening is False
        assert data.ocpp_last_requested_current_a is None
        assert data.ocpp_anti_flap_state == FlapState.Idle.value
        assert data.ocpp_charger_stalled is False

    @pytest.mark.asyncio
    async def test_both_servers_are_published_and_pushed_targets(self) -> None:
        """Each EV's server state is published and receives its own target."""
        coordinator, published = _coordinator(
            {
                "hsem_ocpp_enabled": True,
                "hsem_ocpp_second_enabled": True,
                "hsem_ev_planned_load_enabled": True,
                "hsem_ev_second_planned_load_enabled": True,
            }
        )
        primary = self._server("primary")
        second = self._server("second")
        coordinator._ocpp_server = primary
        coordinator._ocpp_second_server = second

        await _run_cycle(coordinator)

        data = published[-1]
        assert data.ocpp_chargers == primary.charger_sessions
        assert data.ocpp_listening is True
        assert data.ocpp_last_requested_current_a == 16
        assert data.ocpp_anti_flap_state == FlapState.Charging.value
        assert data.ocpp_charger_stalled is True
        assert data.ocpp_second_chargers == second.charger_sessions
        assert data.ocpp_second_listening is True
        primary.update_charge_target.assert_awaited_once()
        second.update_charge_target.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disabled_ocpp_never_pushes_a_target(self) -> None:
        """A server object with OCPP disabled in config stays idle."""
        coordinator, _published = _coordinator({"hsem_ocpp_enabled": False})
        primary = self._server("primary")
        coordinator._ocpp_server = primary

        await _run_cycle(coordinator)

        primary.update_charge_target.assert_not_awaited()


class TestCycleGuards:
    """A stale or failed cycle must never publish or corrupt the plan."""

    @pytest.mark.asyncio
    async def test_state_event_mid_cycle_discards_the_snapshot(self) -> None:
        """A newer generation appearing mid-cycle suppresses publication."""
        coordinator, published = _coordinator()
        accepted = PlannerOutput()
        coordinator._last_planner_output = accepted

        def _bump(**_kwargs: Any) -> tuple[None, bool]:
            coordinator._update_generation += 1
            return None, False

        with patch(f"{_MODULE}.accumulate_forecast_actuals", side_effect=_bump):
            await _run_cycle(coordinator)

        assert published == []
        assert coordinator._last_planner_output is accepted

    @pytest.mark.asyncio
    async def test_failure_restores_the_accepted_plan(self) -> None:
        """A raising cycle surfaces ``UpdateFailed`` and rolls state back."""
        coordinator, published = _coordinator()
        accepted = PlannerOutput()
        coordinator._last_planner_output = accepted

        with (
            patch(
                f"{_MODULE}.accumulate_forecast_actuals",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(UpdateFailed, match="HSEM update cycle failed"),
        ):
            await _run_cycle(coordinator)

        assert published == []
        assert coordinator._last_planner_output is accepted

    @pytest.mark.asyncio
    async def test_cancellation_restores_the_accepted_plan(self) -> None:
        """Cancellation propagates untouched after rolling state back."""
        coordinator, published = _coordinator()
        accepted = PlannerOutput()
        coordinator._last_planner_output = accepted

        with (
            patch(
                f"{_MODULE}.accumulate_forecast_actuals",
                side_effect=asyncio.CancelledError,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await _run_cycle(coordinator)

        assert published == []
        assert coordinator._last_planner_output is accepted

    @pytest.mark.asyncio
    async def test_new_prediction_record_is_persisted(self) -> None:
        """A freshly scored slot triggers a prediction-history write."""
        coordinator, _published = _coordinator()
        persist = AsyncMock()

        with (
            patch(
                f"{_MODULE}.accumulate_forecast_actuals",
                return_value=(None, True),
            ),
            patch(f"{_MODULE}.persist_all_trackers", persist),
        ):
            await _run_cycle(coordinator)

        assert any(
            call.kwargs.get("only") == ["_prediction_tracker"]
            for call in persist.await_args_list
        )

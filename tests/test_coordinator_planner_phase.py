"""Tests for ``CoordinatorPlannerPhaseMixin._run_planner_phase``.

The planner phase runs on a real coordinator (see
:mod:`tests.coordinator_fixtures`). Only ``build_planner_input`` and the
executor job that runs the MILP are replaced, so the surrounding coordinator
logic — dynamic floor, EV session power, plan reuse, window hysteresis, the
auto-full and load-hold overrides, and current-slot resolution — runs for real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.utils.recommendations import Recommendations
from tests.coordinator_fixtures import make_real_coordinator
from tests.test_coordinator_tracking_forecast import _rec

_MODULE = "custom_components.hsem.coordinator_planner_phase"
_SLOT = timedelta(minutes=15)
_SLOT_START = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_NOW = _SLOT_START + timedelta(minutes=5)
_CHARGE = Recommendations.BatteriesChargeGrid.value
_WAIT = Recommendations.BatteriesWaitMode.value


def _planner_output(**kwargs: Any) -> PlannerOutput:
    """Return a two-slot plan: grid charge now, wait mode next."""
    return PlannerOutput(
        slots=[
            PlannedSlot(
                start=_SLOT_START,
                end=_SLOT_START + _SLOT,
                recommendation=_CHARGE,
                batteries_charged_kwh=1.0,
            ),
            PlannedSlot(
                start=_SLOT_START + _SLOT,
                end=_SLOT_START + 2 * _SLOT,
                recommendation=_WAIT,
            ),
        ],
        **kwargs,
    )


def _recommendations() -> list[HourlyRecommendation]:
    """Return recommendation slots matching :func:`_planner_output`."""
    return [
        _rec(
            _SLOT_START,
            _SLOT_START + _SLOT,
            avg_house_consumption_kwh=0.5,
            solcast_pv_estimate_kwh=0.2,
        ),
        _rec(
            _SLOT_START + _SLOT,
            _SLOT_START + 2 * _SLOT,
            avg_house_consumption_kwh=0.4,
            solcast_pv_estimate_kwh=0.1,
        ),
    ]


def _coordinator(
    tmp_path: Path,
    output: PlannerOutput,
    options: dict[str, Any] | None = None,
) -> tuple[HSEMDataUpdateCoordinator, AsyncMock]:
    """Return a real coordinator whose executor job yields *output*."""
    hass = MagicMock()
    hass.config.config_dir = str(tmp_path)
    executor = AsyncMock(return_value=output)
    hass.async_add_executor_job = executor
    coordinator = make_real_coordinator(options, hass=hass)
    coordinator._hourly_recommendations = _recommendations()
    return coordinator, executor


async def _run_phase(
    coordinator: HSEMDataUpdateCoordinator,
    live: LiveState,
    *,
    consumption_ok: bool = True,
) -> tuple[tuple[str | None, bool, PlannerOutput], MagicMock]:
    """Run the planner phase with ``build_planner_input`` patched."""
    build = MagicMock(return_value=PlannerInput())
    with patch(f"{_MODULE}.build_planner_input", build):
        result = await coordinator._run_planner_phase(
            _NOW, live, coordinator._cfg, None, consumption_ok, 0
        )
    return result, build


class TestFreshPlan:
    """A replan runs the planner and publishes the current slot's decision."""

    @pytest.mark.asyncio
    async def test_current_slot_recommendation_is_published(
        self, tmp_path: Path
    ) -> None:
        """The slot containing ``now`` becomes the working-mode state."""
        output = _planner_output(required_capacity_kwh=3.5, warnings=["low PV"])
        coordinator, executor = _coordinator(tmp_path, output)
        log = MagicMock()

        with patch(f"{_MODULE}.async_log", log):
            (state, fresh, planner_output), _ = await _run_phase(
                coordinator, LiveState()
            )

        executor.assert_awaited_once()
        assert fresh is True
        assert planner_output is output
        assert state == _CHARGE
        assert coordinator._hourly_recommendation is not None
        assert coordinator._hourly_recommendation.start == _SLOT_START
        assert coordinator._current_required_battery == pytest.approx(3.5)
        assert coordinator._last_planner_input is not None
        assert any(
            call.args[1:] == ("[planner] %s", "low PV") for call in log.call_args_list
        )

    @pytest.mark.asyncio
    async def test_dynamic_floor_is_computed_and_passed_to_the_planner(
        self, tmp_path: Path
    ) -> None:
        """With the dynamic floor enabled the planner receives its floor."""
        coordinator, _ = _coordinator(
            tmp_path, _planner_output(), {"hsem_dynamic_discharge_floor": True}
        )
        live = LiveState()
        live.huawei_batteries_soc_pct = 40.0
        live.huawei_batteries_rated_capacity_wh = 10_000.0
        compute_floor = MagicMock(return_value=(12.0, {"reason": "bridge"}))
        correct_margin = MagicMock()

        with (
            patch.object(coordinator._dynamic_floor, "compute_floor", compute_floor),
            patch.object(coordinator._dynamic_floor, "correct_margin", correct_margin),
        ):
            _, build = await _run_phase(coordinator, live)

        assert coordinator._effective_discharge_floor_pct == pytest.approx(12.0)
        assert coordinator._effective_discharge_floor_diag == {"reason": "bridge"}
        assert build.call_args.kwargs["dynamic_discharge_floor_pct"] == (
            pytest.approx(12.0)
        )
        bridge_slots = compute_floor.call_args.kwargs["slots"]
        assert [slot.estimated_net_consumption_kwh for slot in bridge_slots] == [
            pytest.approx(0.3),
            pytest.approx(0.3),
        ]
        correct_margin.assert_called_once_with(40.0, 12.0)

    @pytest.mark.asyncio
    async def test_dynamic_floor_without_live_soc_skips_margin_correction(
        self, tmp_path: Path
    ) -> None:
        """No SoC reading → the floor is computed but not self-corrected."""
        coordinator, _ = _coordinator(
            tmp_path, _planner_output(), {"hsem_dynamic_discharge_floor": True}
        )
        correct_margin = MagicMock()

        with patch.object(coordinator._dynamic_floor, "correct_margin", correct_margin):
            await _run_phase(coordinator, LiveState())

        assert coordinator._effective_discharge_floor_pct is not None
        correct_margin.assert_not_called()

    @pytest.mark.asyncio
    async def test_dynamic_floor_disabled_clears_previous_floor(
        self, tmp_path: Path
    ) -> None:
        """A disabled floor resets any earlier value and passes ``None``."""
        coordinator, _ = _coordinator(tmp_path, _planner_output())
        coordinator._effective_discharge_floor_pct = 30.0
        coordinator._effective_discharge_floor_diag = {"stale": True}

        _, build = await _run_phase(coordinator, LiveState())

        assert coordinator._effective_discharge_floor_pct is None
        assert coordinator._effective_discharge_floor_diag is None
        assert build.call_args.kwargs["dynamic_discharge_floor_pct"] is None

    @pytest.mark.asyncio
    async def test_charging_evs_report_session_power_in_kw(
        self, tmp_path: Path
    ) -> None:
        """Live charger power of each charging EV is passed on in kW."""
        coordinator, _ = _coordinator(tmp_path, _planner_output())
        coordinator._cfg.ev_second_enabled = True
        live = LiveState()
        live.ev.is_charging = True
        live.ev.power_w = 7_400.0
        live.ev_second.is_charging = True
        live.ev_second.power_w = 3_700.0

        _, build = await _run_phase(coordinator, live)

        assert build.call_args.kwargs["ev_session_kw"] == {
            "ev": pytest.approx(7.4),
            "ev_second": pytest.approx(3.7),
        }

    @pytest.mark.asyncio
    async def test_second_ev_session_is_ignored_when_disabled(
        self, tmp_path: Path
    ) -> None:
        """Without a configured second EV its live power is not used."""
        coordinator, _ = _coordinator(tmp_path, _planner_output())
        coordinator._cfg.ev_second_enabled = False
        live = LiveState()
        live.ev_second.is_charging = True
        live.ev_second.power_w = 3_700.0

        _, build = await _run_phase(coordinator, live)

        assert build.call_args.kwargs["ev_session_kw"] is None

    @pytest.mark.asyncio
    async def test_charging_ev_without_planned_load_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """A physically charging EV with no planned EV load logs a warning."""
        coordinator, _ = _coordinator(tmp_path, _planner_output())
        live = LiveState()
        live.ev.is_charging = True
        log = MagicMock()

        with patch(f"{_MODULE}.async_log", log):
            await _run_phase(coordinator, live)

        assert any(
            "no current or future slot has ev_total_planned_load_kwh" in call.args[1]
            for call in log.call_args_list
        )


class TestPlanReuse:
    """Without a material change the accepted plan is reused, not re-solved."""

    @pytest.mark.asyncio
    async def test_reuses_a_copy_of_the_accepted_plan(self, tmp_path: Path) -> None:
        """The planner is skipped and the cached plan is never aliased."""
        accepted = _planner_output(
            required_capacity_kwh=2.0,
            wait_mode_reserve_kwh=1.0,
            ev_held_power_w=4_000.0,
            ev_held_slot_start=_SLOT_START,
        )
        coordinator, executor = _coordinator(tmp_path, PlannerOutput())
        coordinator._last_planner_output = accepted
        coordinator._last_plan_slot_start = _SLOT_START

        with patch.object(coordinator, "_should_replan", MagicMock(return_value=False)):
            (state, fresh, planner_output), build = await _run_phase(
                coordinator, LiveState()
            )

        executor.assert_not_awaited()
        build.assert_not_called()
        assert fresh is False
        assert planner_output is not accepted
        assert planner_output.slots is not accepted.slots
        assert state == _CHARGE
        assert coordinator._current_required_battery == pytest.approx(2.0)
        assert coordinator._current_wait_mode_reserve == pytest.approx(1.0)
        assert coordinator._ev_held_power_w == pytest.approx(4_000.0)
        assert coordinator._ev_held_slot_start == _SLOT_START
        assert coordinator._data_quality.load_forecast_ready is True


class TestWindowHysteresisDisabled:
    """With hysteresis off the current slot is remembered verbatim."""

    @pytest.mark.asyncio
    async def test_current_slot_is_recorded_as_previous(self, tmp_path: Path) -> None:
        """The next cycle compares against this slot's recommendation."""
        coordinator, _ = _coordinator(tmp_path, _planner_output())
        coordinator._cfg.planner_window_hysteresis_minutes = 0

        await _run_phase(coordinator, LiveState())

        assert coordinator._window_hys_previous_rec == _CHARGE
        assert coordinator._window_hys_previous_slot_start == _SLOT_START


class TestPostPlanOverrides:
    """Overrides applied to the published plan after the planner runs."""

    @pytest.mark.asyncio
    async def test_auto_full_ev_applies_on_non_positive_price(
        self, tmp_path: Path
    ) -> None:
        """Auto-full at a negative price overrides the primary EV's power."""
        coordinator, _ = _coordinator(
            tmp_path, _planner_output(), {"hsem_ev_auto_full_negative_price": True}
        )
        live = LiveState()
        live.import_electricity_price = -0.05
        override = MagicMock()

        with patch(f"{_MODULE}.apply_current_ev_power_override", override):
            await _run_phase(coordinator, live)

        override.assert_called_once()
        kwargs = override.call_args.kwargs
        assert kwargs["override_primary"] is True
        assert kwargs["override_second"] is False
        assert kwargs["now"] == _NOW

    @pytest.mark.asyncio
    async def test_auto_full_ev_needs_a_current_slot(self, tmp_path: Path) -> None:
        """Without a slot containing ``now`` there is nothing to override."""
        coordinator, _ = _coordinator(
            tmp_path, _planner_output(), {"hsem_ev_auto_full_negative_price": True}
        )
        coordinator._hourly_recommendations = [
            _rec(_SLOT_START + _SLOT, _SLOT_START + 2 * _SLOT)
        ]
        live = LiveState()
        live.import_electricity_price = 0.0
        override = MagicMock()

        with patch(f"{_MODULE}.apply_current_ev_power_override", override):
            await _run_phase(coordinator, live)

        override.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_full_ev_ignores_positive_prices(self, tmp_path: Path) -> None:
        """A positive import price never triggers auto-full."""
        coordinator, _ = _coordinator(
            tmp_path, _planner_output(), {"hsem_ev_auto_full_negative_price": True}
        )
        live = LiveState()
        live.import_electricity_price = 0.5
        override = MagicMock()

        with patch(f"{_MODULE}.apply_current_ev_power_override", override):
            await _run_phase(coordinator, live)

        override.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsafe_load_forecast_holds_the_current_slot(
        self, tmp_path: Path
    ) -> None:
        """An unusable load forecast replaces the current action with a hold."""
        coordinator, _ = _coordinator(tmp_path, _planner_output())
        log = MagicMock()

        with patch(f"{_MODULE}.async_log", log):
            (state, _, _), _ = await _run_phase(
                coordinator, LiveState(), consumption_ok=False
            )

        assert state == _WAIT
        current = coordinator._hourly_recommendation
        assert current is not None
        assert current.recommendation == _WAIT
        assert current.batteries_charged_kwh == pytest.approx(0.0)
        assert any(
            "holding current slot in batteries_wait_mode" in call.args[1]
            for call in log.call_args_list
        )

    @pytest.mark.asyncio
    async def test_forced_mode_is_not_overridden_by_the_load_hold(
        self, tmp_path: Path
    ) -> None:
        """A user-forced working mode keeps the planner's slot untouched."""
        coordinator, _ = _coordinator(tmp_path, _planner_output())
        live = LiveState()
        live.force_working_mode_state = _CHARGE

        (state, _, _), _ = await _run_phase(coordinator, live, consumption_ok=False)

        assert state == _CHARGE

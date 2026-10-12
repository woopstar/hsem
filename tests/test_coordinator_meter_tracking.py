"""Tests for the per-cycle feeding of the meter-based trackers (issue #1267).

The daily plan-vs-actual, financial and savings trackers used to be fed from
inside the planner phase, which is skipped while a working mode is forced, a
required entity is missing or the load forecast is not ready.  Grid cost of
such a period was dropped as a stale gap, and "missed savings" could never
accrue.  These tests pin that the trackers are fed on every cycle, that what
needs a plan is skipped without one, and that nothing changed for a cycle
that does have a plan.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator_meter_tracking import (
    async_accumulate_meter_trackers,
)
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.coordinator_tracking import (
    accumulate_daily_plan_actuals,
    accumulate_financials,
    accumulate_savings,
)
from custom_components.hsem.models.daily_plan_vs_actual_tracker import (
    DailyPlanVsActualTracker,
)
from custom_components.hsem.models.financial_tracker import FinancialTracker
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.savings_tracker import SavingsTracker
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations
from tests.test_coordinator_cycle_paths import _coordinator, _run_cycle
from tests.test_ha_mock_integration import FakeState

_MODULE = "custom_components.hsem.coordinator_meter_tracking"
_TRACKING = "custom_components.hsem.coordinator_tracking"
_CYCLE = "custom_components.hsem.coordinator_cycle"
_T0 = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
_FORCE_MODE_ENTITY = "select.hsem_force_working_mode"
_FORCED = "batteries_charge_grid"
#: Five-minute cycles, as the integration is configured by default.
_CFG = SensorConfig(update_interval=5)


def _state(tmp_path: Path) -> CoordinatorSharedState:
    """Return the slice of the coordinator the tracker feeding uses."""
    hass = SimpleNamespace(config=SimpleNamespace(config_dir=str(tmp_path)))
    return cast(
        CoordinatorSharedState,
        SimpleNamespace(
            hass=hass,
            _daily_tracker=DailyPlanVsActualTracker(today=_T0.date().isoformat()),
            _financial_tracker=FinancialTracker(today=_T0.date().isoformat()),
            _savings_tracker=SavingsTracker(),
            _daily_plan_last_accumulated=None,
            _hourly_recommendation=None,
        ),
    )


def _live(
    *,
    grid_import: float = 100.0,
    grid_export: float = 20.0,
    force_mode: str = "auto",
    battery_power_w: float | None = None,
) -> LiveState:
    live = LiveState()
    live.force_working_mode_state = force_mode
    live.grid_import_energy_kwh = grid_import
    live.grid_export_energy_kwh = grid_export
    live.import_electricity_price = 2.0
    live.export_electricity_price = 0.5
    live.import_electricity_price_available = True
    live.export_electricity_price_available = True
    live.huawei_batteries_charge_discharge_power_w = battery_power_w
    live.house_consumption_power_w = 0.0
    live.solar_production_power_w = 0.0
    return live


def _plan(now: datetime) -> PlannerOutput:
    """Return a plan whose current slot imports 1 kWh and charges 1 kWh."""
    slot = PlannedSlot(start=now, end=now + timedelta(minutes=15))
    slot.grid_import_kwh = 1.0
    slot.batteries_charged_kwh = 1.0
    return PlannerOutput(slots=[slot])


async def _feed(
    coordinator: CoordinatorSharedState,
    minutes: int,
    live: LiveState,
    planner_output: PlannerOutput | None = None,
) -> None:
    with patch("custom_components.hsem.coordinator_tracking.async_track_time_change"):
        await async_accumulate_meter_trackers(
            coordinator,
            _T0 + timedelta(minutes=minutes),
            live,
            _CFG,
            planner_output,
        )


class TestWithoutAPlan:
    """A cycle in which the planner did not run still counts."""

    @pytest.mark.asyncio
    async def test_grid_cost_of_a_forced_period_is_counted(
        self, tmp_path: Path
    ) -> None:
        """Half an hour of forced grid charging at 2.0 per kWh."""
        coordinator = _state(tmp_path)
        log = MagicMock()

        with patch(f"{_TRACKING}.async_log", log):
            for minutes, meter in ((0, 100.0), (5, 100.5), (10, 101.0), (30, 103.0)):
                await _feed(
                    coordinator,
                    minutes,
                    _live(grid_import=meter, force_mode=_FORCED),
                )

        tracker = coordinator._financial_tracker
        # 0.5 + 0.5 kWh in the first ten minutes.  The 20-minute step is
        # longer than the gap tolerance (2 × the 5-minute interval) and is
        # still skipped: that is the tracker's own rule, not the forced mode.
        assert tracker.import_cost_total == pytest.approx(2.0)
        assert [call.args[0] for call in log.call_args_list] == ["warning"]

    @pytest.mark.asyncio
    async def test_consecutive_forced_cycles_leave_no_gap(self, tmp_path: Path) -> None:
        """No stale-baseline warning while cycles keep running."""
        coordinator = _state(tmp_path)
        log = MagicMock()

        with patch(f"{_TRACKING}.async_log", log):
            for step in range(13):
                await _feed(
                    coordinator,
                    5 * step,
                    _live(grid_import=100.0 + 0.5 * step, force_mode=_FORCED),
                )

        assert coordinator._financial_tracker.import_cost_total == pytest.approx(12.0)
        log.assert_not_called()

    @pytest.mark.asyncio
    async def test_savings_in_a_forced_mode_are_booked_as_missed(
        self, tmp_path: Path
    ) -> None:
        """A 1 kW discharge for 15 minutes at 2.0 per kWh, with the mode forced."""
        coordinator = _state(tmp_path)
        live = _live(force_mode=_FORCED, battery_power_w=-1000.0)

        await _feed(coordinator, 0, live)
        await _feed(coordinator, 15, live)

        savings = coordinator._savings_tracker
        assert savings.today_missed == pytest.approx(0.5)
        assert savings.today_actual == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_savings_in_auto_are_booked_as_actual(self, tmp_path: Path) -> None:
        coordinator = _state(tmp_path)
        live = _live(battery_power_w=-1000.0)

        await _feed(coordinator, 0, live)
        await _feed(coordinator, 15, live)

        savings = coordinator._savings_tracker
        assert savings.today_actual == pytest.approx(0.5)
        assert savings.today_missed == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_the_daily_actuals_accumulate_and_the_plan_side_does_not(
        self, tmp_path: Path
    ) -> None:
        coordinator = _state(tmp_path)
        marker = _T0 - timedelta(minutes=15)
        coordinator._daily_plan_last_accumulated = marker

        await _feed(coordinator, 0, _live(grid_import=100.0, force_mode=_FORCED))
        await _feed(coordinator, 5, _live(grid_import=100.4, force_mode=_FORCED))

        daily = coordinator._daily_tracker
        assert daily.actual.grid_import_kwh == pytest.approx(0.4)
        assert daily.plan.grid_import_kwh == pytest.approx(0.0)
        assert coordinator._daily_plan_last_accumulated == marker

    @pytest.mark.asyncio
    async def test_charge_savings_need_a_plan(self, tmp_path: Path) -> None:
        """A stale charge recommendation earns nothing without this cycle's plan."""
        coordinator = _state(tmp_path)
        coordinator._hourly_recommendation = cast(
            HourlyRecommendation,
            SimpleNamespace(
                recommendation=Recommendations.BatteriesChargeGrid.value,
                batteries_charged_kwh=3.0,
            ),
        )
        # With a plan the same recommendation does earn: the price is below
        # the day's mean.
        with_plan = _state(tmp_path / "with_plan")
        with_plan._hourly_recommendation = coordinator._hourly_recommendation
        dear = _plan(_T0)
        dear.slots[0].price = SlotPrice(5.0, 0.0)
        await _feed(with_plan, 0, _live(), dear)
        assert with_plan._savings_tracker.today_actual == pytest.approx(9.0)

        await _feed(coordinator, 0, _live())
        await _feed(coordinator, 15, _live())

        assert coordinator._savings_tracker.today_actual == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_both_trackers_are_persisted(self, tmp_path: Path) -> None:
        coordinator = _state(tmp_path)

        await _feed(coordinator, 0, _live(force_mode=_FORCED))

        storage = tmp_path / ".storage"
        assert (storage / "hsem_financial_history.json").exists()
        assert (storage / "hsem_savings_history.json").exists()


class TestWithAPlan:
    @pytest.mark.asyncio
    async def test_a_planned_cycle_gives_what_the_planner_phase_used_to(
        self, tmp_path: Path
    ) -> None:
        """Same calls, same order, same result as before the move."""
        new = _state(tmp_path / "new")
        old = _state(tmp_path / "old")
        samples = [
            (0, _live(grid_import=100.0, grid_export=20.0, battery_power_w=-800.0)),
            (5, _live(grid_import=100.3, grid_export=20.2, battery_power_w=-800.0)),
            (10, _live(grid_import=100.9, grid_export=20.2, battery_power_w=500.0)),
        ]

        for minutes, live in samples:
            now = _T0 + timedelta(minutes=minutes)
            await _feed(new, minutes, live, _plan(_T0))
            with patch(f"{_TRACKING}.async_track_time_change"):
                old._daily_plan_last_accumulated = await accumulate_daily_plan_actuals(
                    now=now,
                    live=live,
                    output=_plan(_T0),
                    daily_tracker=old._daily_tracker,
                    daily_plan_last_accumulated=old._daily_plan_last_accumulated,
                    hass=old.hass,
                )
            await accumulate_financials(
                now=now,
                live=live,
                financial_tracker=old._financial_tracker,
                hass=old.hass,
                update_interval_minutes=_CFG.update_interval,
            )
            await accumulate_savings(
                now=now,
                live=live,
                output=_plan(_T0),
                savings_tracker=old._savings_tracker,
                daily_tracker=old._daily_tracker,
                hourly_recommendation=None,
                hass=old.hass,
            )

        assert new._daily_plan_last_accumulated == old._daily_plan_last_accumulated
        assert new._daily_tracker.plan == old._daily_tracker.plan
        assert new._daily_tracker.actual == old._daily_tracker.actual
        assert new._daily_tracker.plan.grid_import_kwh == pytest.approx(1.0)
        assert new._financial_tracker.as_dict() == old._financial_tracker.as_dict()
        assert new._financial_tracker.import_cost_total == pytest.approx(1.8)
        assert new._savings_tracker.as_dict() == old._savings_tracker.as_dict()
        assert new._savings_tracker.today_actual > 0.0

    @pytest.mark.asyncio
    async def test_the_plan_side_accumulates_once_per_slot(
        self, tmp_path: Path
    ) -> None:
        coordinator = _state(tmp_path)

        await _feed(coordinator, 0, _live(), _plan(_T0))
        await _feed(coordinator, 5, _live(), _plan(_T0))

        assert coordinator._daily_tracker.plan.grid_import_kwh == pytest.approx(1.0)
        assert coordinator._daily_plan_last_accumulated == _T0


class TestFailureIsolation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "broken",
        [
            "accumulate_daily_plan_actuals",
            "accumulate_financials",
            "accumulate_savings",
        ],
    )
    async def test_one_failing_tracker_does_not_starve_the_others(
        self, tmp_path: Path, broken: str
    ) -> None:
        coordinator = _state(tmp_path)
        steps = {
            name: AsyncMock(return_value=None)
            for name in (
                "accumulate_daily_plan_actuals",
                "accumulate_financials",
                "accumulate_savings",
                "persist_all_trackers",
            )
        }
        steps[broken].side_effect = RuntimeError("boom")
        log = MagicMock()

        with (
            patch.multiple(_MODULE, **steps),
            patch(f"{_MODULE}.async_log", log),
        ):
            await _feed(coordinator, 0, _live())

        for step in steps.values():
            step.assert_awaited_once()
        assert [call.args[0] for call in log.call_args_list] == ["error"]

    @pytest.mark.asyncio
    async def test_a_failing_save_does_not_reach_the_cycle(
        self, tmp_path: Path
    ) -> None:
        coordinator = _state(tmp_path)
        log = MagicMock()

        with (
            patch(f"{_MODULE}.persist_all_trackers", side_effect=OSError("disk full")),
            patch(f"{_MODULE}.async_log", log),
        ):
            await _feed(coordinator, 0, _live())

        log.assert_called_once()
        assert log.call_args.args[1] == "Tracker persistence failed: %s"

    @pytest.mark.asyncio
    async def test_a_coordinator_without_trackers_is_left_alone(self) -> None:
        """Coordinators built without ``__init__`` own none of them."""
        coordinator = cast(CoordinatorSharedState, SimpleNamespace(hass=MagicMock()))
        persist = AsyncMock()

        with patch(f"{_MODULE}.persist_all_trackers", persist):
            await _feed(coordinator, 0, _live())

        persist.assert_awaited_once_with(coordinator, only=[])


# ---------------------------------------------------------------------------
# Through the real update cycle
# ---------------------------------------------------------------------------

_METERS = {
    "hsem_grid_import_energy_entity": "sensor.grid_import_energy",
    "hsem_grid_export_energy_entity": "sensor.grid_export_energy",
}


def _meter_state(entity_id: str, value: float) -> FakeState:
    return FakeState(entity_id, str(value), {"unit_of_measurement": "kWh"})


async def _cycle(coordinator: Any, minutes: int, grid_import: float) -> None:
    """Run one real cycle at *minutes* past the start with the meter advanced."""
    states = coordinator.hass.states._states
    states["sensor.grid_import_energy"] = _meter_state(
        "sensor.grid_import_energy", grid_import
    )
    states["sensor.grid_export_energy"] = _meter_state(
        "sensor.grid_export_energy", 20.0
    )
    with (
        patch(f"{_CYCLE}.hsem_now", return_value=_T0 + timedelta(minutes=minutes)),
        patch(f"{_TRACKING}.async_track_time_change"),
    ):
        await _run_cycle(coordinator)


class TestTheUpdateCycle:
    @pytest.mark.asyncio
    async def test_a_forced_cycle_feeds_the_trackers_without_a_plan(self) -> None:
        coordinator, _published = _coordinator(
            _METERS, states={_FORCE_MODE_ENTITY: _FORCED}
        )
        coordinator._force_working_mode_entity = _FORCE_MODE_ENTITY

        with patch(f"{_CYCLE}.async_accumulate_meter_trackers", AsyncMock()) as fed:
            await _cycle(coordinator, 0, 100.0)

        fed.assert_awaited_once()
        assert fed.await_args is not None
        handed, _now, live, cfg, planner_output = fed.await_args.args
        assert handed is coordinator
        assert live is coordinator._live
        assert cfg is coordinator._cfg
        assert planner_output is None

    @pytest.mark.asyncio
    async def test_grid_cost_is_counted_through_a_forced_period(self) -> None:
        """The import cost sensor's tracker grows while a mode is forced."""
        coordinator, _published = _coordinator(
            _METERS, states={_FORCE_MODE_ENTITY: _FORCED}
        )
        coordinator._force_working_mode_entity = _FORCE_MODE_ENTITY

        for step in range(5):
            await _cycle(coordinator, 5 * step, 100.0 + step)

        assert coordinator._live is not None
        assert coordinator._live.force_working_mode_state == _FORCED
        # 4 kWh at the harness's import price of 0.25 per kWh.
        assert coordinator._financial_tracker.import_cost_total == pytest.approx(1.0)
        assert coordinator._daily_tracker.actual.grid_import_kwh == pytest.approx(4.0)

    @pytest.mark.asyncio
    async def test_a_failed_cycle_feeds_nothing(self) -> None:
        coordinator, _published = _coordinator(_METERS)

        with (
            patch(f"{_CYCLE}.async_accumulate_meter_trackers", AsyncMock()) as fed,
            patch.object(
                coordinator,
                "_async_collect_and_populate",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(Exception, match="boom"),
        ):
            await _cycle(coordinator, 0, 100.0)

        fed.assert_not_awaited()

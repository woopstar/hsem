"""Tests for the coordinator's performance tracking (issue #1182).

``coordinator_performance`` turns each cycle's live state into a sample and
scores the trailing day in the executor.  These tests pin what goes into a
sample, and the three promises the hindsight solve makes to the rest of the
integration: it runs off the event loop, it cannot fail or delay a cycle, and
it leaves the planner's input untouched.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from collections.abc import Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import homeassistant.util.dt as dt_util

from custom_components.hsem import coordinator_performance
from custom_components.hsem.coordinator_performance import (
    HISTORY_FILE,
    ORACLE_TIME_LIMIT_S,
    REASON_EVALUATION_FAILED,
    async_track_performance,
    build_performance_sample,
)
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.performance_record import (
    REASON_INSUFFICIENT_COVERAGE,
)
from custom_components.hsem.models.performance_tracker import PerformanceTracker
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner.hindsight_scoring import SiteLimits
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils.performance_scoring import (
    PerformanceEvaluation,
    PerformanceRequest,
    evaluate_performance,
)
from tests.test_coordinator_cycle_paths import _coordinator, _run_cycle

_TZ = ZoneInfo("Europe/Copenhagen")
_T0 = datetime(2026, 6, 10, 12, 0, tzinfo=_TZ)
_MODULE = "custom_components.hsem.coordinator_performance"


@pytest.fixture(autouse=True)
def _copenhagen() -> Iterator[None]:
    """Run every test with Home Assistant's zone set to Europe/Copenhagen."""
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(_TZ)
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


class _FakeHass:
    """The slice of Home Assistant the performance tracking uses."""

    def __init__(self, config_dir: Path) -> None:
        self.config = SimpleNamespace(config_dir=str(config_dir))
        self.executor_threads: list[str] = []

    async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
        def run() -> Any:
            self.executor_threads.append(threading.current_thread().name)
            return target(*args)

        return await asyncio.get_running_loop().run_in_executor(None, run)

    def async_create_background_task(
        self, coro: Coroutine[Any, Any, None], name: str
    ) -> asyncio.Task[None]:
        return asyncio.get_running_loop().create_task(coro, name=name)


def _coordinator_state(
    tmp_path: Path, planner_input: PlannerInput | None = None
) -> CoordinatorSharedState:
    """Return the slice of the coordinator the performance tracking uses."""
    return cast(
        CoordinatorSharedState,
        SimpleNamespace(
            hass=_FakeHass(tmp_path),
            _performance_tracker=PerformanceTracker(),
            _performance_task=None,
            _last_planner_input=planner_input,
        ),
    )


def _live(
    *,
    grid_import: float | None = 100.0,
    grid_export: float | None = 20.0,
    soc: float | None = 50.0,
) -> LiveState:
    live = LiveState()
    live.grid_import_energy_kwh = grid_import
    live.grid_export_energy_kwh = grid_export
    live.huawei_batteries_soc_pct = soc
    live.import_electricity_price = 1.5
    live.export_electricity_price = 0.9
    live.import_electricity_price_available = True
    live.export_electricity_price_available = True
    live.solar_production_power_w = 1200.0
    return live


def _history(tmp_path: Path) -> Path:
    return tmp_path / ".storage" / HISTORY_FILE


class TestSample:
    def test_the_meters_soc_and_prices_are_taken_as_read(self) -> None:
        live = _live()
        live.pv_energy_kwh = 4321.0

        sample = build_performance_sample(_T0, live, SensorConfig())

        assert sample.at == _T0
        assert sample.grid_import_energy_kwh == pytest.approx(100.0)
        assert sample.grid_export_energy_kwh == pytest.approx(20.0)
        assert sample.soc_pct == pytest.approx(50.0)
        assert sample.import_price == pytest.approx(1.5)
        assert sample.export_price == pytest.approx(0.9)
        assert sample.pv_energy_kwh == pytest.approx(4321.0)
        assert sample.pv_power_w == pytest.approx(1200.0)
        assert sample.in_control is True

    def test_a_price_that_is_not_authoritative_is_missing_not_zero(self) -> None:
        live = _live()
        live.import_electricity_price = 0.0
        live.import_electricity_price_available = False
        live.export_electricity_price_available = False

        sample = build_performance_sample(_T0, live, SensorConfig())

        assert sample.import_price is None
        assert sample.export_price is None

    def test_a_forced_mode_is_not_hsem_in_control(self) -> None:
        live = _live()
        live.force_working_mode_state = "batteries_charge_grid"

        assert build_performance_sample(_T0, live, SensorConfig()).in_control is False

    def test_read_only_is_not_hsem_in_control(self) -> None:
        cfg = SensorConfig(read_only=True)

        assert build_performance_sample(_T0, _live(), cfg).in_control is False

    def test_a_degraded_cycle_is_not_hsem_in_control(self) -> None:
        live = _live()
        live.add_missing_entity("Missing entity: something optional")

        assert build_performance_sample(_T0, live, SensorConfig()).in_control is False

    def test_no_ev_configured_records_no_ev(self) -> None:
        live = _live()
        live.ev.power_w = 7000.0
        live.ev.is_connected = True

        sample = build_performance_sample(_T0, live, SensorConfig())

        assert sample.ev_power_w is None
        assert sample.ev_connected is None

    def test_a_configured_ev_is_recorded(self) -> None:
        cfg = SensorConfig()
        cfg.ev.power_entity = "sensor.charger_power"
        live = _live()
        live.ev.power_w = 7000.0
        live.ev.is_connected = True

        sample = build_performance_sample(_T0, live, cfg)

        assert sample.ev_power_w == pytest.approx(7000.0)
        assert sample.ev_connected is True

    def test_two_chargers_are_recorded_together(self) -> None:
        cfg = SensorConfig(ev_second_enabled=True)
        cfg.ev.power_entity = "sensor.charger_power"
        cfg.ev_second.power_entity = "sensor.second_charger_power"
        live = _live()
        live.ev.power_w = 7000.0
        live.ev.is_connected = False
        live.ev_second.power_w = 3600.0
        live.ev_second.is_connected = True

        sample = build_performance_sample(_T0, live, cfg)

        assert sample.ev_power_w == pytest.approx(10600.0)
        assert sample.ev_connected is True

    def test_a_second_charger_that_is_switched_off_is_left_out(self) -> None:
        cfg = SensorConfig(ev_second_enabled=False)
        cfg.ev.power_entity = "sensor.charger_power"
        cfg.ev_second.power_entity = "sensor.second_charger_power"
        live = _live()
        live.ev.power_w = 7000.0
        live.ev_second.power_w = 3600.0

        sample = build_performance_sample(_T0, live, cfg)

        assert sample.ev_power_w == pytest.approx(7000.0)

    def test_an_unavailable_charger_reading_is_missing_not_zero(self) -> None:
        cfg = SensorConfig()
        cfg.ev.power_entity = "sensor.charger_power"
        live = _live()
        live.ev.power_w = None

        assert build_performance_sample(_T0, live, cfg).ev_power_w is None


async def _finish(coordinator: CoordinatorSharedState) -> None:
    """Wait for the evaluation the last cycle started, if any."""
    task = coordinator._performance_task
    if task is not None:
        await task


class TestTracking:
    @pytest.mark.asyncio
    async def test_a_cycle_records_into_the_slot_it_happened_in(
        self, tmp_path: Path
    ) -> None:
        coordinator = _coordinator_state(tmp_path)
        cfg = SensorConfig()

        await async_track_performance(coordinator, _T0, _live(), cfg)
        await async_track_performance(
            coordinator, _T0 + timedelta(minutes=5), _live(grid_import=100.4), cfg
        )
        await _finish(coordinator)

        slot = coordinator._performance_tracker.slots[_T0.astimezone(UTC)]
        assert slot.minutes == cfg.recommendation_interval_minutes
        assert slot.grid_import_kwh == pytest.approx(0.4)
        assert slot.coverage_seconds == pytest.approx(300.0)

    @pytest.mark.asyncio
    async def test_the_history_is_restored_before_the_first_sample(
        self, tmp_path: Path
    ) -> None:
        """A restart continues from the persisted baseline."""
        first = _coordinator_state(tmp_path)
        await async_track_performance(first, _T0, _live(), SensorConfig())
        await _finish(first)
        assert _history(tmp_path).exists()

        restarted = _coordinator_state(tmp_path)
        await async_track_performance(
            restarted,
            _T0 + timedelta(minutes=4),
            _live(grid_import=100.2),
            SensorConfig(),
        )
        await _finish(restarted)

        slot = restarted._performance_tracker.slots[_T0.astimezone(UTC)]
        assert slot.grid_import_kwh == pytest.approx(0.2)
        assert slot.coverage_seconds == pytest.approx(240.0)

    @pytest.mark.asyncio
    async def test_the_site_limits_follow_the_latest_plan(self, tmp_path: Path) -> None:
        planner_input = PlannerInput(battery_rated_capacity_kwh=13.5)
        coordinator = _coordinator_state(tmp_path, planner_input)

        await async_track_performance(coordinator, _T0, _live(), SensorConfig())
        await _finish(coordinator)

        assert coordinator._performance_tracker.site == SiteLimits.from_planner_input(
            planner_input
        )

    @pytest.mark.asyncio
    async def test_the_planner_input_is_left_untouched(self, tmp_path: Path) -> None:
        """Tracking only reads the plan's input: planning is unaffected."""
        planner_input = PlannerInput(
            battery_rated_capacity_kwh=13.5,
            excess_export_enabled=True,
            main_fuse_amps=25.0,
        )
        before = copy.deepcopy(planner_input)
        coordinator = _coordinator_state(tmp_path, planner_input)

        for minute in (0, 5, 10, 15, 20):
            await async_track_performance(
                coordinator,
                _T0 + timedelta(minutes=minute),
                _live(grid_import=100.0 + minute / 50.0),
                SensorConfig(),
            )
            await _finish(coordinator)

        assert planner_input == before
        assert coordinator._last_planner_input is planner_input

    @pytest.mark.asyncio
    async def test_an_evaluation_is_started_for_the_trailing_window(
        self, tmp_path: Path
    ) -> None:
        coordinator = _coordinator_state(tmp_path, PlannerInput())

        await async_track_performance(coordinator, _T0, _live(), SensorConfig())
        await _finish(coordinator)

        window = coordinator._performance_tracker.window
        assert window is not None
        assert window.end == _T0.astimezone(UTC)
        assert window.slot_count == 96
        # One sample is not a day: the window says so instead of a number.
        assert window.reason == REASON_INSUFFICIENT_COVERAGE
        saved = json.loads(_history(tmp_path).read_text(encoding="utf-8"))
        assert saved["window"]["reason"] == REASON_INSUFFICIENT_COVERAGE

    @pytest.mark.asyncio
    async def test_the_window_is_not_scored_again_within_the_same_slot(
        self, tmp_path: Path
    ) -> None:
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        await async_track_performance(coordinator, _T0, _live(), SensorConfig())
        await _finish(coordinator)
        first_task = coordinator._performance_task

        await async_track_performance(
            coordinator, _T0 + timedelta(minutes=5), _live(), SensorConfig()
        )

        assert coordinator._performance_task is first_task

    @pytest.mark.asyncio
    async def test_a_new_slot_starts_a_new_evaluation(self, tmp_path: Path) -> None:
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        await async_track_performance(coordinator, _T0, _live(), SensorConfig())
        await _finish(coordinator)
        first_task = coordinator._performance_task

        await async_track_performance(
            coordinator, _T0 + timedelta(minutes=15), _live(), SensorConfig()
        )
        await _finish(coordinator)

        assert coordinator._performance_task is not first_task
        window = coordinator._performance_tracker.window
        assert window is not None
        assert window.end == (_T0 + timedelta(minutes=15)).astimezone(UTC)

    @pytest.mark.asyncio
    async def test_a_partly_built_coordinator_is_left_alone(self) -> None:
        """Coordinators built without ``__init__`` have no tracker."""
        coordinator = cast(CoordinatorSharedState, SimpleNamespace())

        await async_track_performance(coordinator, _T0, _live(), SensorConfig())


class TestTheSolveStaysOutOfTheWay:
    @pytest.mark.asyncio
    async def test_the_solve_runs_in_the_executor_and_is_not_awaited(
        self, tmp_path: Path
    ) -> None:
        """The cycle returns while the solve is still running elsewhere."""
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        hass = cast(_FakeHass, coordinator.hass)
        release = threading.Event()
        started = threading.Event()
        loop_thread = threading.current_thread().name

        def slow(request: PerformanceRequest) -> PerformanceEvaluation:
            started.set()
            assert release.wait(timeout=10.0)
            return evaluate_performance(request)

        with patch(f"{_MODULE}.evaluate_performance", side_effect=slow) as solved:
            await async_track_performance(coordinator, _T0, _live(), SensorConfig())
            task = coordinator._performance_task
            assert task is not None
            # The cycle is back; the solve has not finished, and a further
            # cycle neither waits for it nor starts a second one.
            await asyncio.to_thread(started.wait, 10.0)
            assert not task.done()
            assert coordinator._performance_tracker.window is None
            await async_track_performance(
                coordinator, _T0 + timedelta(minutes=5), _live(), SensorConfig()
            )
            assert coordinator._performance_task is task
            assert solved.call_count == 1

            release.set()
            await task

        assert coordinator._performance_tracker.window is not None
        assert hass.executor_threads
        assert loop_thread not in hass.executor_threads

    @pytest.mark.asyncio
    async def test_the_solve_gets_the_device_time_limit(self, tmp_path: Path) -> None:
        coordinator = _coordinator_state(tmp_path, PlannerInput())

        with patch(
            f"{_MODULE}.evaluate_performance", side_effect=evaluate_performance
        ) as solved:
            await async_track_performance(coordinator, _T0, _live(), SensorConfig())
            await _finish(coordinator)

        (request,) = solved.call_args.args
        assert request.time_limit_s == pytest.approx(ORACLE_TIME_LIMIT_S)
        assert request.slot_minutes == 15

    @pytest.mark.asyncio
    async def test_a_failing_solve_leaves_a_reason_and_is_not_retried(
        self, tmp_path: Path
    ) -> None:
        coordinator = _coordinator_state(tmp_path, PlannerInput())

        with patch(
            f"{_MODULE}.evaluate_performance", side_effect=RuntimeError("boom")
        ) as solved:
            await async_track_performance(coordinator, _T0, _live(), SensorConfig())
            await _finish(coordinator)
            await async_track_performance(
                coordinator, _T0 + timedelta(minutes=5), _live(), SensorConfig()
            )
            await _finish(coordinator)

        window = coordinator._performance_tracker.window
        assert window is not None
        assert window.reason == REASON_EVALUATION_FAILED
        assert window.end == _T0.astimezone(UTC)
        assert not window.is_scored
        assert solved.call_count == 1

    @pytest.mark.asyncio
    async def test_a_failure_while_recording_does_not_reach_the_cycle(
        self, tmp_path: Path
    ) -> None:
        coordinator = _coordinator_state(tmp_path)

        with patch.object(
            PerformanceTracker, "accumulate", side_effect=RuntimeError("boom")
        ):
            await async_track_performance(coordinator, _T0, _live(), SensorConfig())

        assert coordinator._performance_task is None

    @pytest.mark.asyncio
    async def test_limits_that_cannot_be_read_do_not_cost_the_sample(
        self, tmp_path: Path
    ) -> None:
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        cfg = SensorConfig()

        with patch.object(
            SiteLimits, "from_planner_input", side_effect=TypeError("bad input")
        ):
            await async_track_performance(coordinator, _T0, _live(), cfg)
            await async_track_performance(
                coordinator, _T0 + timedelta(minutes=5), _live(grid_import=100.4), cfg
            )

        tracker = coordinator._performance_tracker
        assert tracker.site is None
        assert tracker.slots[_T0.astimezone(UTC)].grid_import_kwh == pytest.approx(0.4)

    @pytest.mark.asyncio
    async def test_a_failure_while_saving_does_not_reach_the_cycle(
        self, tmp_path: Path
    ) -> None:
        coordinator = _coordinator_state(tmp_path, PlannerInput())

        with patch(f"{_MODULE}.persist_all_trackers", side_effect=OSError("disk full")):
            await async_track_performance(coordinator, _T0, _live(), SensorConfig())
            await _finish(coordinator)

        assert coordinator._performance_tracker.window is None
        assert not _history(tmp_path).exists()

    @pytest.mark.asyncio
    async def test_a_save_failing_after_the_solve_keeps_the_score(
        self, tmp_path: Path
    ) -> None:
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        tracker = coordinator._performance_tracker
        await coordinator_performance.init_performance_tracker(
            tracker, coordinator.hass
        )
        request = tracker.request(_T0, slot_minutes=15, zone=_TZ)

        with patch(f"{_MODULE}.persist_all_trackers", side_effect=OSError("disk full")):
            await coordinator_performance._async_evaluate(coordinator, request)

        assert tracker.window is not None


@pytest.mark.skipif(not is_scipy_available(), reason="scipy not available")
class TestADayOfCycles:
    @pytest.mark.asyncio
    async def test_a_recorded_day_ends_with_a_score(self, tmp_path: Path) -> None:
        """Five-minute cycles over 25 hours, an idle battery, dear evenings."""
        planner_input = PlannerInput(
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=5.0,
            battery_max_charge_power_w=5000.0,
            battery_max_discharge_power_w=5000.0,
        )
        coordinator = _coordinator_state(tmp_path, planner_input)
        start = datetime(2026, 6, 9, 23, 0, tzinfo=_TZ)
        imported = 1000.0
        for cycle in range(25 * 12 + 1):
            now = start + timedelta(minutes=5 * cycle)
            live = _live(grid_import=imported, grid_export=20.0, soc=40.0)
            live.import_electricity_price = 2.5 if 17 <= now.hour < 21 else 0.8
            live.export_electricity_price = live.import_electricity_price - 0.3
            live.solar_production_power_w = 0.0
            await async_track_performance(coordinator, now, live, SensorConfig())
            imported += 0.05
        await _finish(coordinator)
        # The last cycle's evaluation may have been skipped while the previous
        # one was still running; one more cycle in the same slot picks it up.
        await async_track_performance(
            coordinator,
            start + timedelta(hours=25, minutes=1),
            _live(grid_import=imported, soc=40.0),
            SensorConfig(),
        )
        await _finish(coordinator)

        tracker = coordinator._performance_tracker
        window = tracker.window
        assert window is not None
        assert window.reason is None
        assert window.coverage == pytest.approx(1.0)
        assert window.control_coverage == pytest.approx(1.0)
        assert window.realized_cost == pytest.approx(
            0.6 * (20 * 0.8 + 4 * 2.5), rel=1e-6
        )
        # The battery sat idle through four dear hours.
        assert window.regret is not None and window.regret > 1.0
        assert window.realized_end_kwh == pytest.approx(4.0)
        # The local day that ended in the run is scored too, once.
        assert list(tracker.days) == ["2026-06-09", "2026-06-10"]
        assert tracker.days["2026-06-09"].reason == REASON_INSUFFICIENT_COVERAGE
        assert tracker.days["2026-06-10"].is_scored
        assert tracker.days["2026-06-10"].slot_count == 96


class TestTheUpdateCycle:
    @pytest.mark.asyncio
    async def test_the_cycle_hands_its_sample_over_last(
        self, _performance_tracking_off_in_cycle_tests: Any
    ) -> None:
        coordinator, published = _coordinator()

        await _run_cycle(coordinator)

        tracked = _performance_tracking_off_in_cycle_tests
        tracked.assert_awaited_once()
        handed_coordinator, _now, live, cfg = tracked.await_args.args
        assert handed_coordinator is coordinator
        assert live is coordinator._live
        assert cfg is coordinator._cfg
        # Published before the performance record is touched.
        assert len(published) == 1

    @pytest.mark.asyncio
    async def test_a_cycle_is_not_recorded_when_it_fails(
        self, _performance_tracking_off_in_cycle_tests: Any
    ) -> None:
        coordinator, _published = _coordinator()

        with (
            patch.object(
                coordinator,
                "_async_collect_and_populate",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(Exception, match="boom"),
        ):
            await _run_cycle(coordinator)

        _performance_tracking_off_in_cycle_tests.assert_not_awaited()

    def test_teardown_cancels_a_running_evaluation(self) -> None:
        coordinator, _published = _coordinator()
        loop = asyncio.new_event_loop()
        try:
            task = loop.create_task(asyncio.sleep(60))
            coordinator._performance_task = task
            loop.run_until_complete(coordinator.async_teardown())
            loop.run_until_complete(asyncio.sleep(0))

            assert task.cancelled()
            assert coordinator._performance_task is None
        finally:
            loop.close()

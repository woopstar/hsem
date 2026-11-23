"""Tests for the coordinator side of ``hsem.what_if`` (issue #1432).

Two promises are pinned here.  A completed day reaches the what-if history
through the same evaluation that scores it, so the service has something to
replay long after the performance tracker dropped the slots.  And a replay
runs off the event loop, one at a time, reading only: it leaves the planner's
input, the plan and the hardware alone.  One at a time means one executor
thread: a caller that stops waiting, or a reload, does not free the slot
before the thread has returned.

Issue #1454 adds three more.  The replay has a worker thread of its own, made
on the first call and shut down on unload and when Home Assistant stops, so it
never holds a worker of the shared pool.  A day read back from disk is not
written again because it is rounded.  And a job that cannot be submitted gives
the slot back.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest

import homeassistant.util.dt as dt_util
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from custom_components.hsem import coordinator_performance, coordinator_what_if
from custom_components.hsem.coordinator_performance import async_track_performance
from custom_components.hsem.coordinator_persistence import persist_all_trackers
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.coordinator_what_if import (
    EXECUTOR_THREAD_NAME,
    HISTORY_FILE,
    ORACLE_TIME_LIMIT_S,
    PAUSE_BETWEEN_DAYS_S,
    RUN_TIME_LIMIT_S,
    WhatIfBusyError,
    WhatIfUnavailableError,
    async_rebuild_days,
    async_record_days,
    async_run_what_if,
    build_what_if_request,
    cancel_what_if,
    init_what_if_history,
    shutdown_what_if,
    what_if_running,
)
from custom_components.hsem.models.energy_backfill import HourlyEnergy
from custom_components.hsem.models.performance_record import (
    REASON_INSUFFICIENT_COVERAGE,
    PerformanceScore,
    PerformanceSlot,
)
from custom_components.hsem.models.performance_tracker import PerformanceTracker
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_history_tracker import PriceHistoryTracker
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.what_if_history import WhatIfHistory
from custom_components.hsem.planner.hindsight_scoring import SiteLimits
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils.misc import resolve_cycle_cost
from custom_components.hsem.utils.performance_scoring import PerformanceRequest
from custom_components.hsem.utils.what_if import (
    WhatIfScenario,
    usable_capacity_kwh,
)
from custom_components.hsem.utils.what_if_rebuild import (
    REASON_PRICE_INCOMPLETE,
    REASON_STATISTICS_INCOMPLETE,
    local_day_hours,
)
from tests.test_coordinator_cycle_paths import _coordinator
from tests.test_coordinator_performance import _FakeHass, _finish, _live
from tests.utils.what_if_days import FIRST_DAY, SITE, make_day, make_days

needs_scipy = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2026, 6, 4, 12, 0, tzinfo=_TZ)
_BIGGER = (WhatIfScenario(usable_kwh=15.0),)


@pytest.fixture(autouse=True)
def _copenhagen() -> Iterator[None]:
    """Run every test with Home Assistant's zone set to Europe/Copenhagen."""
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(_TZ)
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


@pytest.fixture(autouse=True)
def _free_slot() -> Iterator[None]:
    """Leave the process-wide replay slot free and its executor shut down."""
    yield
    shutdown_what_if()
    coordinator_what_if._Flight.cancel = None


class _Bus:
    """The slice of Home Assistant's event bus the what-if uses."""

    def __init__(self) -> None:
        self.listeners: dict[Any, list[Callable[[Any], None]]] = {}
        self.removed: list[Any] = []

    def async_listen_once(
        self, event_type: Any, listener: Callable[[Any], None]
    ) -> Callable[[], None]:
        self.listeners.setdefault(event_type, []).append(listener)

        def remove() -> None:
            self.removed.append(event_type)
            self.listeners[event_type].remove(listener)

        return remove

    def fire_once(self, event_type: Any) -> None:
        """Call the listeners of *event_type* and forget them."""
        for listener in self.listeners.pop(event_type, []):
            listener(SimpleNamespace(event_type=event_type))


class _Hass(_FakeHass):
    """Home Assistant with an event bus, for the replay's stop listener."""

    def __init__(self, config_dir: Path) -> None:
        super().__init__(config_dir)
        self.bus = _Bus()


async def _until_free() -> None:
    """Wait for the executor thread of a replay to return."""
    for _ in range(1000):
        if not what_if_running():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the replay never released its slot")


def _coordinator_state(
    tmp_path: Path, planner_input: PlannerInput | None = None
) -> CoordinatorSharedState:
    """Return the slice of the coordinator the what-if uses."""
    return cast(
        CoordinatorSharedState,
        SimpleNamespace(
            hass=_Hass(tmp_path),
            _performance_tracker=PerformanceTracker(),
            _performance_task=None,
            _what_if_history=WhatIfHistory(),
            _last_planner_input=planner_input,
        ),
    )


def _history_file(tmp_path: Path) -> Path:
    return tmp_path / ".storage" / HISTORY_FILE


def _day_request(
    *, complete: bool, site: SiteLimits | None = SITE
) -> PerformanceRequest:
    """Return an evaluation request holding one local day of hourly slots."""
    start = datetime(2026, 6, 9, 22, 0, tzinfo=UTC)
    keys = tuple(start + timedelta(hours=index) for index in range(24))
    slots = {
        key: PerformanceSlot(
            start=key,
            minutes=60,
            grid_import_kwh=0.5,
            soc_start_pct=50.0,
            soc_end_pct=50.0,
            import_price=1.5,
            export_price=0.6,
            coverage_seconds=3600.0,
        )
        for key in keys
    }
    if not complete:
        del slots[keys[7]]
    return PerformanceRequest(
        slots=slots,
        slot_minutes=60,
        window=keys,
        days=(("2026-06-10", keys),),
        site=site,
    )


class TestHistoryFile:
    @pytest.mark.asyncio
    async def test_it_is_restored_once_from_storage(self, tmp_path: Path) -> None:
        saved = WhatIfHistory(
            history_file=str(_history_file(tmp_path)), days=make_days(2)
        )
        await saved.save_history()
        history = WhatIfHistory()
        hass = cast(Any, _FakeHass(tmp_path))

        await init_what_if_history(history, hass)
        history.days.clear()
        await init_what_if_history(history, hass)

        assert history.history_file == str(_history_file(tmp_path))
        # The second call did not read the file again.
        assert history.days == {}

    @pytest.mark.asyncio
    async def test_it_is_not_persisted_before_its_file_is_known(
        self, tmp_path: Path
    ) -> None:
        coordinator = _coordinator_state(tmp_path)
        coordinator._what_if_history.days.update(make_days(1))

        await persist_all_trackers(coordinator, only=["_what_if_history"])

        assert not _history_file(tmp_path).exists()


class TestRecordingDays:
    @pytest.mark.asyncio
    async def test_a_complete_day_is_kept_and_saved(self, tmp_path: Path) -> None:
        coordinator = _coordinator_state(tmp_path)

        await async_record_days(coordinator, _day_request(complete=True))

        day = coordinator._what_if_history.days["2026-06-10"]
        assert len(day.grid_import_kwh) == 24
        assert day.start == datetime(2026, 6, 9, 22, 0, tzinfo=UTC)
        saved = json.loads(_history_file(tmp_path).read_text(encoding="utf-8"))
        assert [entry["day"] for entry in saved["days"]] == ["2026-06-10"]

    @pytest.mark.asyncio
    async def test_a_day_is_kept_with_the_battery_of_that_day(
        self, tmp_path: Path
    ) -> None:
        coordinator = _coordinator_state(tmp_path)
        site = SiteLimits(
            rated_kwh=7.0,
            min_soc_pct=5.0,
            max_soc_pct=100.0,
            max_charge_kw=3.5,
            max_discharge_kw=3.5,
            charge_efficiency=0.95,
            discharge_efficiency=0.94,
        )

        await async_record_days(coordinator, _day_request(complete=True, site=site))

        day = coordinator._what_if_history.days["2026-06-10"]
        assert day.rated_kwh == pytest.approx(7.0)
        assert day.charge_efficiency == pytest.approx(0.95)
        assert day.discharge_efficiency == pytest.approx(0.94)

    @pytest.mark.asyncio
    async def test_a_day_before_the_limits_are_known_is_not_kept(
        self, tmp_path: Path
    ) -> None:
        """Without the battery, the SoC of the day cannot be turned into energy."""
        coordinator = _coordinator_state(tmp_path)

        await async_record_days(coordinator, _day_request(complete=True, site=None))

        assert coordinator._what_if_history.days == {}
        assert not _history_file(tmp_path).exists()

    @pytest.mark.asyncio
    async def test_the_same_day_again_does_not_rewrite_the_file(
        self, tmp_path: Path
    ) -> None:
        """An evaluation that keeps asking for a day writes it once."""
        coordinator = _coordinator_state(tmp_path)

        with patch.object(
            coordinator_what_if, "persist_all_trackers", AsyncMock()
        ) as persist:
            await async_record_days(coordinator, _day_request(complete=True))
            await async_record_days(coordinator, _day_request(complete=True))
            assert persist.await_count == 1

            # The day did change: it is written again.
            changed = replace(SITE, rated_kwh=12.0)
            await async_record_days(
                coordinator, _day_request(complete=True, site=changed)
            )
            assert persist.await_count == 2

    @pytest.mark.asyncio
    async def test_a_restart_does_not_rewrite_an_unchanged_day(
        self, tmp_path: Path
    ) -> None:
        """The day on disk is rounded, the fresh one is not: still the same day."""
        request = _day_request(
            complete=True,
            site=replace(SITE, rated_kwh=10.0 / 3.0, charge_efficiency=0.97123456789),
        )
        request = replace(
            request,
            slots={
                key: replace(
                    slot,
                    grid_import_kwh=1.0 / 3.0,
                    soc_start_pct=50.123456789,
                    soc_end_pct=50.123456789,
                    import_price=1.23456789,
                    pv_kwh=2.0 / 3.0,
                )
                for key, slot in request.slots.items()
            },
        )
        await async_record_days(_coordinator_state(tmp_path), request)
        assert _history_file(tmp_path).exists()

        restarted = _coordinator_state(tmp_path)
        with patch.object(
            coordinator_what_if, "persist_all_trackers", AsyncMock()
        ) as persist:
            await async_record_days(restarted, request)
            assert persist.await_count == 0
            # The day it was compared with really is the rounded one.
            stored = restarted._what_if_history.days["2026-06-10"]
            assert stored.grid_import_kwh[1] == pytest.approx(0.3333, abs=1e-12)

            # A reading that did change is written.
            first = next(iter(request.slots))
            changed = replace(
                request,
                slots={
                    **request.slots,
                    first: replace(request.slots[first], grid_import_kwh=0.5),
                },
            )
            await async_record_days(restarted, changed)
            assert persist.await_count == 1

    @pytest.mark.asyncio
    async def test_an_evaluation_that_keeps_failing_writes_a_day_once(
        self, tmp_path: Path
    ) -> None:
        """Through the real evaluation: it raises, the day stays unscored."""
        coordinator = _coordinator_state(tmp_path)
        request = _day_request(complete=True)

        with (
            patch.object(
                coordinator_performance,
                "evaluate_performance",
                side_effect=RuntimeError("solver broke"),
            ),
            patch.object(coordinator_performance, "async_log"),
            patch.object(
                coordinator_what_if, "persist_all_trackers", AsyncMock()
            ) as persist,
        ):
            await coordinator_performance._async_evaluate(coordinator, request)
            await coordinator_performance._async_evaluate(coordinator, request)

        assert "2026-06-10" in coordinator._what_if_history.days
        assert persist.await_count == 1

    @pytest.mark.asyncio
    async def test_a_day_with_a_gap_is_not_kept(self, tmp_path: Path) -> None:
        coordinator = _coordinator_state(tmp_path)

        await async_record_days(coordinator, _day_request(complete=False))

        assert coordinator._what_if_history.days == {}
        assert not _history_file(tmp_path).exists()

    @pytest.mark.asyncio
    async def test_earlier_days_on_disk_are_not_overwritten(
        self, tmp_path: Path
    ) -> None:
        """The first day recorded after a restart joins the days already saved."""
        saved = WhatIfHistory(
            history_file=str(_history_file(tmp_path)), days=make_days(2)
        )
        await saved.save_history()
        coordinator = _coordinator_state(tmp_path)

        await async_record_days(coordinator, _day_request(complete=True))

        on_disk = json.loads(_history_file(tmp_path).read_text(encoding="utf-8"))
        assert [entry["day"] for entry in on_disk["days"]] == [
            "2026-06-01",
            "2026-06-02",
            "2026-06-10",
        ]

    @pytest.mark.asyncio
    async def test_a_coordinator_without_a_history_is_left_alone(self) -> None:
        coordinator = cast(CoordinatorSharedState, SimpleNamespace())

        await async_record_days(coordinator, _day_request(complete=True))

    @needs_scipy
    @pytest.mark.asyncio
    async def test_a_day_the_cycles_recorded_can_be_replayed(
        self, tmp_path: Path
    ) -> None:
        """From the meters a cycle reads, through the evaluation, to a replay."""
        planner_input = PlannerInput(battery_rated_capacity_kwh=10.0)
        coordinator = _coordinator_state(tmp_path, planner_input)
        cfg = SensorConfig()
        cfg.recommendation_interval_minutes = 60
        first = datetime(2026, 6, 9, 23, 0, tzinfo=_TZ)

        for step in range(26 * 12 + 1):
            hours = step / 12.0
            await async_track_performance(
                coordinator,
                first + timedelta(minutes=5 * step),
                _live(grid_import=100.0 + 0.5 * hours, grid_export=20.0, soc=50.0),
                cfg,
            )
            await _finish(coordinator)

        day = coordinator._what_if_history.days["2026-06-10"]
        assert len(day.grid_import_kwh) == 24
        assert sum(day.grid_import_kwh) == pytest.approx(12.0)
        assert _history_file(tmp_path).exists()

        response = await async_run_what_if(
            coordinator,
            datetime(2026, 6, 11, 12, 0, tzinfo=_TZ),
            history_days=2,
            scenarios=_BIGGER,
        )

        assert response is not None
        assert response["days_compared"] == 1
        # The evening before is a day of one hour: scored as incomplete, never kept.
        assert response["skipped_reasons"] == {REASON_INSUFFICIENT_COVERAGE: 1}
        assert [entry["date"] for entry in response["days"]] == ["2026-06-10"]

    @pytest.mark.asyncio
    async def test_a_failure_to_record_does_not_fail_the_evaluation(
        self, tmp_path: Path
    ) -> None:
        coordinator = _coordinator_state(tmp_path, PlannerInput())

        with (
            patch.object(
                coordinator_performance,
                "async_record_days",
                side_effect=RuntimeError("disk full"),
            ),
            patch.object(coordinator_performance, "async_log") as log,
        ):
            await async_track_performance(coordinator, _NOW, _live(), SensorConfig())
            await _finish(coordinator)

        assert coordinator._performance_tracker.window is not None
        assert "Recording days for what-if failed" in log.call_args.args[1]


class TestRequest:
    def test_it_covers_the_completed_days_before_today(self, tmp_path: Path) -> None:
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        coordinator._what_if_history.days.update(make_days(3))

        request = build_what_if_request(
            coordinator,
            _NOW,
            history_days=3,
            scenarios=_BIGGER,
            capacity_price_per_kwh=2500.0,
        )

        assert request.days == ("2026-06-01", "2026-06-02", "2026-06-03")
        assert request.scenarios == _BIGGER
        assert request.capacity_price_per_kwh == pytest.approx(2500.0)
        assert request.time_limit_s == pytest.approx(ORACLE_TIME_LIMIT_S)
        # The whole run is bounded and hands the processor over between days.
        assert request.budget_s == pytest.approx(RUN_TIME_LIMIT_S)
        assert request.pause_s == pytest.approx(PAUSE_BETWEEN_DAYS_S)
        assert request.pause_s > 0.0
        assert request.site == SiteLimits.from_planner_input(PlannerInput())

    def test_the_recorded_days_are_a_copy(self, tmp_path: Path) -> None:
        """A day recorded while a replay runs does not reach that replay."""
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        coordinator._what_if_history.days.update(make_days(2))

        request = build_what_if_request(
            coordinator, _NOW, history_days=3, scenarios=_BIGGER
        )
        coordinator._what_if_history.days.clear()

        assert sorted(request.recorded) == ["2026-06-01", "2026-06-02"]

    def test_unscored_days_carry_the_scoring_reason(self, tmp_path: Path) -> None:
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        start = datetime(2026, 6, 1, 22, 0, tzinfo=UTC)
        coordinator._performance_tracker.days = {
            "2026-06-02": PerformanceScore(
                start=start,
                end=start + timedelta(hours=24),
                slot_count=24,
                reason=REASON_INSUFFICIENT_COVERAGE,
                day="2026-06-02",
            ),
            "2026-06-03": PerformanceScore(
                start=start, end=start + timedelta(hours=24), slot_count=24
            ),
        }

        request = build_what_if_request(
            coordinator, _NOW, history_days=3, scenarios=_BIGGER
        )

        assert request.reasons == {"2026-06-02": REASON_INSUFFICIENT_COVERAGE}

    def test_the_wear_is_the_planners_own_cycle_cost(self, tmp_path: Path) -> None:
        planner_input = PlannerInput(
            battery_rated_capacity_kwh=10.0,
            battery_purchase_price=40000.0,
            battery_expected_cycles=6000,
        )
        coordinator = _coordinator_state(tmp_path, planner_input)

        request = build_what_if_request(
            coordinator, _NOW, history_days=1, scenarios=_BIGGER
        )

        assert request.cycle_cost_per_kwh > 0.0
        assert request.cycle_cost_per_kwh == pytest.approx(
            resolve_cycle_cost(
                purchase_price=40000.0,
                usable_kwh=usable_capacity_kwh(request.site),
                expected_cycles=6000,
                capacity_loss_pct=planner_input.battery_capacity_loss_pct,
                user_margin=planner_input.battery_cycle_cost_per_kwh,
            )
        )

    def test_there_is_none_before_the_first_plan(self, tmp_path: Path) -> None:
        with pytest.raises(WhatIfUnavailableError, match="no planner cycle"):
            build_what_if_request(
                _coordinator_state(tmp_path), _NOW, history_days=1, scenarios=_BIGGER
            )

    def test_there_is_none_without_usable_capacity(self, tmp_path: Path) -> None:
        planner_input = PlannerInput(battery_rated_capacity_kwh=0.0)

        with pytest.raises(WhatIfUnavailableError, match="no usable capacity"):
            build_what_if_request(
                _coordinator_state(tmp_path, planner_input),
                _NOW,
                history_days=1,
                scenarios=_BIGGER,
            )


class TestRun:
    @needs_scipy
    @pytest.mark.asyncio
    async def test_it_solves_in_its_own_executor_and_only_reads(
        self, tmp_path: Path
    ) -> None:
        planner_input = PlannerInput(battery_rated_capacity_kwh=10.0)
        before = copy.deepcopy(planner_input)
        coordinator = _coordinator_state(tmp_path, planner_input)
        coordinator._what_if_history.days.update(make_days(2))
        coordinator._what_if_history.history_file = str(_history_file(tmp_path))
        recorded = dict(coordinator._what_if_history.days)
        run = coordinator_what_if.run_what_if
        threads: list[str] = []

        def recording(*args: Any) -> Any:
            threads.append(threading.current_thread().name)
            return run(*args)

        with patch.object(coordinator_what_if, "run_what_if", recording):
            response = await async_run_what_if(
                coordinator, _NOW, history_days=3, scenarios=_BIGGER
            )

        assert response is not None
        assert response["days_compared"] == 2
        assert response["days_skipped"] == 1
        # Its own worker thread, not one of Home Assistant's shared pool.
        assert len(threads) == 1
        assert threads[0].startswith(EXECUTOR_THREAD_NAME)
        assert cast(_Hass, coordinator.hass).executor_threads == []
        assert coordinator._last_planner_input == before
        assert coordinator._what_if_history.days == recorded
        assert coordinator._performance_tracker.days == {}
        # Nothing was written: the history file was never created.
        assert not _history_file(tmp_path).exists()
        await _until_free()

    @pytest.mark.asyncio
    async def test_only_one_runs_at_a_time(self, tmp_path: Path) -> None:
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        release = threading.Event()
        running = threading.Event()

        def slow(request: Any, cancelled: Any) -> dict[str, Any]:
            running.set()
            release.wait(timeout=10.0)
            return {"cancelled": cancelled()}

        with patch.object(coordinator_what_if, "run_what_if", slow):
            first = asyncio.create_task(
                async_run_what_if(coordinator, _NOW, history_days=1, scenarios=_BIGGER)
            )
            await asyncio.to_thread(running.wait, 10.0)
            # The event loop is free while the replay runs.
            with pytest.raises(WhatIfBusyError):
                await async_run_what_if(
                    coordinator, _NOW, history_days=1, scenarios=_BIGGER
                )
            release.set()
            assert await first == {"cancelled": False}

        await _until_free()

    @pytest.mark.asyncio
    async def test_a_planner_cycle_is_not_delayed_by_a_running_replay(
        self, tmp_path: Path
    ) -> None:
        """While a replay sits in the executor, a cycle runs and finishes.

        The cycle here does what a planner cycle does to the machinery the
        replay shares with it: it takes turns on the event loop and sends a
        job of its own to the executor.  Both finish while the replay is still
        running.
        """
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        release = threading.Event()
        running = threading.Event()

        def slow(request: Any, cancelled: Any) -> dict[str, Any]:
            running.set()
            release.wait(timeout=10.0)
            return {}

        async def cycle() -> str:
            for _ in range(50):
                await asyncio.sleep(0)
            return await coordinator.hass.async_add_executor_job(lambda: "planned")

        with patch.object(coordinator_what_if, "run_what_if", slow):
            replay = asyncio.create_task(
                async_run_what_if(coordinator, _NOW, history_days=1, scenarios=_BIGGER)
            )
            await asyncio.to_thread(running.wait, 10.0)

            assert await asyncio.wait_for(cycle(), timeout=5.0) == "planned"
            assert not replay.done()
            assert what_if_running()

            release.set()
            await replay

    @pytest.mark.asyncio
    async def test_a_caller_that_is_cancelled_stops_the_replay(
        self, tmp_path: Path
    ) -> None:
        """A stopped script must not leave a thread solving for nobody.

        The slot stays taken until that thread has returned: a second call,
        also from a coordinator made by a reload, is refused meanwhile.
        """
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        running = threading.Event()
        finish = threading.Event()
        seen: list[bool] = []

        def slow(request: Any, cancelled: Any) -> None:
            running.set()
            for _ in range(1000):
                if cancelled():
                    break
                finish.wait(timeout=0.01)
            seen.append(cancelled())
            finish.wait(timeout=10.0)

        with patch.object(coordinator_what_if, "run_what_if", slow):
            caller = asyncio.create_task(
                async_run_what_if(coordinator, _NOW, history_days=1, scenarios=_BIGGER)
            )
            await asyncio.to_thread(running.wait, 10.0)

            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller

            # The thread is still in its last solve: the slot is not free.
            assert what_if_running()
            reloaded = _coordinator_state(tmp_path, PlannerInput())
            for other in (coordinator, reloaded):
                with pytest.raises(WhatIfBusyError):
                    await async_run_what_if(
                        other, _NOW, history_days=1, scenarios=_BIGGER
                    )

            finish.set()
            await _until_free()

        assert seen == [True]

    @pytest.mark.asyncio
    async def test_a_reload_cannot_start_a_second_replay(self, tmp_path: Path) -> None:
        """Teardown asks the replay to stop; the new coordinator waits for it."""
        old = _coordinator_state(tmp_path, PlannerInput())
        running = threading.Event()
        finish = threading.Event()

        def slow(request: Any, cancelled: Any) -> None:
            running.set()
            finish.wait(timeout=10.0)
            return None

        with patch.object(coordinator_what_if, "run_what_if", slow):
            caller = asyncio.create_task(
                async_run_what_if(old, _NOW, history_days=1, scenarios=_BIGGER)
            )
            await asyncio.to_thread(running.wait, 10.0)

            cancel_what_if()
            new = _coordinator_state(tmp_path, PlannerInput())
            with pytest.raises(WhatIfBusyError):
                await async_run_what_if(new, _NOW, history_days=1, scenarios=_BIGGER)

            finish.set()
            assert await caller is None
            await _until_free()
            # The old thread is gone: the new coordinator may run.
            finish.clear()
            running.clear()
            again = asyncio.create_task(
                async_run_what_if(new, _NOW, history_days=1, scenarios=_BIGGER)
            )
            await asyncio.to_thread(running.wait, 10.0)
            finish.set()
            assert await again is None

    @pytest.mark.asyncio
    async def test_a_call_that_lost_the_race_for_the_slot_is_refused(
        self, tmp_path: Path
    ) -> None:
        """Another call may take the slot while the history file is read."""
        coordinator = _coordinator_state(tmp_path, PlannerInput())

        async def taken(*_args: Any) -> None:
            coordinator_what_if._Flight.cancel = threading.Event()

        with (
            patch.object(coordinator_what_if, "init_what_if_history", taken),
            pytest.raises(WhatIfBusyError),
        ):
            await async_run_what_if(
                coordinator, _NOW, history_days=1, scenarios=_BIGGER
            )

    @pytest.mark.asyncio
    async def test_a_run_that_fails_frees_the_slot(self, tmp_path: Path) -> None:
        coordinator = _coordinator_state(tmp_path, PlannerInput())

        with (
            patch.object(
                coordinator_what_if, "run_what_if", side_effect=RuntimeError("boom")
            ),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await async_run_what_if(
                coordinator, _NOW, history_days=1, scenarios=_BIGGER
            )

        await _until_free()

    @pytest.mark.asyncio
    async def test_a_request_that_cannot_be_built_does_not_take_the_slot(
        self, tmp_path: Path
    ) -> None:
        coordinator = _coordinator_state(tmp_path)

        with pytest.raises(WhatIfUnavailableError):
            await async_run_what_if(
                coordinator, _NOW, history_days=1, scenarios=_BIGGER
            )

        assert not what_if_running()

    @pytest.mark.asyncio
    async def test_a_job_that_cannot_be_submitted_frees_the_slot(
        self, tmp_path: Path
    ) -> None:
        """No job means no done-callback: the slot is given back at once."""
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        closed = ThreadPoolExecutor(max_workers=1)
        closed.shutdown()

        with (
            patch.object(coordinator_what_if, "_executor", return_value=closed),
            pytest.raises(RuntimeError, match="after shutdown"),
        ):
            await async_run_what_if(
                coordinator, _NOW, history_days=1, scenarios=_BIGGER
            )

        assert not what_if_running()
        with patch.object(coordinator_what_if, "run_what_if", return_value={}):
            assert (
                await async_run_what_if(
                    coordinator, _NOW, history_days=1, scenarios=_BIGGER
                )
                == {}
            )
        await _until_free()

    def test_cancelling_when_nothing_runs_does_nothing(self) -> None:
        cancel_what_if()

        assert not what_if_running()

    def test_teardown_stops_a_running_replay(self) -> None:
        coordinator, _published = _coordinator()
        cancel = coordinator_what_if._Flight.cancel = threading.Event()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coordinator.async_teardown())
        finally:
            loop.close()

        assert cancel.is_set()
        # Asked to stop, but the slot is the thread's to give back.
        assert what_if_running()

    def test_a_new_coordinator_has_an_empty_history(self) -> None:
        coordinator, _published = _coordinator()

        assert coordinator._what_if_history.days == {}
        assert FIRST_DAY.isoformat() not in coordinator._what_if_history.days


class TestExecutor:
    """The replay's own worker thread (issue #1454)."""

    @pytest.mark.asyncio
    async def test_it_is_made_on_the_first_call_and_kept(self, tmp_path: Path) -> None:
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        hass = cast(_Hass, coordinator.hass)
        assert coordinator_what_if._Flight.executor is None

        with patch.object(coordinator_what_if, "run_what_if", return_value={}):
            await async_run_what_if(
                coordinator, _NOW, history_days=1, scenarios=_BIGGER
            )
            await _until_free()
            first = coordinator_what_if._Flight.executor
            await async_run_what_if(
                coordinator, _NOW, history_days=1, scenarios=_BIGGER
            )
            await _until_free()

        assert first is not None
        assert first._max_workers == 1
        assert coordinator_what_if._Flight.executor is first
        # One stop listener for the one executor.
        assert len(hass.bus.listeners[EVENT_HOMEASSISTANT_STOP]) == 1

    def test_teardown_shuts_it_down(self) -> None:
        coordinator, _published = _coordinator()
        executor = coordinator_what_if._Flight.executor = ThreadPoolExecutor(
            max_workers=1
        )
        unsub = Mock()
        coordinator_what_if._Flight.unsub_stop.append(unsub)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coordinator.async_teardown())
        finally:
            loop.close()

        assert coordinator_what_if._Flight.executor is None
        assert coordinator_what_if._Flight.unsub_stop == []
        unsub.assert_called_once_with()
        with pytest.raises(RuntimeError, match="after shutdown"):
            executor.submit(lambda: None)

    @pytest.mark.asyncio
    async def test_a_stopping_home_assistant_shuts_it_down(
        self, tmp_path: Path
    ) -> None:
        """A stop does not unload the entry, so the executor listens for it."""
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        hass = cast(_Hass, coordinator.hass)
        with patch.object(coordinator_what_if, "run_what_if", return_value={}):
            await async_run_what_if(
                coordinator, _NOW, history_days=1, scenarios=_BIGGER
            )
        await _until_free()
        executor = coordinator_what_if._Flight.executor
        assert executor is not None

        hass.bus.fire_once(EVENT_HOMEASSISTANT_STOP)

        assert coordinator_what_if._Flight.executor is None
        # The listener ran once and is gone: it is not removed a second time.
        assert hass.bus.removed == []
        with pytest.raises(RuntimeError, match="after shutdown"):
            executor.submit(lambda: None)

    @pytest.mark.asyncio
    async def test_a_shutdown_lets_the_running_replay_return(
        self, tmp_path: Path
    ) -> None:
        """Shut down mid-run: the replay is told to stop and frees its slot.

        The next call gets a new executor.
        """
        coordinator = _coordinator_state(tmp_path, PlannerInput())
        hass = cast(_Hass, coordinator.hass)
        running = threading.Event()
        finish = threading.Event()

        def slow(request: Any, cancelled: Any) -> dict[str, Any]:
            running.set()
            finish.wait(timeout=10.0)
            return {"cancelled": cancelled()}

        with patch.object(coordinator_what_if, "run_what_if", slow):
            caller = asyncio.create_task(
                async_run_what_if(coordinator, _NOW, history_days=1, scenarios=_BIGGER)
            )
            await asyncio.to_thread(running.wait, 10.0)
            old = coordinator_what_if._Flight.executor

            shutdown_what_if()

            assert hass.bus.removed == [EVENT_HOMEASSISTANT_STOP]
            assert what_if_running()
            finish.set()
            assert await caller == {"cancelled": True}
            await _until_free()

            assert await async_run_what_if(
                coordinator, _NOW, history_days=1, scenarios=_BIGGER
            ) == {"cancelled": False}
            await _until_free()

        assert coordinator_what_if._Flight.executor is not old

    def test_shutting_down_before_the_first_call_does_nothing(self) -> None:
        shutdown_what_if()

        assert coordinator_what_if._Flight.executor is None
        assert not what_if_running()


def _priced(*days: str) -> dict[datetime, tuple[float, float]]:
    """Return a cheap night and an expensive evening for each of *days*."""
    return {
        hour: (3.0 if 17 <= index < 21 else 0.5, 0.2)
        for day in days
        for index, hour in enumerate(local_day_hours(day, _TZ))
    }


def _statistics(*days: str) -> HourlyEnergy:
    """Return 1 kWh of house load for every hour of *days*."""
    return HourlyEnergy(
        house_kwh={hour: 1.0 for day in days for hour in local_day_hours(day, _TZ)}
    )


def _with_recorder(
    tmp_path: Path, planner_input: PlannerInput | None = None
) -> CoordinatorSharedState:
    """Return a coordinator whose Home Assistant has the recorder loaded."""
    coordinator = _coordinator_state(tmp_path, planner_input)
    state = cast(Any, coordinator)
    state.hass.config.components = {"recorder"}
    state._cfg = SensorConfig()
    state._price_history_tracker = PriceHistoryTracker()
    return coordinator


class TestPriceArchive:
    """Issue #1455: the hourly prices of completed days are kept for a year."""

    @pytest.mark.asyncio
    async def test_the_prices_before_today_are_kept_and_saved_once(
        self, tmp_path: Path
    ) -> None:
        coordinator = _with_recorder(tmp_path)
        tracker = cast(Any, coordinator)._price_history_tracker
        # Today starts at 22:00 UTC of June 3rd in Copenhagen.
        yesterday = datetime(2026, 6, 3, 21, 0, tzinfo=UTC)
        today = datetime(2026, 6, 3, 22, 0, tzinfo=UTC)
        for start in (yesterday, today):
            tracker.record(start, 60, 1.5, 0.6)

        with (
            patch.object(coordinator_what_if, "hsem_now", return_value=_NOW),
            patch.object(
                coordinator_what_if, "persist_all_trackers", AsyncMock()
            ) as persist,
        ):
            await async_record_days(coordinator, _day_request(complete=False))
            await async_record_days(coordinator, _day_request(complete=False))

        assert coordinator._what_if_history.prices == {
            yesterday: pytest.approx((1.5, 0.6))
        }
        # No day was complete: the file was written for the prices, once.
        assert persist.await_count == 1


class TestRebuild:
    """Issue #1455: days without a record are rebuilt from statistics."""

    @pytest.mark.asyncio
    async def test_nothing_is_rebuilt_without_the_recorder(
        self, tmp_path: Path
    ) -> None:
        coordinator = _coordinator_state(tmp_path)
        coordinator._what_if_history.prices.update(_priced("2026-06-02"))

        assert await async_rebuild_days(coordinator, ["2026-06-02"], SITE, _NOW) == (
            {},
            {},
        )

    @pytest.mark.asyncio
    async def test_nothing_is_read_when_no_day_is_missing(self, tmp_path: Path) -> None:
        read = AsyncMock()

        with patch.object(coordinator_what_if, "async_read_hourly_energy", read):
            result = await async_rebuild_days(_with_recorder(tmp_path), [], SITE, _NOW)

        assert result == ({}, {})
        read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_day_without_full_prices_is_counted_and_not_read(
        self, tmp_path: Path
    ) -> None:
        coordinator = _with_recorder(tmp_path)
        prices = _priced("2026-06-02")
        del prices[local_day_hours("2026-06-02", _TZ)[5]]
        coordinator._what_if_history.prices.update(prices)
        read = AsyncMock()

        with patch.object(coordinator_what_if, "async_read_hourly_energy", read):
            rebuilt, reasons = await async_rebuild_days(
                coordinator, ["2026-06-01", "2026-06-02"], SITE, _NOW
            )

        assert rebuilt == {}
        assert reasons == {
            "2026-06-01": REASON_PRICE_INCOMPLETE,
            "2026-06-02": REASON_PRICE_INCOMPLETE,
        }
        read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_priced_days_are_rebuilt_from_the_statistics(
        self, tmp_path: Path
    ) -> None:
        """Kept prices and the live price history both count as real prices."""
        coordinator = _with_recorder(tmp_path)
        coordinator._what_if_history.prices.update(_priced("2026-06-01"))
        tracker = cast(Any, coordinator)._price_history_tracker
        for hour, (import_price, export_price) in _priced("2026-06-03").items():
            tracker.record(hour, 60, import_price, export_price)
        read = AsyncMock(return_value=_statistics("2026-06-01"))

        with patch.object(coordinator_what_if, "async_read_hourly_energy", read):
            rebuilt, reasons = await async_rebuild_days(
                coordinator, ["2026-06-01", "2026-06-02", "2026-06-03"], SITE, _NOW
            )

        assert list(rebuilt) == ["2026-06-01"]
        assert rebuilt["2026-06-01"].from_statistics
        assert rebuilt["2026-06-01"].import_price[18] == pytest.approx(3.0)
        assert reasons == {
            "2026-06-02": REASON_PRICE_INCOMPLETE,
            "2026-06-03": REASON_STATISTICS_INCOMPLETE,
        }
        # One read, from the first priced hour to the end of the last.
        assert read.await_args is not None
        hass, cfg, start, end = read.await_args.args
        assert hass is coordinator.hass
        assert cfg is cast(Any, coordinator)._cfg
        assert start == datetime(2026, 5, 31, 22, 0, tzinfo=UTC)
        assert end == datetime(2026, 6, 3, 22, 0, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_the_kept_prices_are_enough_without_a_price_history(
        self, tmp_path: Path
    ) -> None:
        coordinator = _with_recorder(tmp_path)
        del cast(Any, coordinator)._price_history_tracker
        coordinator._what_if_history.prices.update(_priced("2026-06-01"))
        read = AsyncMock(return_value=_statistics("2026-06-01"))

        with patch.object(coordinator_what_if, "async_read_hourly_energy", read):
            rebuilt, reasons = await async_rebuild_days(
                coordinator, ["2026-06-01"], SITE, _NOW
            )

        assert list(rebuilt) == ["2026-06-01"]
        assert reasons == {}

    @pytest.mark.asyncio
    async def test_a_recorder_error_leaves_the_days_out(self, tmp_path: Path) -> None:
        coordinator = _with_recorder(tmp_path)
        coordinator._what_if_history.prices.update(_priced("2026-06-02"))
        read = AsyncMock(side_effect=RuntimeError("database is locked"))

        with (
            patch.object(coordinator_what_if, "async_read_hourly_energy", read),
            patch.object(coordinator_what_if, "_LOGGER") as logger,
        ):
            rebuilt, reasons = await async_rebuild_days(
                coordinator, ["2026-06-01", "2026-06-02"], SITE, _NOW
            )

        assert rebuilt == {}
        assert reasons == {
            "2026-06-01": REASON_PRICE_INCOMPLETE,
            "2026-06-02": REASON_STATISTICS_INCOMPLETE,
        }
        assert "reading statistics failed" in logger.warning.call_args.args[0]

    @needs_scipy
    @pytest.mark.asyncio
    async def test_a_run_replays_rebuilt_days_next_to_measured_ones(
        self, tmp_path: Path
    ) -> None:
        """June 1st has no prices, the 2nd is rebuilt, the 3rd was measured."""
        planner_input = PlannerInput(battery_rated_capacity_kwh=10.0)
        before = copy.deepcopy(planner_input)
        coordinator = _with_recorder(tmp_path, planner_input)
        history = coordinator._what_if_history
        measured = make_day(date(2026, 6, 3))
        history.days[measured.day] = measured
        history.prices.update(_priced("2026-06-02"))
        history.history_file = str(_history_file(tmp_path))
        recorded, prices = dict(history.days), dict(history.prices)
        read = AsyncMock(return_value=_statistics("2026-06-02"))

        with patch.object(coordinator_what_if, "async_read_hourly_energy", read):
            response = await async_run_what_if(
                coordinator, _NOW, history_days=3, scenarios=_BIGGER
            )

        assert response is not None
        assert response["days_compared"] == 2
        assert response["days_measured"] == 1
        assert response["days_from_statistics"] == 1
        assert response["skipped_reasons"] == {REASON_PRICE_INCOMPLETE: 1}
        assert [(day["date"], day["source"]) for day in response["days"]] == [
            ("2026-06-02", "statistics"),
            ("2026-06-03", "measured"),
        ]
        assert response["months_compared"] == [{"month": "2026-06", "days": 2}]
        # Read-only: a rebuilt day is not kept, and nothing was written.
        assert history.days == recorded
        assert history.prices == prices
        assert coordinator._last_planner_input == before
        assert not _history_file(tmp_path).exists()
        await _until_free()

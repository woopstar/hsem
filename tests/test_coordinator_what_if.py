"""Tests for the coordinator side of ``hsem.what_if`` (issue #1432).

Two promises are pinned here.  A completed day reaches the what-if history
through the same evaluation that scores it, so the service has something to
replay long after the performance tracker dropped the slots.  And a replay
runs off the event loop, one at a time, reading only: it leaves the planner's
input, the plan and the hardware alone.  One at a time means one executor
thread: a caller that stops waiting, or a reload, does not free the slot
before the thread has returned.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

import homeassistant.util.dt as dt_util

from custom_components.hsem import coordinator_performance, coordinator_what_if
from custom_components.hsem.coordinator_performance import async_track_performance
from custom_components.hsem.coordinator_persistence import persist_all_trackers
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.coordinator_what_if import (
    HISTORY_FILE,
    ORACLE_TIME_LIMIT_S,
    PAUSE_BETWEEN_DAYS_S,
    RUN_TIME_LIMIT_S,
    WhatIfBusyError,
    WhatIfUnavailableError,
    async_record_days,
    async_run_what_if,
    build_what_if_request,
    cancel_what_if,
    init_what_if_history,
    what_if_running,
)
from custom_components.hsem.models.performance_record import (
    REASON_INSUFFICIENT_COVERAGE,
    PerformanceScore,
    PerformanceSlot,
)
from custom_components.hsem.models.performance_tracker import PerformanceTracker
from custom_components.hsem.models.planner_input import PlannerInput
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
from tests.test_coordinator_cycle_paths import _coordinator
from tests.test_coordinator_performance import _FakeHass, _finish, _live
from tests.utils.what_if_days import FIRST_DAY, SITE, make_days

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
    """Leave the process-wide replay slot free after every test."""
    yield
    coordinator_what_if._Flight.cancel = None


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
            hass=_FakeHass(tmp_path),
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
    async def test_it_solves_in_the_executor_and_only_reads(
        self, tmp_path: Path
    ) -> None:
        planner_input = PlannerInput(battery_rated_capacity_kwh=10.0)
        before = copy.deepcopy(planner_input)
        coordinator = _coordinator_state(tmp_path, planner_input)
        coordinator._what_if_history.days.update(make_days(2))
        coordinator._what_if_history.history_file = str(_history_file(tmp_path))
        recorded = dict(coordinator._what_if_history.days)

        response = await async_run_what_if(
            coordinator, _NOW, history_days=3, scenarios=_BIGGER
        )

        assert response is not None
        assert response["days_compared"] == 2
        assert response["days_skipped"] == 1
        hass = cast(_FakeHass, coordinator.hass)
        assert hass.executor_threads
        assert threading.current_thread().name not in hass.executor_threads
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

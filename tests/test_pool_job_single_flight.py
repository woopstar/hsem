"""Regression: coordinator executor jobs are single-flight and time-budgeted.

On 2026-10-15 a production install stopped publishing at 03:43:56 while its
cycles kept reaching the planner (the ``now`` of the dumped planner input moved
on to 03:46:17 and 03:50:49) and Home Assistant's executor pool stalled.  A
cycle that stops awaiting an executor job — because it is cancelled by a newer
options or OCPP refresh, or gives up — cannot stop the worker thread, and the
next cycle used to submit the same job again, so solves could stack on the
pool.

These tests run real worker threads: a job blocks on an event, so whether a
second job of the same kind starts while the first is still running is
observed, not simulated.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.hsem.planner.ev_soc_economics import EVSoCEconomicsResult
from custom_components.hsem.utils.pool_jobs import (
    JOB_EV_SOC_ECONOMICS,
    JOB_PLANNER,
    PoolJobGuard,
    PoolJobUnavailable,
)
from tests.coordinator_fixtures import make_real_coordinator

_NOW = datetime(2026, 10, 15, 1, 46, 17, tzinfo=UTC)


class _BlockingJob:
    """A blocking callable that records how many copies run at once."""

    def __init__(self, result: Any = "done") -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = 0
        self.running = 0
        self.max_running = 0
        self._lock = threading.Lock()
        self._result = result

    def __call__(self, *args: Any) -> Any:
        with self._lock:
            self.calls += 1
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        self.started.set()
        try:
            # Bounded so a failing test cannot hang the worker forever.
            self.release.wait(10)
            return self._result
        finally:
            with self._lock:
                self.running -= 1


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    """Yield a real worker pool, standing in for Home Assistant's."""
    executor = ThreadPoolExecutor(max_workers=4)
    yield executor
    executor.shutdown(wait=True, cancel_futures=True)


def _hass(pool: ThreadPoolExecutor, tmp_path: Path | None = None) -> MagicMock:
    """Return a hass whose executor jobs run on *pool*."""
    hass = MagicMock()
    if tmp_path is not None:
        hass.config.config_dir = str(tmp_path)

    def _add_executor_job(target: Callable[..., Any], *args: Any) -> Any:
        return asyncio.get_running_loop().run_in_executor(pool, target, *args)

    hass.async_add_executor_job = _add_executor_job
    return hass


async def _wait_until(predicate: Callable[[], bool]) -> None:
    """Yield to the loop until *predicate* holds (bounded)."""
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


class TestPoolJobGuard:
    """One job per kind in flight; the caller's wait is bounded."""

    @pytest.mark.asyncio
    async def test_returns_the_job_result(self, pool: ThreadPoolExecutor) -> None:
        """A job within budget behaves like a plain executor job."""
        guard = PoolJobGuard()
        result = await guard.run(_hass(pool), JOB_PLANNER, 5.0, lambda x: x * 2, 21)
        assert result == 42
        assert not guard.is_busy(JOB_PLANNER)

    @pytest.mark.asyncio
    async def test_second_job_of_a_kind_is_refused_while_first_runs(
        self, pool: ThreadPoolExecutor
    ) -> None:
        """No second worker thread starts for a kind already in flight."""
        guard, job, hass = PoolJobGuard(), _BlockingJob(), _hass(pool)
        first = asyncio.ensure_future(guard.run(hass, JOB_PLANNER, 5.0, job))
        await _wait_until(job.started.is_set)

        with pytest.raises(PoolJobUnavailable) as refused:
            await guard.run(hass, JOB_PLANNER, 5.0, job)

        assert refused.value.reason == "busy"
        # Another kind is independent.
        assert await guard.run(hass, JOB_EV_SOC_ECONOMICS, 5.0, lambda: 1) == 1
        job.release.set()
        assert await first == "done"
        assert job.calls == 1

    @pytest.mark.asyncio
    async def test_timeout_keeps_the_kind_busy_until_the_thread_returns(
        self, pool: ThreadPoolExecutor
    ) -> None:
        """Giving up waiting does not free the kind for a second solve."""
        guard, job, hass = PoolJobGuard(), _BlockingJob(), _hass(pool)

        with pytest.raises(PoolJobUnavailable) as timed_out:
            await guard.run(hass, JOB_PLANNER, 0.05, job)

        assert timed_out.value.reason == "timeout"
        assert guard.is_busy(JOB_PLANNER)
        with pytest.raises(PoolJobUnavailable):
            await guard.run(hass, JOB_PLANNER, 5.0, job)

        job.release.set()
        await _wait_until(lambda: not guard.is_busy(JOB_PLANNER))
        assert job.max_running == 1
        assert await guard.run(hass, JOB_PLANNER, 5.0, lambda: "next") == "next"

    @pytest.mark.asyncio
    async def test_cancelled_caller_keeps_the_kind_busy(
        self, pool: ThreadPoolExecutor
    ) -> None:
        """A cancelled cycle cannot let the next one stack a second solve."""
        guard, job, hass = PoolJobGuard(), _BlockingJob(), _hass(pool)
        caller = asyncio.ensure_future(guard.run(hass, JOB_PLANNER, 5.0, job))
        await _wait_until(job.started.is_set)

        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        assert guard.is_busy(JOB_PLANNER)
        with pytest.raises(PoolJobUnavailable):
            await guard.run(hass, JOB_PLANNER, 5.0, job)
        job.release.set()
        await _wait_until(lambda: not guard.is_busy(JOB_PLANNER))
        assert job.calls == 1

    @pytest.mark.asyncio
    async def test_error_of_an_abandoned_job_is_not_left_unretrieved(
        self, pool: ThreadPoolExecutor
    ) -> None:
        """A job that fails after its caller gave up is not logged as orphaned."""
        loop = asyncio.get_running_loop()
        reported: list[dict[str, Any]] = []
        loop.set_exception_handler(lambda _loop, context: reported.append(context))
        release = threading.Event()

        def _fails_late() -> None:
            release.wait(10)
            raise RuntimeError("late failure")

        guard = PoolJobGuard()
        with pytest.raises(PoolJobUnavailable):
            await guard.run(_hass(pool), JOB_PLANNER, 0.05, _fails_late)
        release.set()
        await _wait_until(lambda: not guard.is_busy(JOB_PLANNER))

        import gc

        gc.collect()
        await asyncio.sleep(0)
        loop.set_exception_handler(None)
        assert not reported


class TestEvSoCEconomicsDoesNotStack:
    """The EV sweep — up to ~8 planner solves per EV — never runs twice."""

    @pytest.mark.asyncio
    async def test_cancelled_cycle_does_not_let_a_second_sweep_start(
        self, pool: ThreadPoolExecutor, tmp_path: Path
    ) -> None:
        """Reproduces the stacking path through a superseded refresh."""
        coordinator = make_real_coordinator(hass=_hass(pool, tmp_path))
        coordinator._last_planner_input = MagicMock()
        empty = EVSoCEconomicsResult()
        sweep = _BlockingJob(result=(empty, empty))

        with patch(
            "custom_components.hsem.coordinator_ev_soc_economics."
            "_compute_both_ev_soc_economics",
            sweep,
        ):
            superseded = asyncio.ensure_future(
                coordinator._maybe_compute_ev_soc_economics(_NOW, 0, force=True)
            )
            await _wait_until(sweep.started.is_set)
            superseded.cancel()
            with pytest.raises(asyncio.CancelledError):
                await superseded

            # The next cycle's fresh plan forces another sweep.
            await coordinator._maybe_compute_ev_soc_economics(_NOW, 0, force=True)

            assert sweep.calls == 1, "a second sweep was submitted to the pool"
            sweep.release.set()
            await _wait_until(lambda: sweep.running == 0)

        assert sweep.max_running == 1
        assert coordinator._ev_soc_economics_last_computed is None


#: The planner input of the 03:46:17 cycle, dumped while it was in flight.
#: Harvested like a corpus cycle (``site-a``, no entity ids) but kept out of
#: the curated corpus, which is chosen by situation, not by incident.
_STALLED_CYCLE = Path(__file__).parent / "fixtures" / "cycle-2026-10-15-0346.json"


def test_stalled_cycle_input_solves_well_within_the_planner_budget() -> None:
    """The input of the 03:46:17 cycle that never published is not the cause.

    Replayed offline it solves like its neighbours (about 1.5 s, ~200 MB), so
    the budget only bounds a pool that is slow for other reasons; this keeps
    the input in the suite and catches a regression that makes it slow.
    """
    import time

    from custom_components.hsem.planner import run_planner
    from custom_components.hsem.utils.pool_jobs import PLANNER_JOB_BUDGET_S
    from tests.backtest.replay import load_planner_input

    inp, report = load_planner_input(_STALLED_CYCLE)
    assert report.is_faithful, report.describe()

    started = time.monotonic()
    output = run_planner(inp)
    elapsed = time.monotonic() - started

    assert output.slots
    assert elapsed < PLANNER_JOB_BUDGET_S / 3

"""Single-flight, time-budgeted executor jobs for the coordinator.

The coordinator hands its heavy, CPU-bound work — the planner solve, the
dynamic-floor reference solve and the EV SoC economics sweep — to Home
Assistant's shared executor pool.  A Python thread cannot be stopped from the
outside, so two things can go wrong there:

* an awaiting cycle that is cancelled (a superseded options or OCPP refresh)
  or gives up leaves its worker thread running, and the next cycle used to
  submit the same job again, so jobs could stack on the pool;
* a job that runs far longer than normal held the whole cycle with it.

:class:`PoolJobGuard` bounds both.  Each job *kind* has at most one job on the
pool at a time; the next request for that kind is refused until the worker
thread has actually returned, not merely until its caller stopped waiting.
The caller waits at most a wall-clock budget and then carries on with a
fallback, so one slow job degrades a cycle instead of holding it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Literal

from homeassistant.core import HomeAssistant

#: Wall-clock budget for one planner solve.  The MILP alone is capped at 2 s
#: and a production cycle takes about 1.5 s; well above that, the cycle stops
#: waiting and reuses the accepted plan.
PLANNER_JOB_BUDGET_S = 30.0

#: Wall-clock budget for the EV SoC economics sweep: up to ~8 planner solves
#: per EV, about 7 s for one EV in production.
EV_SOC_ECONOMICS_JOB_BUDGET_S = 60.0

#: Job kinds, one in flight each.
JOB_PLANNER = "planner"
JOB_DYNAMIC_FLOOR = "dynamic_floor_reference"
JOB_EV_SOC_ECONOMICS = "ev_soc_economics"

type PoolJobRefusal = Literal["busy", "timeout"]


class PoolJobUnavailable(Exception):
    """No result from a pool job this cycle; the caller must fall back.

    Attributes:
        kind: The job kind that produced no result.
        reason: ``"busy"`` when the previous job of this kind is still running
            on the pool, ``"timeout"`` when this job exceeded its budget (it
            keeps running and blocks its kind until it returns).
    """

    def __init__(self, kind: str, reason: PoolJobRefusal) -> None:
        """Record which job produced no result and why."""
        super().__init__(f"pool job {kind!r} unavailable: {reason}")
        self.kind = kind
        self.reason: PoolJobRefusal = reason


class PoolJobGuard:
    """Run executor jobs one per kind, each within a wall-clock budget."""

    def __init__(self) -> None:
        """Start with nothing in flight."""
        self._in_flight: dict[str, asyncio.Future[Any]] = {}

    def is_busy(self, kind: str) -> bool:
        """Return whether a job of *kind* is still running on the pool."""
        job = self._in_flight.get(kind)
        return job is not None and not job.done()

    async def run[T](
        self,
        hass: HomeAssistant,
        kind: str,
        budget_s: float,
        target: Callable[..., T],
        *args: Any,
    ) -> T:
        """Run ``target(*args)`` on the executor pool and return its result.

        Cancelling the caller does not release the job's slot: the slot is
        held until the worker thread returns, so a cancelled or timed-out job
        can never be joined by a second one of the same kind.

        Args:
            hass: Home Assistant instance whose executor runs the job.
            kind: Job kind; at most one job per kind is in flight.
            budget_s: Seconds to wait for the result before giving up.
            target: The blocking callable.
            *args: Positional arguments for *target*.

        Returns:
            Whatever *target* returned.

        Raises:
            PoolJobUnavailable: The previous job of this kind is still running,
                or this one exceeded *budget_s*.
        """
        if self.is_busy(kind):
            raise PoolJobUnavailable(kind, "busy")
        job: asyncio.Future[T] = asyncio.ensure_future(
            hass.async_add_executor_job(target, *args)
        )
        self._in_flight[kind] = job
        job.add_done_callback(lambda done: self._release(kind, done))
        # asyncio.wait neither cancels the job on timeout or on the caller's
        # cancellation nor, unlike shield(), logs a late error of a job
        # nobody awaits any more.
        finished, _ = await asyncio.wait({job}, timeout=budget_s)
        if not finished:
            raise PoolJobUnavailable(kind, "timeout")
        return job.result()

    def _release(self, kind: str, job: asyncio.Future[Any]) -> None:
        """Free *kind* once its worker returned, consuming an orphaned error."""
        if self._in_flight.get(kind) is job:
            del self._in_flight[kind]
        if not job.cancelled():
            # Nobody awaits a job whose caller gave up; reading the exception
            # here keeps asyncio from logging it as never retrieved.
            job.exception()

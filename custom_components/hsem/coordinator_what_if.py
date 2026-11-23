"""Keep the recorded days and run ``hsem.what_if`` off the event loop (issue #1432).

Two jobs, both read-only towards the planner and the hardware:

- after each performance evaluation, copy every completed local day whose
  slots are complete into the what-if history, and the real hourly prices of
  the completed days with them;
- on a service call, replay those days in a worker thread of their own, one
  run at a time.  A day of the asked-for history without a measured record is
  rebuilt for that run from hourly long-term statistics and the kept prices
  (issue #1455).

Nothing here is awaited by a coordinator cycle except the copy of a day, which
solves nothing.

One replay runs per process, not per coordinator: a reload makes a new
coordinator while the old one's replay may still be in its last solve, and the
slot stays taken until that thread has really returned.

The replay has its own single-worker executor (issue #1454), so a run of
minutes never holds a worker of Home Assistant's shared pool, which the planner
jobs use.  It is made on the first call and shut down on unload and when Home
Assistant stops.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, ClassVar

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback

from custom_components.hsem.coordinator_energy_backfill import (
    async_read_hourly_energy,
)
from custom_components.hsem.coordinator_persistence import persist_all_trackers
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.what_if_history import RecordedDay, WhatIfHistory
from custom_components.hsem.planner.hindsight_scoring import SiteLimits
from custom_components.hsem.utils.datetime_utils import now as hsem_now
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.misc import resolve_cycle_cost
from custom_components.hsem.utils.performance_scoring import (
    PerformanceRequest,
    usable_slots,
)
from custom_components.hsem.utils.what_if import (
    WhatIfRequest,
    WhatIfScenario,
    run_what_if,
    usable_capacity_kwh,
)
from custom_components.hsem.utils.what_if_rebuild import (
    REASON_PRICE_INCOMPLETE,
    REASON_STATISTICS_INCOMPLETE,
    priced_days,
    rebuild_days,
)

#: Name of the history file under ``.storage``.
HISTORY_FILE = "hsem_what_if_history.json"

#: Wall-clock limit for one hindsight solve, as for the performance sensors.
ORACLE_TIME_LIMIT_S = 10.0

#: Wall-clock limit for a whole replay.  Days it does not reach are reported
#: as skipped, so a run of slow solves cannot occupy the device for hours.
RUN_TIME_LIMIT_S = 600.0

#: Sleep after each replayed day.
PAUSE_BETWEEN_DAYS_S = 0.02

#: Name prefix of the replay's worker thread.
EXECUTOR_THREAD_NAME = "hsem_what_if"


class _Flight:
    """The one replay that may run, and the worker thread it runs in.

    Attributes:
        cancel: The running replay's cancel flag, or ``None`` when idle.
        executor: The replay's own single-worker executor, or ``None`` before
            the first call and after a shutdown.
        unsub_stop: What removes the listener that shuts the executor down
            when Home Assistant stops; empty when there is no listener.
    """

    cancel: ClassVar[threading.Event | None] = None
    executor: ClassVar[ThreadPoolExecutor | None] = None
    unsub_stop: ClassVar[list[Callable[[], None]]] = []


def what_if_running() -> bool:
    """Return whether a replay holds the slot, cancelled or not."""
    return _Flight.cancel is not None


def cancel_what_if() -> None:
    """Ask the running replay, if any, to stop before its next solve."""
    if _Flight.cancel is not None:
        _Flight.cancel.set()


def _executor(hass: HomeAssistant) -> ThreadPoolExecutor:
    """Return the replay's own executor, made on first use.

    Home Assistant does not unload a config entry when it stops, so the
    executor is also shut down on the stop event: a replay must not keep the
    process alive for the rest of its run.

    Args:
        hass: Home Assistant instance.
    """
    if _Flight.executor is None:
        _Flight.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=EXECUTOR_THREAD_NAME
        )

        @callback
        def stopping(_event: Event) -> None:
            """Shut down when Home Assistant stops; the listener is gone."""
            _Flight.unsub_stop.clear()
            shutdown_what_if()

        _Flight.unsub_stop.append(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stopping)
        )
    return _Flight.executor


def shutdown_what_if() -> None:
    """Stop the running replay, if any, and shut its executor down.

    Does not wait: a replay in its last solve finishes that solve, frees the
    slot and its thread then ends.  The next call makes a new executor.
    """
    cancel_what_if()
    while _Flight.unsub_stop:
        _Flight.unsub_stop.pop()()
    executor, _Flight.executor = _Flight.executor, None
    if executor is not None:
        executor.shutdown(wait=False)


class WhatIfBusyError(Exception):
    """A what-if is already running."""


class WhatIfUnavailableError(Exception):
    """The battery's limits are not known yet."""


async def init_what_if_history(history: WhatIfHistory, hass: HomeAssistant) -> None:
    """Restore the what-if history from disk, once.

    Args:
        history: The what-if history instance.
        hass: Home Assistant instance.
    """
    if history.history_file:
        return
    history.history_file = str(Path(hass.config.config_dir) / ".storage" / HISTORY_FILE)
    await history.load_history()


async def async_record_days(
    coordinator: CoordinatorSharedState, request: PerformanceRequest
) -> None:
    """Copy the completed days of an evaluation into the what-if history.

    A day is kept only when every one of its slots is complete, which is what
    the performance scoring asks before it scores a day, and only once the
    battery's limits are known.  The file is written only when a day is new
    or has changed: an evaluation that asks for the same day again, slot
    after slot, does not rewrite it.  Neither does the first evaluation after
    a restart, when the stored day is rounded and the fresh one is not.

    The real hourly prices of the completed local days are kept with them
    (issue #1455): they are what a day rebuilt from statistics is priced
    with.  Only hours before today are taken, so they change the file once a
    day.

    Args:
        coordinator: The running coordinator.
        request: The evaluation request, whose slots are already detached.
    """
    history = getattr(coordinator, "_what_if_history", None)
    if history is None or request.site is None:
        return
    await init_what_if_history(history, coordinator.hass)
    prices = getattr(coordinator, "_price_history_tracker", None)
    current = hsem_now()
    today = datetime.combine(current.date(), time(0), tzinfo=current.tzinfo)
    added = prices is not None and history.add_prices(prices.hourly_prices(), today)
    for day, keys in request.days:
        usable = usable_slots(
            [request.slots.get(key) for key in keys], request.slot_minutes
        )
        if len(usable) != len(keys):
            continue
        recorded = RecordedDay.from_slots(day, usable, request.site)
        if not recorded.matches(history.days.get(day)):
            history.add(recorded)
            added = True
    if added:
        await persist_all_trackers(coordinator, only=["_what_if_history"])


def build_what_if_request(
    coordinator: CoordinatorSharedState,
    now: datetime,
    *,
    history_days: int,
    scenarios: tuple[WhatIfScenario, ...],
    capacity_price_per_kwh: float | None = None,
) -> WhatIfRequest:
    """Return what a what-if at *now* needs, detached from the coordinator.

    Args:
        coordinator: The running coordinator.
        now: Current time in the site's zone; today is not part of the history.
        history_days: Completed local days before today to replay.
        scenarios: The batteries to compare.
        capacity_price_per_kwh: Price of one kWh of usable capacity.

    Raises:
        WhatIfUnavailableError: Before the first plan, or when the configured
            SoC limits leave no usable capacity.
    """
    planner_input = coordinator._last_planner_input
    if planner_input is None:
        raise WhatIfUnavailableError("no planner cycle has completed yet")
    site = SiteLimits.from_planner_input(planner_input)
    usable_kwh = usable_capacity_kwh(site)
    if usable_kwh <= 0.0:
        raise WhatIfUnavailableError("the configured battery has no usable capacity")
    today = now.date()
    return WhatIfRequest(
        days=tuple(
            (today - timedelta(days=offset)).isoformat()
            for offset in range(history_days, 0, -1)
        ),
        recorded=dict(coordinator._what_if_history.days),
        reasons={
            day: score.reason
            for day, score in coordinator._performance_tracker.days.items()
            if score.reason is not None
        },
        site=site,
        cycle_cost_per_kwh=resolve_cycle_cost(
            purchase_price=planner_input.battery_purchase_price,
            usable_kwh=usable_kwh,
            expected_cycles=planner_input.battery_expected_cycles,
            capacity_loss_pct=planner_input.battery_capacity_loss_pct,
            user_margin=planner_input.battery_cycle_cost_per_kwh,
        ),
        scenarios=scenarios,
        capacity_price_per_kwh=capacity_price_per_kwh,
        time_limit_s=ORACLE_TIME_LIMIT_S,
        budget_s=RUN_TIME_LIMIT_S,
        pause_s=PAUSE_BETWEEN_DAYS_S,
    )


async def async_rebuild_days(
    coordinator: CoordinatorSharedState,
    days: Sequence[str],
    site: SiteLimits,
    now: datetime,
) -> tuple[dict[str, RecordedDay], dict[str, str]]:
    """Rebuild days without a measured record from hourly statistics.

    Only reads: the kept hourly prices, the price history and the recorder's
    long-term statistics.  Statistics are asked for only when a day has a
    real price for every hour, and only from the first such day on.

    Args:
        coordinator: The running coordinator.
        days: The local days to rebuild, as ISO dates.
        site: The installation's limits as configured today.
        now: Current time in the site's zone.

    Returns:
        The rebuilt days by ISO date, and why each of the others could not be
        rebuilt.  Both are empty without the recorder: the days then keep the
        reason they had.
    """
    hass = coordinator.hass
    if not days or "recorder" not in getattr(hass.config, "components", ()):
        return {}, {}
    prices = dict(coordinator._what_if_history.prices)
    tracker = getattr(coordinator, "_price_history_tracker", None)
    if tracker is not None:
        prices.update(tracker.hourly_prices())
    priced = priced_days(days, now.tzinfo, prices)
    reasons = {day: REASON_PRICE_INCOMPLETE for day in days if day not in priced}
    if not priced:
        return {}, reasons
    try:
        hourly = await async_read_hourly_energy(
            hass,
            coordinator._cfg,
            min(hours[0] for hours in priced.values()),
            max(hours[-1] for hours in priced.values()) + timedelta(hours=1),
        )
    except Exception as exc:
        # A recorder or database error leaves the measured days to replay.
        _LOGGER.warning(
            "What-if: reading statistics failed, %d day(s) not rebuilt: %s: %r",
            len(priced),
            type(exc).__name__,
            exc,
        )
        return {}, {**reasons, **dict.fromkeys(priced, REASON_STATISTICS_INCOMPLETE)}
    rebuilt, incomplete = rebuild_days(priced, hourly, prices, site)
    return rebuilt, {**reasons, **incomplete}


async def async_run_what_if(
    coordinator: CoordinatorSharedState,
    now: datetime,
    *,
    history_days: int,
    scenarios: tuple[WhatIfScenario, ...],
    capacity_price_per_kwh: float | None = None,
) -> dict[str, Any] | None:
    """Replay the recorded days in the replay's own executor, one at a time.

    Reads a detached copy of the recorded days and the latest planner input,
    and rebuilds the days without a record from long-term statistics.  It
    writes nothing: not to the hardware, not to the plan, not to a tracker.

    Args:
        coordinator: The running coordinator.
        now: Current time in the site's zone.
        history_days: Completed local days before today to replay.
        scenarios: The batteries to compare.
        capacity_price_per_kwh: Price of one kWh of usable capacity.

    A caller that is cancelled (a script that is stopped, an automation that
    restarts) stops the replay too.  The slot is released when the executor
    thread has returned, not when the caller stopped waiting for it.

    Returns:
        The response of :func:`run_what_if`, or ``None`` when the run was
        cancelled because the integration is shutting down.

    Raises:
        WhatIfBusyError: While another what-if is running.
        WhatIfUnavailableError: When the battery's limits are not known.
    """
    if what_if_running():
        raise WhatIfBusyError("a what-if is already running")
    await init_what_if_history(coordinator._what_if_history, coordinator.hass)
    request = build_what_if_request(
        coordinator,
        now,
        history_days=history_days,
        scenarios=scenarios,
        capacity_price_per_kwh=capacity_price_per_kwh,
    )
    rebuilt, reasons = await async_rebuild_days(
        coordinator,
        [day for day in request.days if day not in request.recorded],
        request.site,
        now,
    )
    request = replace(
        request,
        recorded={**request.recorded, **rebuilt},
        reasons={**request.reasons, **reasons},
    )
    if what_if_running():
        # Another call took the slot while the history was being read.
        raise WhatIfBusyError("a what-if is already running")
    cancel = _Flight.cancel = threading.Event()

    def release(done: asyncio.Future[Any]) -> None:
        """Free the slot once the executor thread has returned."""
        if _Flight.cancel is cancel:
            _Flight.cancel = None
        if not done.cancelled():
            # Retrieved here too: the caller may have stopped waiting.
            done.exception()

    try:
        job = asyncio.get_running_loop().run_in_executor(
            _executor(coordinator.hass), run_what_if, request, cancel.is_set
        )
    except Exception:
        # Nothing was submitted, so no done-callback will free the slot.
        _Flight.cancel = None
        raise
    job.add_done_callback(release)
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        cancel.set()
        raise

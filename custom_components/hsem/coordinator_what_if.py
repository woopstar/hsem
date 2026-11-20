"""Keep the recorded days and run ``hsem.what_if`` off the event loop (issue #1432).

Two jobs, both read-only towards the planner and the hardware:

- after each performance evaluation, copy every completed local day whose
  slots are complete into the what-if history;
- on a service call, replay those days in the executor, one run at a time.

Nothing here is awaited by a coordinator cycle except the copy of a day, which
solves nothing.

One replay runs per process, not per coordinator: a reload makes a new
coordinator while the old one's replay may still be in its last solve, and the
slot stays taken until that thread has really returned.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.hsem.coordinator_persistence import persist_all_trackers
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.what_if_history import RecordedDay, WhatIfHistory
from custom_components.hsem.planner.hindsight_scoring import SiteLimits
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

#: Name of the history file under ``.storage``.
HISTORY_FILE = "hsem_what_if_history.json"

#: Wall-clock limit for one hindsight solve, as for the performance sensors.
ORACLE_TIME_LIMIT_S = 10.0

#: Wall-clock limit for a whole replay.  Days it does not reach are reported
#: as skipped, so a run of slow solves cannot occupy the device for hours.
RUN_TIME_LIMIT_S = 600.0

#: Sleep after each replayed day.
PAUSE_BETWEEN_DAYS_S = 0.02


class _Flight:
    """The one replay that may run: its cancel flag, or ``None`` when idle."""

    cancel: threading.Event | None = None


def what_if_running() -> bool:
    """Return whether a replay holds the slot, cancelled or not."""
    return _Flight.cancel is not None


def cancel_what_if() -> None:
    """Ask the running replay, if any, to stop before its next solve."""
    if _Flight.cancel is not None:
        _Flight.cancel.set()


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
    after slot, does not rewrite it.

    Args:
        coordinator: The running coordinator.
        request: The evaluation request, whose slots are already detached.
    """
    history = getattr(coordinator, "_what_if_history", None)
    if history is None or request.site is None:
        return
    await init_what_if_history(history, coordinator.hass)
    added = False
    for day, keys in request.days:
        usable = usable_slots(
            [request.slots.get(key) for key in keys], request.slot_minutes
        )
        if len(usable) != len(keys):
            continue
        recorded = RecordedDay.from_slots(day, usable, request.site)
        if history.days.get(day) != recorded:
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


async def async_run_what_if(
    coordinator: CoordinatorSharedState,
    now: datetime,
    *,
    history_days: int,
    scenarios: tuple[WhatIfScenario, ...],
    capacity_price_per_kwh: float | None = None,
) -> dict[str, Any] | None:
    """Replay the recorded days in the executor, one run at a time.

    Reads a detached copy of the recorded days and the latest planner input.
    It writes nothing: not to the hardware, not to the plan, not to a tracker.

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

    job = asyncio.ensure_future(
        coordinator.hass.async_add_executor_job(run_what_if, request, cancel.is_set)
    )
    job.add_done_callback(release)
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        cancel.set()
        raise

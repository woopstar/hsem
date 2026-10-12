"""Feed and evaluate the performance tracker (issue #1182).

Each coordinator cycle hands the tracker one sample of the meters.  Once a
slot is complete, the trailing 24 hours (and any local day that just ended)
are scored against perfect hindsight **in the executor, as a background
task**: the solve is never awaited by a cycle, cannot fail one, and reads only
a detached copy of the recorded slots.  Nothing here feeds back into the
planner or the hardware.

Extracted into its own module to keep :mod:`coordinator_tracking` under the
30 KB / 1000-line limit.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from homeassistant.core import HomeAssistant

from custom_components.hsem.coordinator_persistence import persist_all_trackers
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.performance_record import (
    PerformanceSample,
    PerformanceScore,
)
from custom_components.hsem.models.performance_tracker import PerformanceTracker
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner.hindsight_scoring import SiteLimits
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.logger import async_log
from custom_components.hsem.utils.performance_scoring import (
    PerformanceRequest,
    evaluate_performance,
)

#: Name of the history file under ``.storage``.
HISTORY_FILE = "hsem_performance_history.json"

#: Wall-clock limit for one hindsight solve on the device.  A day of
#: 15-minute slots solves in well under a second; a solve that hits the limit
#: is reported as unsolved, never as a number.
ORACLE_TIME_LIMIT_S = 10.0

#: Longest stretch of samples left unsaved.  Together with the gap tolerance
#: it decides how long Home Assistant may be down without a slot being lost.
SAVE_INTERVAL_S = 120.0

#: Longest gap between two samples that is still attributed to the slots it
#: spans.  The cumulative meters keep counting through a restart, so a short
#: gap loses nothing; a long one cannot be placed in time.
MAX_GAP_S = 600.0

#: Why a window has no score when the evaluation itself failed.
REASON_EVALUATION_FAILED = "evaluation_failed"


def build_performance_sample(
    now: datetime, live: LiveState, cfg: SensorConfig
) -> PerformanceSample:
    """Return this cycle's readings as a performance sample.

    Args:
        now: Current time (timezone-aware).
        live: The live HA entity state snapshot.
        cfg: The sensor configuration.

    Returns:
        The sample.  A price that is not authoritative and an EV power that
        is not configured are ``None``, never zero.
    """
    chargers = [live.ev] if cfg.ev.power_entity else []
    if cfg.ev_second_enabled and cfg.ev_second.power_entity:
        chargers.append(live.ev_second)
    powers = [charger.power_w for charger in chargers]
    connected = [
        charger.is_connected for charger in chargers if charger.is_connected is not None
    ]
    return PerformanceSample(
        at=now,
        grid_import_energy_kwh=live.grid_import_energy_kwh,
        grid_export_energy_kwh=live.grid_export_energy_kwh,
        soc_pct=live.huawei_batteries_soc_pct,
        import_price=(
            live.import_electricity_price
            if live.import_electricity_price_available
            else None
        ),
        export_price=(
            live.export_electricity_price
            if live.export_electricity_price_available
            else None
        ),
        in_control=(
            not live.is_forced_mode
            and not cfg.read_only
            and live.degraded_mode is DegradedMode.OK
        ),
        pv_energy_kwh=live.pv_energy_kwh,
        pv_power_w=live.solar_production_power_w,
        ev_power_w=(
            sum(power for power in powers if power is not None)
            if powers and None not in powers
            else None
        ),
        ev_connected=any(connected) if connected else None,
    )


async def init_performance_tracker(
    tracker: PerformanceTracker, hass: HomeAssistant
) -> None:
    """Restore the performance tracker from disk, once.

    Args:
        tracker: The performance tracker instance.
        hass: Home Assistant instance.
    """
    if tracker.history_file:
        return
    tracker.history_file = str(Path(hass.config.config_dir) / ".storage" / HISTORY_FILE)
    await tracker.load_history()


async def async_track_performance(
    coordinator: CoordinatorSharedState,
    now: datetime,
    live: LiveState,
    cfg: SensorConfig,
) -> None:
    """Record this cycle's sample and start an evaluation when one is due.

    Never raises: a failure here is logged and must not fail the cycle that
    called it.

    Args:
        coordinator: The running coordinator.
        now: Current time (timezone-aware).
        live: The live HA entity state snapshot.
        cfg: The sensor configuration.
    """
    try:
        tracker = getattr(coordinator, "_performance_tracker", None)
        if tracker is None:
            return
        await init_performance_tracker(tracker, coordinator.hass)
        slot_minutes = cfg.recommendation_interval_minutes
        tracker.accumulate(
            build_performance_sample(now, live, cfg),
            slot_minutes=slot_minutes,
            max_gap_seconds=max(MAX_GAP_S, 2.0 * cfg.update_interval * 60.0),
        )
        # After the sample: limits that cannot be read must not cost a slot.
        planner_input = coordinator._last_planner_input
        if planner_input is not None:
            tracker.site = SiteLimits.from_planner_input(planner_input)
        if tracker.save_due(SAVE_INTERVAL_S):
            await persist_all_trackers(coordinator, only=["_performance_tracker"])

        task = coordinator._performance_task
        if (task is None or task.done()) and tracker.needs_evaluation(
            now, slot_minutes
        ):
            request = tracker.request(
                now,
                slot_minutes=slot_minutes,
                zone=now.tzinfo,
                time_limit_s=ORACLE_TIME_LIMIT_S,
            )
            coordinator._performance_task = (
                coordinator.hass.async_create_background_task(
                    _async_evaluate(coordinator, request),
                    name="hsem_performance_evaluation",
                )
            )
    except Exception as exc:
        async_log("error", "Performance tracking failed: %s", exc)


async def _async_evaluate(
    coordinator: CoordinatorSharedState, request: PerformanceRequest
) -> None:
    """Score *request* in the executor and hand the result to the tracker.

    A solve that raises leaves the window without a score for this slot, with
    a reason, instead of being retried on every cycle.
    """
    tracker = coordinator._performance_tracker
    try:
        evaluation = await coordinator.hass.async_add_executor_job(
            evaluate_performance, request
        )
        tracker.apply(evaluation.window, evaluation.days)
    except Exception as exc:
        async_log("error", "Performance evaluation failed: %s", exc)
        tracker.apply(
            PerformanceScore(
                start=request.window[0],
                end=request.window[-1] + timedelta(minutes=request.slot_minutes),
                slot_count=len(request.window),
                reason=REASON_EVALUATION_FAILED,
            )
        )
    try:
        await persist_all_trackers(coordinator, only=["_performance_tracker"])
    except Exception as exc:
        async_log("error", "Performance tracker persistence failed: %s", exc)

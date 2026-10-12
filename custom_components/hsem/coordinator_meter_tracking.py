"""Feed the meter-based trackers once per update cycle (issue #1267).

The daily plan-vs-actual, financial and savings trackers read the grid meters,
the live prices and the battery, none of which depend on a plan.  They used to
be fed from inside the planner phase, which is skipped whenever a working mode
is forced, a required entity is missing or the load forecast is not ready: the
grid cost of such a period was dropped as a stale gap, and savings could never
be booked as "missed" because the tracker only ran while the force mode was
``auto``.

They are fed here instead, on every cycle.  What needs a plan (the daily
tracker's plan side, the charge savings) is skipped when there is none.

Extracted into its own module to keep :mod:`coordinator_cycle`,
:mod:`coordinator_planner_phase` and :mod:`coordinator_tracking` under the
30 KB / 1000-line limit.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.coordinator_persistence import persist_all_trackers
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.coordinator_tracking import (
    accumulate_daily_plan_actuals,
    accumulate_financials,
    accumulate_savings,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.logger import async_log


async def async_accumulate_meter_trackers(
    coordinator: CoordinatorSharedState,
    now: datetime,
    live: LiveState,
    cfg: SensorConfig,
    planner_output: PlannerOutput | None,
) -> None:
    """Accumulate this cycle into the daily, financial and savings trackers.

    Never raises: each tracker is fed in its own guard, so one failing cannot
    starve the others or fail the cycle.  A coordinator that does not own a
    tracker (some are built without ``__init__`` in tests) skips it.

    Args:
        coordinator: The running coordinator.
        now: Current time (timezone-aware).
        live: The live HA entity state snapshot.
        cfg: The sensor configuration.
        planner_output: This cycle's plan, or ``None`` when the planner phase
            did not run.
    """
    daily_tracker = getattr(coordinator, "_daily_tracker", None)
    financial_tracker = getattr(coordinator, "_financial_tracker", None)
    savings_tracker = getattr(coordinator, "_savings_tracker", None)

    # Daily plan-vs-actual accumulation (issue #540).
    if daily_tracker is not None:
        try:
            coordinator._daily_plan_last_accumulated = (
                await accumulate_daily_plan_actuals(
                    now=now,
                    live=live,
                    output=planner_output,
                    daily_tracker=daily_tracker,
                    daily_plan_last_accumulated=getattr(
                        coordinator, "_daily_plan_last_accumulated", None
                    ),
                    hass=coordinator.hass,
                )
            )
        except Exception as e:
            async_log("error", "Daily plan-vs-actual accumulation failed: %s", e)

    # Financial tracker accumulation (issue #599).
    if financial_tracker is not None:
        try:
            await accumulate_financials(
                now=now,
                live=live,
                financial_tracker=financial_tracker,
                hass=coordinator.hass,
                update_interval_minutes=cfg.update_interval,
            )
        except Exception as e:
            async_log("error", "Financial tracker accumulation failed: %s", e)

    # Savings tracker accumulation (issue #604).  It reads the export revenue
    # the daily tracker just accumulated.
    if savings_tracker is not None and daily_tracker is not None:
        try:
            await accumulate_savings(
                now=now,
                live=live,
                output=planner_output,
                savings_tracker=savings_tracker,
                daily_tracker=daily_tracker,
                hourly_recommendation=getattr(
                    coordinator, "_hourly_recommendation", None
                ),
                hass=coordinator.hass,
            )
        except Exception as e:
            async_log("error", "Savings tracker accumulation failed: %s", e)

    # Persist financial and savings tracker state (issues #599, #604, #890)
    # through the shared registry -- see coordinator_persistence.py.
    try:
        await persist_all_trackers(
            coordinator,
            only=[
                name
                for name, tracker in (
                    ("_financial_tracker", financial_tracker),
                    ("_savings_tracker", savings_tracker),
                )
                if tracker is not None
            ],
        )
    except Exception as e:
        async_log("error", "Tracker persistence failed: %s", e)

"""Coordinator wiring for the persisted price history (issue #1434).

Runs once per cycle, right after the price populator:

* While the price sources deliver, the real price of every slot that has
  started is recorded and the published prices are kept as *held* prices.
* When no slot has a price pair (a price sensor outage), the held prices are
  written back to the slots they cover and the cause is reported through
  degraded mode.  Slots without a held price are left uncovered, so the
  planner fills them from the weekday profile and reports them as missing.

Only source-published prices are recorded.  Held and estimated prices never
enter the history.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from homeassistant.core import HomeAssistant

from custom_components.hsem.coordinator_persistence import persist_all_trackers
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.forecast_coverage import ForecastCoverage
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.price_history_tracker import PriceHistoryTracker
from custom_components.hsem.models.price_profile import PricePair
from custom_components.hsem.utils.datetime_utils import utc_key
from custom_components.hsem.utils.logger import async_log
from custom_components.hsem.utils.persistence import read_json_history_file

_HISTORY_FILE_NAME = "hsem_price_history.json"


async def init_price_history_tracker(
    tracker: PriceHistoryTracker, hass: HomeAssistant
) -> None:
    """Point the tracker at its ``.storage`` file and load it.

    A missing or unreadable file leaves the tracker empty, which plans
    exactly as a setup without a price history.

    Args:
        tracker: The coordinator's price history tracker.
        hass: Home Assistant instance, for the config directory.
    """
    path = Path(hass.config.config_dir) / ".storage" / _HISTORY_FILE_NAME
    tracker.history_file = str(path)
    data = await asyncio.to_thread(read_json_history_file, path)
    if data is not None:
        tracker.restore_from(PriceHistoryTracker.from_dict(data))


def _missing_sides(coverage: ForecastCoverage) -> str:
    """Return which price series delivered no slot, for the degraded reason."""
    sides = [
        name
        for name, covered in (
            ("import", coverage.import_price),
            ("export", coverage.export_price),
        )
        if not covered
    ]
    # Both delivered, but no slot got a pair: neither series is usable alone.
    return " and ".join(sides or ["import", "export"])


def update_price_history(
    tracker: PriceHistoryTracker,
    recommendations: list[HourlyRecommendation],
    coverage: ForecastCoverage,
    now: datetime,
    interval_minutes: int,
) -> tuple[ForecastCoverage, str | None]:
    """Record this cycle's real prices, or fall back to held ones in an outage.

    Args:
        tracker: The price history tracker.
        recommendations: This cycle's slots, with prices as the populator
            left them.  In an outage the held prices are written to them.
        coverage: Which slots the price sources covered this cycle.
        now: Current time.
        interval_minutes: Slot width in minutes.

    Returns:
        ``(coverage, outage reason)``.  Without an outage the coverage is
        returned unchanged and the reason is ``None``.  In an outage the
        price coverage lists the slots that got a held price.
    """
    published: dict[datetime, PricePair] = {
        rec.start: (rec.import_price, rec.export_price)
        for rec in recommendations
        if coverage.has_price(rec.start)
    }
    if published or not recommendations:
        now_key = utc_key(now)
        for start, (import_price, export_price) in published.items():
            if utc_key(start) <= now_key:
                tracker.record(start, interval_minutes, import_price, export_price)
        tracker.prune(now)
        if published:
            tracker.hold(published)
        return coverage, None

    held_starts: set[datetime] = set()
    for rec in recommendations:
        pair = tracker.held_price(rec.start)
        if pair is not None:
            rec.import_price, rec.export_price = pair
            held_starts.add(utc_key(rec.start))
    reason = (
        f"Price data unavailable ({_missing_sides(coverage)} price): planning "
        f"{len(held_starts)} of {len(recommendations)} slots from held "
        f"published prices and the rest from the price history "
        f"({tracker.sample_count} recorded slots)"
    )
    if not held_starts:
        return coverage, reason
    held = frozenset(held_starts)
    return replace(coverage, import_price=held, export_price=held), reason


async def async_apply_price_history(
    coordinator: CoordinatorSharedState,
    coverage: ForecastCoverage | None,
    now: datetime,
    live: LiveState,
    interval_minutes: int,
) -> ForecastCoverage | None:
    """Run the price history step of one cycle and persist what changed.

    Args:
        coordinator: The running coordinator.
        coverage: Price and PV coverage from the populator.  ``None`` (no
            coverage information) skips the step.
        now: Current time.
        live: This cycle's live state; receives the degraded reason.
        interval_minutes: Slot width in minutes.

    Returns:
        The coverage the planner input is built from.
    """
    tracker = getattr(coordinator, "_price_history_tracker", None)
    if tracker is None or coverage is None:
        return coverage
    coverage, outage_reason = update_price_history(
        tracker, coordinator._hourly_recommendations, coverage, now, interval_minutes
    )
    if outage_reason is not None:
        live.add_degraded_reason(outage_reason)
        async_log("debug", "%s", outage_reason)
    await persist_all_trackers(coordinator, only=["_price_history_tracker"])
    return coverage

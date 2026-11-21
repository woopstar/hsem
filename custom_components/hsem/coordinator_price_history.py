"""Coordinator wiring for the persisted price history (issue #1434).

Runs once per cycle, right after the price populator:

* The real price of every started slot the price sensors published is
  recorded, and what each sensor published is kept per side as *held* prices.
* A side whose price sensor publishes nothing this cycle is in an outage.
  Its held prices are written to the slots they cover and the cause is
  reported through degraded mode.  The other side keeps its live prices.
  Slots without a held price are uncovered on the failed side, also where a
  price forecast sensor predicts them, so the planner estimates that side
  and reports the slot as missing.
* An outage that lasts longer than
  :data:`~custom_components.hsem.models.price_history_tracker.PRICE_OUTAGE_MAX_HOURS`
  is reported as a missing entity, which stops the planner.

Only real prices the price sensors published are recorded or held.
Predictions (a forecast sensor's values and the price sensor's own
``forecast``, ``predictions`` and ``forecasts`` attributes), held prices and
estimates never enter the store.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.hsem.coordinator_persistence import persist_all_trackers
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.forecast_coverage import ForecastCoverage
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.price_history_tracker import (
    PRICE_OUTAGE_MAX_HOURS,
    PriceHistoryTracker,
)
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.utils.datetime_utils import utc_key
from custom_components.hsem.utils.logger import async_log

_HISTORY_FILE_NAME = "hsem_price_history.json"

#: Label of the missing entity an expired price outage is reported as.
PRICE_OUTAGE_EXPIRED_LABEL = (
    f"electricity price data (unavailable for more than {PRICE_OUTAGE_MAX_HOURS} h)"
)


async def init_price_history_tracker(
    tracker: PriceHistoryTracker, hass: HomeAssistant
) -> None:
    """Point the tracker at its ``.storage`` file and load it.

    A missing or unreadable file leaves the tracker empty, which plans
    exactly as a setup without a price history.  An unreadable file is not
    overwritten: the load is retried before each save (issue #1446).

    Args:
        tracker: The coordinator's price history tracker.
        hass: Home Assistant instance, for the config directory.
    """
    path = Path(hass.config.config_dir) / ".storage" / _HISTORY_FILE_NAME
    tracker.history_file = str(path)
    await tracker.load_history()


def price_source(cfg: SensorConfig, snapshot: StateSnapshot | None) -> dict[str, str]:
    """Return the identity of the price source the history belongs to.

    Args:
        cfg: Sensor configuration, for the price entity IDs.
        snapshot: This cycle's state snapshot, for the entities' units.

    Returns:
        ``import_entity``, ``export_entity``, ``import_unit`` and
        ``export_unit``.  A unit is empty while its sensor reports none.
    """
    attributes = snapshot.sensor_attributes if snapshot is not None else {}
    import_entity = cfg.import_electricity_price_sensor or ""
    export_entity = cfg.export_electricity_price_sensor or ""
    return {
        "import_entity": import_entity,
        "export_entity": export_entity,
        "import_unit": _unit(attributes, import_entity),
        "export_unit": _unit(attributes, export_entity),
    }


def _unit(attributes: Any, entity_id: str) -> str:
    """Return the unit an entity reports, or an empty string without one."""
    entity = attributes.get(entity_id) if isinstance(attributes, dict) else None
    unit = entity.get("unit_of_measurement") if isinstance(entity, dict) else None
    return unit.strip() if isinstance(unit, str) else ""


@dataclass(frozen=True)
class PriceHistoryResult:
    """Outcome of the price history step of one cycle.

    Attributes:
        coverage: The coverage the planner input is built from.
        outage_reason: Degraded reason of a price outage, or ``None``.
        expired: Whether the outage has lasted longer than allowed.
    """

    coverage: ForecastCoverage
    outage_reason: str | None = None
    expired: bool = False


def _restore_held(
    tracker: PriceHistoryTracker,
    recommendations: list[HourlyRecommendation],
    side: str,
) -> frozenset[datetime]:
    """Write the held prices of a failed side to the slots they cover.

    The side's price sensor publishes nothing this cycle, so a value on a
    slot can only be a forecast sensor's prediction.  The price that was
    published for the slot wins over it, and the caller drops the side's
    other slots from the coverage.

    Args:
        tracker: The price history tracker.
        recommendations: This cycle's slots.
        side: ``"import"`` or ``"export"``.

    Returns:
        The slots that got a held price.
    """
    restored: set[datetime] = set()
    for rec in recommendations:
        price = tracker.held_price(side, rec.start)
        if price is not None:
            setattr(rec, f"{side}_price", price)
            restored.add(utc_key(rec.start))
    return frozenset(restored)


def update_price_history(
    tracker: PriceHistoryTracker,
    recommendations: list[HourlyRecommendation],
    coverage: ForecastCoverage,
    now: datetime,
    interval_minutes: int,
) -> PriceHistoryResult:
    """Record this cycle's real prices and bridge a price sensor outage.

    Args:
        tracker: The price history tracker.
        recommendations: This cycle's slots, with prices as the populator
            left them.  In an outage the held prices of the failed side are
            written to the slots they cover.
        coverage: Which slots the price sources covered this cycle, and
            what the price sensors themselves published.
        now: Current time.
        interval_minutes: Slot width in minutes.

    Returns:
        The coverage to plan from, the outage reason and whether the outage
        has expired.  Without an outage, and once it has expired, the
        coverage object is returned unchanged.
    """
    now_key = utc_key(now)
    published = {
        "import": coverage.published_import,
        "export": coverage.published_export,
    }
    for start, import_price in published["import"].items():
        export_price = published["export"].get(start)
        if export_price is not None and start <= now_key:
            tracker.record(start, interval_minutes, import_price, export_price)
    tracker.prune(now)

    # A sensor that only publishes predictions is alive: it prices its
    # slots as before and is not an outage.
    predictions_only = {
        "import": coverage.import_predictions_only,
        "export": coverage.export_predictions_only,
    }
    failed = [
        side
        for side, prices in published.items()
        if not prices and not predictions_only[side]
    ]
    outage = bool(recommendations) and bool(failed)
    hours = tracker.mark_outage(now, outage)
    for side, prices in published.items():
        if prices:
            tracker.hold(side, prices)
    if not outage:
        return PriceHistoryResult(coverage)
    if hours > PRICE_OUTAGE_MAX_HOURS:
        return PriceHistoryResult(coverage, expired=True)

    restored = {side: _restore_held(tracker, recommendations, side) for side in failed}
    held_slots = len(frozenset().union(*restored.values()))
    reason = (
        f"Price data unavailable ({' and '.join(failed)} price) for "
        f"{hours:.1f} h: planning {held_slots} of {len(recommendations)} slots "
        f"from held published prices and the rest from the price history "
        f"({tracker.sample_count} recorded slots). The planner stops after "
        f"{PRICE_OUTAGE_MAX_HOURS} h"
    )
    if not (held_slots or coverage.import_price or coverage.export_price):
        # Nothing held and no live side to keep: nothing to change.
        return PriceHistoryResult(coverage, reason)
    return PriceHistoryResult(
        replace(
            coverage,
            # A failed side keeps its held slots only: a prediction on any
            # other slot is an estimate, and the planner's own estimate
            # reports the slot as missing.
            import_price=restored.get("import", coverage.import_price),
            export_price=restored.get("export", coverage.export_price),
            single_sided_prices=True,
        ),
        reason,
    )


async def async_apply_price_history(
    coordinator: CoordinatorSharedState,
    coverage: ForecastCoverage | None,
    now: datetime,
    live: LiveState,
    cfg: SensorConfig,
) -> ForecastCoverage | None:
    """Run the price history step of one cycle and persist what changed.

    Args:
        coordinator: The running coordinator.
        coverage: Price and PV coverage from the populator.  Anything else
            (no coverage information) skips the step.
        now: Current time.
        live: This cycle's live state; receives the degraded reason, or the
            missing entity once the outage has expired.
        cfg: Sensor configuration.

    Returns:
        The coverage the planner input is built from.
    """
    tracker = getattr(coordinator, "_price_history_tracker", None)
    if tracker is None or not isinstance(coverage, ForecastCoverage):
        return coverage
    if tracker.set_source(price_source(cfg, getattr(coordinator, "_snapshot", None))):
        async_log(
            "warning",
            "The price sensors or their units changed: the price history was "
            "reset and starts recording again.",
        )
    result = update_price_history(
        tracker,
        coordinator._hourly_recommendations,
        coverage,
        now,
        int(cfg.recommendation_interval_minutes),
    )
    if result.expired:
        live.add_missing_entity(PRICE_OUTAGE_EXPIRED_LABEL)
        async_log("warning", "Missing: %s", PRICE_OUTAGE_EXPIRED_LABEL)
    elif result.outage_reason is not None:
        live.add_degraded_reason(result.outage_reason)
        async_log("debug", "%s", result.outage_reason)
    await persist_all_trackers(coordinator, only=["_price_history_tracker"])
    return result.coverage

"""Feeds and persists the battery efficiency learner (issue #1431).

Kept out of ``coordinator_cycle.py`` to respect the 30 KiB file limit.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from homeassistant.core import HomeAssistant

from custom_components.hsem.coordinator_persistence import persist_all_trackers
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.efficiency_learner import EfficiencyLearner
from custom_components.hsem.utils.logger import async_log

HISTORY_FILE = "hsem_battery_efficiency.json"

#: Longest gap between two cycles of one measurement (s), unless the update
#: interval is longer.
MAX_GAP_S = 600.0


def battery_capacity_kwh(live: LiveState) -> float | None:
    """Return the rated battery capacity in kWh, or ``None``.

    HSEM has no reading of what the battery really holds: the capacity
    learner works from ``SoC x rated capacity`` and so returns the rated
    capacity too.  The efficiency learner therefore measures against the
    rated capacity, and only its round trip is free of the capacity error.

    Args:
        live: The live HA entity state snapshot.
    """
    rated_wh = live.huawei_batteries_rated_capacity_wh
    if rated_wh is None or rated_wh <= 0.0:
        return None
    return rated_wh / 1000.0


async def init_efficiency_learner(
    learner: EfficiencyLearner, hass: HomeAssistant
) -> None:
    """Restore the efficiency learner from disk, once.

    Args:
        learner: The efficiency learner instance.
        hass: Home Assistant instance.
    """
    if learner.history_file:
        return
    learner.history_file = str(Path(hass.config.config_dir) / ".storage" / HISTORY_FILE)
    await learner.load_history()


async def async_track_battery_efficiency(
    coordinator: CoordinatorSharedState,
    now: datetime,
    live: LiveState,
    cfg: SensorConfig,
) -> None:
    """Feed this cycle's battery reading to the efficiency learner.

    Never raises: a failure here is logged and must not fail the cycle that
    called it.

    Args:
        coordinator: The running coordinator.
        now: Current time (timezone-aware).
        live: The live HA entity state snapshot.
        cfg: The sensor configuration.
    """
    try:
        learner = getattr(coordinator, "_efficiency_learner", None)
        if learner is None:
            return
        await init_efficiency_learner(learner, coordinator.hass)
        learner.update(
            now,
            live.huawei_batteries_charge_discharge_power_w,
            live.huawei_batteries_soc_pct,
            battery_capacity_kwh(live),
            max_gap_seconds=max(MAX_GAP_S, 2.0 * cfg.update_interval * 60.0),
        )
        if learner.dirty:
            await persist_all_trackers(coordinator, only=["_efficiency_learner"])
    except Exception as exc:
        async_log("error", "Battery efficiency tracking failed: %s", exc)

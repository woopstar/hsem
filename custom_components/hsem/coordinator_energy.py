"""Feed the energy tracker once per update cycle (issue #1435).

Each cycle hands the tracker one sample of the meters the coordinator already
reads: the grid and PV energy meters, the house and battery power, and the
SoC.  The tracker turns consecutive samples into daily energy totals, from
which the monthly energy sensors are built.

Extracted into its own module to keep :mod:`coordinator_tracking` under the
30 KB / 1000-line limit.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from homeassistant.core import HomeAssistant

from custom_components.hsem.coordinator_persistence import persist_all_trackers
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.energy_tracker import (
    BatterySite,
    EnergySample,
    EnergyTracker,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.logger import async_log
from custom_components.hsem.utils.misc import clamp_efficiency, resolve_cycle_cost

#: Name of the history file under ``.storage``.
HISTORY_FILE = "hsem_energy_history.json"

#: Longest stretch of samples left unsaved.
SAVE_INTERVAL_S = 120.0

#: Longest gap between two samples that is still booked.  The cumulative
#: meters keep counting through a restart, so a short gap loses nothing.
MAX_GAP_S = 600.0


def _finite(value: float | None) -> float | None:
    """Return *value* when it is a finite number, else ``None``."""
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def build_energy_sample(now: datetime, live: LiveState) -> EnergySample:
    """Return this cycle's readings as an energy sample.

    Args:
        now: Current time (timezone-aware).
        live: The live HA entity state snapshot.

    Returns:
        The sample.  A reading that is missing or not finite is ``None``.
    """
    return EnergySample(
        at=now,
        grid_import_energy_kwh=_finite(live.grid_import_energy_kwh),
        grid_export_energy_kwh=_finite(live.grid_export_energy_kwh),
        pv_energy_kwh=_finite(live.pv_energy_kwh),
        pv_power_w=_finite(live.solar_production_power_w),
        house_power_w=_finite(live.house_consumption_power_w),
        battery_power_w=_finite(live.huawei_batteries_charge_discharge_power_w),
        soc_pct=_finite(live.huawei_batteries_soc_pct),
    )


def build_battery_site(live: LiveState, cfg: SensorConfig) -> BatterySite:
    """Return the battery's capacities, efficiencies and cycle cost.

    The cycle cost is the planner's own: the depreciation per kWh of
    throughput from :func:`resolve_cycle_cost`, with the configured cycle
    cost as its floor.

    Args:
        live: The live HA entity state snapshot.
        cfg: The sensor configuration.
    """
    usable_kwh = max(_finite(live.battery_usable_capacity_kwh) or 0.0, 0.0)
    rated_kwh = max(
        (_finite(live.huawei_batteries_rated_capacity_wh) or 0.0) / 1000.0, 0.0
    )
    return BatterySite(
        rated_capacity_kwh=rated_kwh,
        usable_capacity_kwh=usable_kwh,
        charge_efficiency=clamp_efficiency(cfg.batteries_charge_efficiency),
        discharge_efficiency=clamp_efficiency(cfg.batteries_discharge_efficiency),
        cycle_cost_per_kwh=resolve_cycle_cost(
            purchase_price=cfg.batteries_purchase_price,
            usable_kwh=usable_kwh,
            expected_cycles=cfg.batteries_expected_cycles,
            capacity_loss_pct=cfg.batteries_capacity_loss_pct,
            user_margin=cfg.batteries_cycle_cost,
        ),
    )


async def init_energy_tracker(tracker: EnergyTracker, hass: HomeAssistant) -> None:
    """Restore the energy tracker from disk, once.

    Args:
        tracker: The energy tracker instance.
        hass: Home Assistant instance.
    """
    if tracker.history_file:
        return
    tracker.history_file = str(Path(hass.config.config_dir) / ".storage" / HISTORY_FILE)
    await tracker.load_history()


async def async_track_energy(
    coordinator: CoordinatorSharedState,
    now: datetime,
    live: LiveState,
    cfg: SensorConfig,
) -> None:
    """Record this cycle's sample in the energy tracker.

    Never raises: a failure here is logged and must not fail the cycle that
    called it, nor starve the other trackers.

    Args:
        coordinator: The running coordinator.
        now: Current time (timezone-aware).
        live: The live HA entity state snapshot.
        cfg: The sensor configuration.
    """
    try:
        tracker = getattr(coordinator, "_energy_tracker", None)
        if tracker is None:
            return
        await init_energy_tracker(tracker, coordinator.hass)
        tracker.accumulate(
            build_energy_sample(now, live),
            build_battery_site(live, cfg),
            max_gap_seconds=max(MAX_GAP_S, 2.0 * cfg.update_interval * 60.0),
        )
        if tracker.save_due(SAVE_INTERVAL_S):
            await persist_all_trackers(coordinator, only=["_energy_tracker"])
    except Exception as exc:
        async_log("error", "Energy tracking failed: %s", exc)

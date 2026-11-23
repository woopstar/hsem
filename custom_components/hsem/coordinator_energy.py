"""Feed the energy tracker once per update cycle (issue #1435).

Each cycle hands the tracker one sample of the meters the coordinator already
reads: the grid and PV energy meters, the house and battery power, and the
SoC.  The optional AC-side battery power is read here, since only the tracker
uses it (issue #1450).  The tracker turns consecutive samples into daily
energy totals, from which the monthly energy sensors are built.  The days
before the first measured one are taken from long-term statistics, once
(:mod:`coordinator_energy_backfill`, issue #1450).

The planner gets ``0 W`` for a house or PV power sensor that gives no reading.
The tracker must not: a sample takes those two readings only while their
entity has a numeric state, and they are missing otherwise.

Extracted into its own module to keep :mod:`coordinator_tracking` under the
30 KB / 1000-line limit.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfPower
from homeassistant.core import HomeAssistant

from custom_components.hsem.coordinator_energy_backfill import (
    async_backfill_energy_history,
)
from custom_components.hsem.coordinator_persistence import persist_all_trackers
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.energy_round_trip import (
    REASON_AC_BATTERY_POWER_NO_READINGS,
    REASON_AC_BATTERY_POWER_NOT_CONFIGURED,
    REASON_AC_BATTERY_POWER_UNIT_NOT_POWER,
)
from custom_components.hsem.models.energy_tracker import (
    BatterySite,
    EnergySample,
    EnergyTracker,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.logger import async_log
from custom_components.hsem.utils.misc import clamp_efficiency, resolve_cycle_cost
from custom_components.hsem.utils.unit_normalize import normalize_to_unit_family

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


def _measured(
    hass: HomeAssistant, entity_id: str | None, value: float | None
) -> float | None:
    """Return *value* while the entity it was read from has a numeric state.

    Args:
        hass: Home Assistant instance.
        entity_id: The entity the live state read *value* from.
        value: The value in the live state, where a failed read may be zero.
    """
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or convert_to_float(state.state) is None:
        return None
    return _finite(value)


def consumption_power_w(
    hass: HomeAssistant, live: LiveState, cfg: SensorConfig
) -> float | None:
    """Return what the house draws, EV charging included, or ``None``.

    The grid meter counts the EV chargers.  When the house power sensor does
    not (``hsem_house_power_includes_ev_charger_power`` off), their power is
    added, so the consumption covers what the import covers.  A configured
    charger without a power reading leaves the consumption unknown.

    Args:
        hass: Home Assistant instance.
        live: The live HA entity state snapshot.
        cfg: The sensor configuration.
    """
    power = _measured(hass, cfg.house_consumption_power, live.house_consumption_power_w)
    if power is None or cfg.house_power_includes_ev_charger_power:
        return power
    for ev_cfg, ev in ((cfg.ev, live.ev), (cfg.ev_second, live.ev_second)):
        if not ev_cfg.power_entity:
            continue
        ev_power = _finite(ev.power_w)
        if ev_power is None:
            return None
        power += max(ev_power, 0.0)
    return power


def read_battery_ac_power(
    hass: HomeAssistant, cfg: SensorConfig, now: datetime, max_age_seconds: float
) -> tuple[float | None, str]:
    """Return the battery's power on the AC side of the inverter, and a reason.

    Read from the optional ``hsem_battery_ac_power_entity`` and converted to
    W.  The sign is the battery power sensor's: positive while charging.

    The sensor is one the user picked, so nothing is taken on trust:

    - Only a power unit is read.  A reading in another unit (``kVA``, ``A``,
      ``kWh``, ``%``) or without a unit is not a number of watts.
    - A state last reported more than *max_age_seconds* ago is a sensor that
      stopped, not a constant power.

    Args:
        hass: Home Assistant instance.
        cfg: The sensor configuration.
        now: Current time (timezone-aware).
        max_age_seconds: Oldest report still taken for a reading.

    Returns:
        The power in W, or ``None``; and why the sensor gives no readings as
        it is set up: not configured, a unit that is not power, or, as the
        reason for every other missing reading, no readings.
    """
    entity_id = cfg.battery_ac_power_entity
    if not entity_id:
        return None, REASON_AC_BATTERY_POWER_NOT_CONFIGURED
    state = hass.states.get(entity_id)
    value = None if state is None else convert_to_float(state.state)
    if state is None or value is None:
        return None, REASON_AC_BATTERY_POWER_NO_READINGS
    watts = normalize_to_unit_family(
        value, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT), (UnitOfPower.WATT,)
    )
    if watts is None:
        return None, REASON_AC_BATTERY_POWER_UNIT_NOT_POWER
    # A state object without the time of its last report is taken as fresh.
    reported: datetime | None = getattr(state, "last_reported", None)
    if reported is not None and (now - reported).total_seconds() > max_age_seconds:
        return None, REASON_AC_BATTERY_POWER_NO_READINGS
    return _finite(watts[0]), REASON_AC_BATTERY_POWER_NO_READINGS


def build_energy_sample(
    hass: HomeAssistant,
    now: datetime,
    live: LiveState,
    cfg: SensorConfig,
    *,
    battery_ac_power_w: float | None = None,
) -> EnergySample:
    """Return this cycle's readings as an energy sample.

    Args:
        hass: Home Assistant instance.
        now: Current time (timezone-aware).
        live: The live HA entity state snapshot.
        cfg: The sensor configuration.
        battery_ac_power_w: The AC-side battery power from
            :func:`read_battery_ac_power`, or ``None``.

    Returns:
        The sample.  A reading that is missing or not finite is ``None``.
    """
    return EnergySample(
        at=now,
        grid_import_energy_kwh=_finite(live.grid_import_energy_kwh),
        grid_export_energy_kwh=_finite(live.grid_export_energy_kwh),
        pv_energy_kwh=_finite(live.pv_energy_kwh),
        pv_power_w=_measured(
            hass, cfg.solar_production_power, live.solar_production_power_w
        ),
        house_power_w=consumption_power_w(hass, live, cfg),
        battery_power_w=_finite(live.huawei_batteries_charge_discharge_power_w),
        battery_ac_power_w=battery_ac_power_w,
        soc_pct=_finite(live.huawei_batteries_soc_pct),
    )


def build_battery_site(live: LiveState, cfg: SensorConfig) -> BatterySite:
    """Return the battery's capacities, efficiencies and cycle cost.

    The cycle cost is the planner's own: the depreciation per kWh of
    throughput from :func:`resolve_cycle_cost`, with the configured cycle
    cost as its floor.  Where that comes to nothing (no purchase price,
    expected cycles or usable capacity, and no configured cost) the cycle
    cost is unknown, not zero.

    Args:
        live: The live HA entity state snapshot.
        cfg: The sensor configuration.
    """
    usable_kwh = max(_finite(live.battery_usable_capacity_kwh) or 0.0, 0.0)
    rated_kwh = max(
        (_finite(live.huawei_batteries_rated_capacity_wh) or 0.0) / 1000.0, 0.0
    )
    cycle_cost = resolve_cycle_cost(
        purchase_price=cfg.batteries_purchase_price,
        usable_kwh=usable_kwh,
        expected_cycles=cfg.batteries_expected_cycles,
        capacity_loss_pct=cfg.batteries_capacity_loss_pct,
        user_margin=cfg.batteries_cycle_cost,
    )
    return BatterySite(
        rated_capacity_kwh=rated_kwh,
        usable_capacity_kwh=usable_kwh,
        charge_efficiency=clamp_efficiency(cfg.batteries_charge_efficiency),
        discharge_efficiency=clamp_efficiency(cfg.batteries_discharge_efficiency),
        cycle_cost_per_kwh=cycle_cost if cycle_cost > 1e-9 else None,
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
        site = build_battery_site(live, cfg)
        max_gap_s = max(MAX_GAP_S, 2.0 * cfg.update_interval * 60.0)
        ac_power_w, ac_reason = read_battery_ac_power(
            coordinator.hass, cfg, now, max_gap_s
        )
        # Logged when the reason changes, so once per wrong unit.
        if (
            tracker.set_ac_sensor_reason(ac_reason)
            and ac_reason == REASON_AC_BATTERY_POWER_UNIT_NOT_POWER
        ):
            async_log(
                "warning",
                "AC-side battery power sensor %s has no power unit (W, kW). "
                "Its readings are not used for the AC-side round-trip loss.",
                cfg.battery_ac_power_entity,
            )
        tracker.accumulate(
            build_energy_sample(
                coordinator.hass, now, live, cfg, battery_ac_power_w=ac_power_w
            ),
            site,
            max_gap_seconds=max_gap_s,
        )
        backfilled = await async_backfill_energy_history(
            coordinator.hass, tracker, now, cfg, site
        )
        if backfilled or tracker.save_due(SAVE_INTERVAL_S):
            await persist_all_trackers(coordinator, only=["_energy_tracker"])
    except Exception as exc:
        async_log("error", "Energy tracking failed: %s", exc)

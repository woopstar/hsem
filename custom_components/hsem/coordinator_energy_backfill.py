"""Backfill the energy tracker from long-term statistics, once (issue #1450).

The monthly energy sensors only know the days HSEM ran.  Home Assistant keeps
hourly long-term statistics of the same sensors from before that.  On the
first cycles of a history that was never backfilled, they are read through
:class:`~custom_components.hsem.ml.statistics_reader.StatisticsReader` and
turned into daily records for the days before the first measured one.

- A measured day is never changed
  (:meth:`~custom_components.hsem.models.energy_tracker.EnergyTracker.apply_backfill`).
- Statistics are asked for in W and kWh, the units the live path normalises
  to, so the result does not depend on a sensor's current unit.
- A source without a numeric state is not read.  The backfill waits up to
  :data:`SOURCE_GRACE_S` for every configured source to report, then runs
  with the ones that do; the others are skipped.
- A quantity without statistics is skipped: its hours are not counted, and
  the month shows it as unknown, as for a day HSEM measured without it.
- The house consumption covers what the grid import covers.  When the house
  power sensor leaves the EV chargers out, an hour counts only when every
  configured charger has statistics for it too.
- A recorder error is logged and the read is tried again after
  :data:`RETRY_INTERVAL_S`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant

from custom_components.hsem.ml.statistics_reader import StatisticsReader
from custom_components.hsem.models.energy_backfill import (
    HourlyEnergy,
    build_backfill_days,
)
from custom_components.hsem.models.energy_tracker import (
    MAX_DAILY_LOG_DAYS,
    BatterySite,
    EnergyTracker,
)
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

#: How long the backfill waits for every configured source to have a state.
SOURCE_GRACE_S = 900.0

#: How long after a failed read the statistics are asked again.
RETRY_INTERVAL_S = 3600.0


@dataclass
class _Attempt:
    """When the backfill of one history file first ran and may run again."""

    first_at: float
    retry_at: float


#: Open backfills by history file.
_attempts: dict[str, _Attempt] = {}


def _has_state(hass: HomeAssistant, entity_id: str) -> bool:
    """Return whether *entity_id* has a numeric state."""
    state = hass.states.get(entity_id)
    return state is not None and convert_to_float(state.state) is not None


def _house_entities(cfg: SensorConfig) -> tuple[str, ...]:
    """Return the house power sensor and the EV chargers it leaves out."""
    if not cfg.house_consumption_power:
        return ()
    chargers = (
        ()
        if cfg.house_power_includes_ev_charger_power
        else (cfg.ev.power_entity, cfg.ev_second.power_entity)
    )
    return (cfg.house_consumption_power, *(entity for entity in chargers if entity))


def _source_entities(cfg: SensorConfig) -> list[str]:
    """Return every configured sensor the energy history is built from."""
    optional = (
        cfg.grid_import_energy_entity,
        cfg.grid_export_energy_entity,
        cfg.pv_energy_entity,
        cfg.solar_production_power,
        cfg.huawei_solar_batteries_charge_discharge_power,
    )
    return [*_house_entities(cfg), *(entity for entity in optional if entity)]


async def _read_hourly(
    hass: HomeAssistant, cfg: SensorConfig, start: datetime, end: datetime
) -> HourlyEnergy:
    """Read the hourly statistics of every source that has a state."""
    reader = StatisticsReader(hass)

    async def energy(entity_id: str | None) -> dict[datetime, float]:
        if not entity_id or not _has_state(hass, entity_id):
            return {}
        return await reader.read_hourly_energy(
            entity_id, start, end, UnitOfEnergy.KILO_WATT_HOUR
        )

    async def power(entity_id: str | None) -> dict[datetime, float]:
        """Return the hourly mean in W as kWh: a mean of W over an hour is Wh."""
        if not entity_id or not _has_state(hass, entity_id):
            return {}
        means = await reader.read_hourly_mean(entity_id, start, end, UnitOfPower.WATT)
        return {hour: watts / 1000.0 for hour, watts in means.items()}

    house: dict[datetime, float] = {}
    house_entities = _house_entities(cfg)
    if house_entities and all(_has_state(hass, entity) for entity in house_entities):
        house = await power(house_entities[0])
        for charger in house_entities[1:]:
            charged = await power(charger)
            # An hour the charger has no statistics for is unknown, not idle.
            house = {
                hour: energy_kwh + max(charged[hour], 0.0)
                for hour, energy_kwh in house.items()
                if hour in charged
            }

    # The PV meter where it has an hour, the PV power for the others.
    pv = {
        **await power(cfg.solar_production_power),
        **await energy(cfg.pv_energy_entity),
    }
    return HourlyEnergy(
        house_kwh=house,
        grid_import_kwh=await energy(cfg.grid_import_energy_entity),
        grid_export_kwh=await energy(cfg.grid_export_energy_entity),
        pv_kwh=pv,
        battery_net_kwh=await power(cfg.huawei_solar_batteries_charge_discharge_power),
    )


async def async_backfill_energy_history(
    hass: HomeAssistant,
    tracker: EnergyTracker,
    now: datetime,
    cfg: SensorConfig,
    site: BatterySite,
) -> bool:
    """Fill the days before the first measured one from statistics, once.

    Args:
        hass: Home Assistant instance.
        tracker: The energy tracker, with its history loaded.
        now: Current time (timezone-aware, local).
        cfg: The sensor configuration.
        site: The battery's capacities and cycle cost.

    Returns:
        ``True`` when the backfill ran and the tracker should be saved.
    """
    if tracker.backfill_complete or "recorder" not in getattr(
        hass.config, "components", ()
    ):
        return False
    stamp = now.timestamp()
    attempt = _attempts.setdefault(tracker.history_file, _Attempt(stamp, stamp))
    if stamp < attempt.retry_at:
        return False
    sources = _source_entities(cfg)
    waiting = [entity for entity in sources if not _has_state(hass, entity)]
    if waiting and stamp - attempt.first_at < SOURCE_GRACE_S:
        return False

    # One day more than is kept, so the oldest kept day is read in full.
    start = now - timedelta(days=MAX_DAILY_LOG_DAYS + 1)
    try:
        hourly = await _read_hourly(hass, cfg, start, now)
    except Exception as exc:
        # A recorder or database error must not stop the energy tracking.
        attempt.retry_at = stamp + RETRY_INTERVAL_S
        _LOGGER.warning(
            "Energy history backfill: reading statistics failed, next try in "
            "%d minutes: %s: %r",
            RETRY_INTERVAL_S // 60,
            type(exc).__name__,
            exc,
        )
        return False

    added = tracker.apply_backfill(
        build_backfill_days(hourly, now.tzinfo, site), now.date()
    )
    del _attempts[tracker.history_file]
    _LOGGER.info(
        "Energy history backfill: %d days added from long-term statistics "
        "(%d sources read, skipped without a state: %s)",
        added,
        len(sources) - len(waiting),
        ", ".join(waiting) or "none",
    )
    return True

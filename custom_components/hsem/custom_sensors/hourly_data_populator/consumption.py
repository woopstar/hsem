"""House consumption average population (async + snapshot).

Populates per-slot weighted average house consumption fields on
:class:`HourlyRecommendation` slots from HA energy average sensors (async)
or from a pre-collected :class:`StateSnapshot` (snapshot).
"""

from __future__ import annotations

from typing import Any

from homeassistant.exceptions import HomeAssistantError

from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot

# Delegate the spike-aware weighting algorithm to the canonical implementation
# in planner.slot_population so the logic lives in exactly one place.
from custom_components.hsem.planner.slot_population import weighted_avg_consumption
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.ha_helpers import (
    ha_get_entity_state_and_convert,
)
from custom_components.hsem.utils.logger import async_logger, log_planner
from custom_components.hsem.utils.sensornames import get_energy_average_sensor_unique_id

from . import _resolve_cached  # noqa: F401 — shared helper from package __init__


async def async_populate_avg_house_consumption(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    recommendations: list[HourlyRecommendation],
    cfg: SensorConfig,
    entity_id_cache: dict[str, str],
    entry_id: str,
) -> bool:
    """Populate per-slot weighted average house consumption from the 1/3/7/14-day sensors.

    Applies spike-aware dynamic reweighting, capping, and reliability scaling
    before writing the weighted average back to each :class:`HourlyRecommendation`.

    Args:
        sensor: The ``HSEMWorkingModeSensor`` instance for HA access and logging.
        recommendations: Mutable list of recommendation slots to update.
        cfg: Current sensor configuration (weights and interval settings).
        entity_id_cache: Mutable cache mapping unique_id → entity_id.  Updated
            in-place as new entity IDs are resolved.

    Returns:
        ``True`` on success, ``False`` when one or more required sensors are
        missing or unavailable (caller should flag ``missing_input_entities``).
    """
    w1 = cfg.house_consumption_energy_weight_1d
    w3 = cfg.house_consumption_energy_weight_3d
    w7 = cfg.house_consumption_energy_weight_7d
    w14 = cfg.house_consumption_energy_weight_14d

    for weight, name in [(w1, "1d"), (w3, "3d"), (w7, "7d"), (w14, "14d")]:
        if weight is None:
            await async_logger(
                sensor, f"Weight for {name} is None. Skipping this calculation."
            )
            return False

    await async_logger(sensor, "Calculating hourly data for energy averages...")

    scale_to_interval = 60.0 / cfg.recommendation_interval_minutes
    w_total_config = int(w1) + int(w3) + int(w7) + int(w14)

    for h in range(24):
        hour_end = (h + 1) % 24

        uid_1d = get_energy_average_sensor_unique_id(entry_id, h, hour_end, 1)
        uid_3d = get_energy_average_sensor_unique_id(entry_id, h, hour_end, 3)
        uid_7d = get_energy_average_sensor_unique_id(entry_id, h, hour_end, 7)
        uid_14d = get_energy_average_sensor_unique_id(entry_id, h, hour_end, 14)

        # Resolve entity IDs (cached)
        eid_1d = await _resolve_cached(sensor, entity_id_cache, uid_1d)
        eid_3d = await _resolve_cached(sensor, entity_id_cache, uid_3d)
        eid_7d = await _resolve_cached(sensor, entity_id_cache, uid_7d)
        eid_14d = await _resolve_cached(sensor, entity_id_cache, uid_14d)

        if None in (eid_1d, eid_3d, eid_7d, eid_14d):
            await async_logger(
                sensor,
                "One of the required sensors for average house consumptions load is "
                "not ready/found. Waiting for next update.",
            )
            return False

        # Fetch values
        try:
            v1 = convert_to_float(
                ha_get_entity_state_and_convert(sensor, eid_1d, "float", 3)
            )
            v3 = convert_to_float(
                ha_get_entity_state_and_convert(sensor, eid_3d, "float", 3)
            )
            v7 = convert_to_float(
                ha_get_entity_state_and_convert(sensor, eid_7d, "float", 3)
            )
            v14 = convert_to_float(
                ha_get_entity_state_and_convert(sensor, eid_14d, "float", 3)
            )
        except (HomeAssistantError, ValueError, TypeError) as exc:
            await async_logger(
                sensor,
                f"Sensor read failed for hour {h} energy averages "
                f"(entity_ids={eid_1d},{eid_3d},{eid_7d},{eid_14d}): "
                f"{type(exc).__name__}: {exc!r}",
            )
            v1 = v3 = v7 = v14 = None

        if None in (v1, v3, v7, v14):
            await async_logger(
                sensor,
                "One of the required sensors for average house consumptions load is "
                "not ready/found. Waiting for next update.",
            )
            return False

        # Narrow types for pyright: the None check above ensures all values
        # are float at this point.
        assert v1 is not None and v3 is not None and v7 is not None and v14 is not None

        if w_total_config == 0:
            await async_logger(sensor, "All weights sum to 0. Skipping calculation.")
            continue

        avg, _ = weighted_avg_consumption(
            v1,
            v3,
            v7,
            v14,
            int(w1),
            int(w3),
            int(w7),
            int(w14),
        )

        for obj in recommendations:
            if int(obj.start.hour) == int(h):
                obj.avg_house_consumption_kwh = round(avg / scale_to_interval, 3)
                obj.avg_house_consumption_1d_kwh = round(v1 / scale_to_interval, 3)
                obj.avg_house_consumption_3d_kwh = round(v3 / scale_to_interval, 3)
                obj.avg_house_consumption_7d_kwh = round(v7 / scale_to_interval, 3)
                obj.avg_house_consumption_14d_kwh = round(v14 / scale_to_interval, 3)

    return True


# ---------------------------------------------------------------------------
# Snapshot-based average consumption population
# ---------------------------------------------------------------------------


def populate_avg_house_consumption_from_snapshot(
    recommendations: list[HourlyRecommendation],
    snapshot: StateSnapshot,
    cfg: SensorConfig,
    energy_average_entity_id_cache: dict[str, str],
    entry_id: str,
) -> bool:
    """Populate per-slot house consumption averages from a pre-collected snapshot.

    Synchronous — uses :attr:`StateSnapshot.energy_average_values` which was
    populated by :func:`~state_collector.async_collect_all_states`.

    The energy average sensors are HSEM's own entities.  When they are not yet
    registered or not reporting state (e.g. during the very first coordinator
    cycle) the function simply returns ``False``.  The caller **must not** treat
    this as a ``missing_input_entities`` error — it is a transient condition
    that resolves on the next cycle once the sensors are available.

    Args:
        recommendations: Mutable list of recommendation slots to update.
        snapshot: Pre-collected state snapshot with ``energy_average_values``.
        cfg: Current sensor configuration (weights and interval settings).
        energy_average_entity_id_cache: Cache mapping unique_id → entity_id,
            populated during snapshot collection.

    Returns:
        ``True`` when all 24 hours were populated.  ``False`` when one or more
        sensors are not yet ready — the caller should retry on the next cycle
        **without** flagging ``missing_input_entities``.
    """
    w1 = cfg.house_consumption_energy_weight_1d
    w3 = cfg.house_consumption_energy_weight_3d
    w7 = cfg.house_consumption_energy_weight_7d
    w14 = cfg.house_consumption_energy_weight_14d

    for weight, _name in [(w1, "1d"), (w3, "3d"), (w7, "7d"), (w14, "14d")]:
        if weight is None:
            log_planner(
                "warning",
                "[avg] snapshot populator: weight %s is None, returning False",
                _name,
            )
            return False

    scale_to_interval = 60.0 / cfg.recommendation_interval_minutes
    w_total_config = int(w1) + int(w3) + int(w7) + int(w14)

    for h in range(24):
        hour_end = (h + 1) % 24

        uid_1d = get_energy_average_sensor_unique_id(entry_id, h, hour_end, 1)
        uid_3d = get_energy_average_sensor_unique_id(entry_id, h, hour_end, 3)
        uid_7d = get_energy_average_sensor_unique_id(entry_id, h, hour_end, 7)
        uid_14d = get_energy_average_sensor_unique_id(entry_id, h, hour_end, 14)

        log_planner(
            "debug",
            "[avg] snapshot populator: processing hour %d with UIDs "
            "1d=%s, 3d=%s, 7d=%s, 14d=%s",
            h,
            uid_1d,
            uid_3d,
            uid_7d,
            uid_14d,
        )

        eid_1d = energy_average_entity_id_cache.get(uid_1d)
        eid_3d = energy_average_entity_id_cache.get(uid_3d)
        eid_7d = energy_average_entity_id_cache.get(uid_7d)
        eid_14d = energy_average_entity_id_cache.get(uid_14d)

        log_planner(
            "debug",
            "[avg] hour %d: resolved entity IDs from cache "
            "(1d=%s, 3d=%s, 7d=%s, 14d=%s)",
            h,
            eid_1d,
            eid_3d,
            eid_7d,
            eid_14d,
        )

        if eid_1d is None or eid_3d is None or eid_7d is None or eid_14d is None:
            log_planner(
                "debug",
                "[avg] hour %d: missing entity IDs in cache (1d=%s, 3d=%s, 7d=%s, 14d=%s), returning False",
                h,
                eid_1d,
                eid_3d,
                eid_7d,
                eid_14d,
            )
            return False

        v1 = snapshot.energy_average_values.get(eid_1d)
        v3 = snapshot.energy_average_values.get(eid_3d)
        v7 = snapshot.energy_average_values.get(eid_7d)
        v14 = snapshot.energy_average_values.get(eid_14d)

        log_planner(
            "debug",
            "[avg] hour %d: fetched energy average values from snapshot. values are (1d=%s, 3d=%s, 7d=%s, 14d=%s)",
            h,
            v1,
            v3,
            v7,
            v14,
        )

        if None in (v1, v3, v7, v14):
            log_planner(
                "debug",
                "[avg] hour %d: values missing in snapshot for eids "
                "(1d=%s=%s, 3d=%s=%s, 7d=%s=%s, 14d=%s=%s), returning False",
                h,
                eid_1d,
                v1,
                eid_3d,
                v3,
                eid_7d,
                v7,
                eid_14d,
                v14,
            )
            return False

        # Narrow types for pyright: the None check above guarantees all
        # values are float at this point.
        assert v1 is not None and v3 is not None and v7 is not None and v14 is not None

        # At this point all values are float
        if w_total_config == 0:
            log_planner(
                "debug",
                "[avg] hour %d: all weights sum to 0, returning False",
                h,
            )
            return False

        avg, _ = weighted_avg_consumption(
            v1,
            v3,
            v7,
            v14,
            int(w1),
            int(w3),
            int(w7),
            int(w14),
        )

        log_planner(
            "debug",
            "[avg] hour %d: calculated weighted average consumption %s kWh (scaled to interval: %s kWh)",
            h,
            round(avg, 3),
            round(avg / scale_to_interval, 3),
        )

        for obj in recommendations:
            if int(obj.start.hour) == int(h):
                obj.avg_house_consumption_kwh = round(avg / scale_to_interval, 3)
                obj.avg_house_consumption_1d_kwh = round(v1 / scale_to_interval, 3)
                obj.avg_house_consumption_3d_kwh = round(v3 / scale_to_interval, 3)
                obj.avg_house_consumption_7d_kwh = round(v7 / scale_to_interval, 3)
                obj.avg_house_consumption_14d_kwh = round(v14 / scale_to_interval, 3)

    log_planner(
        "debug",
        "[avg] snapshot populator: returning True after processing 24 hours",
    )
    return True


# _compute_weighted_average has been removed. The canonical implementation lives
# in planner.slot_population.weighted_avg_consumption and is imported above.

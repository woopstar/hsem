"""Hourly data populator for HSEMWorkingModeSensor.

Single responsibility: populate a list of :class:`HourlyRecommendation` slots
with prices, Solcast PV estimates, and weighted house-consumption averages by
reading from Home Assistant sensor attributes.

All functions in this module take a ``sensor`` argument for HA access but have
**no** side-effects beyond writing to the ``HourlyRecommendation`` list they
receive.  No hardware writes occur here.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.const import (
    BASELINE_7D_SHARE,
    BASELINE_14D_SHARE,
    CAP7_DOWN,
    CAP7_UP,
    CAP14_DOWN,
    CAP14_UP,
    CHANGE3_LIMIT_DOWN_FACTOR,
    CHANGE3_LIMIT_UP_FACTOR,
    CHANGE_LIMIT_DOWN_FACTOR,
    CHANGE_LIMIT_UP_FACTOR,
    RELIABILITY_EPS,
    RELIABILITY_SCALE_STRENGTH,
    SPIKE1_RATIO_MAX,
    SPIKE1_RATIO_MIN,
    SPIKE1_REDIST_TO_3D,
    SPIKE1_REDIST_TO_7D,
    SPIKE1_REDIST_TO_14D,
    SPIKE1_REDUCE_FRACTION_MAX,
    SPIKE3_RATIO_MAX,
    SPIKE3_RATIO_MIN,
    SPIKE3_REDIST_TO_7D,
    SPIKE3_REDIST_TO_14D,
    SPIKE3_REDUCE_FRACTION_MAX,
    SPIKE7_RATIO_MAX,
    SPIKE7_RATIO_MIN,
    SPIKE7_REDIST_TO_14D,
    SPIKE7_REDUCE_FRACTION_MAX,
    SPIKE14_RATIO_MAX,
    SPIKE14_RATIO_MIN,
    SPIKE14_REDIST_TO_7D,
    SPIKE14_REDUCE_FRACTION_MAX,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.misc import (
    async_logger,
    async_resolve_entity_id_from_unique_id,
    convert_to_float,
    ha_get_entity_state_and_convert,
)
from custom_components.hsem.utils.sensornames import get_energy_average_sensor_unique_id


async def async_populate_price_and_solcast(
    sensor,
    recommendations: list[HourlyRecommendation],
    cfg: SensorConfig,
    tz,
) -> None:
    """Populate import/export prices and Solcast PV estimates into recommendation slots.

    Reads attribute arrays from the EDS and Solcast sensors, matches each data
    point to the corresponding :class:`HourlyRecommendation` by datetime, and
    writes the value into the appropriate field.

    Args:
        sensor: The ``HSEMWorkingModeSensor`` instance for HA access and logging.
        recommendations: Mutable list of recommendation slots to update.
        cfg: Current sensor configuration.
        tz: Timezone (``ZoneInfo`` instance) for datetime normalization.
    """
    eds_share = (
        cfg.energi_data_service_update_interval / cfg.recommendation_interval_minutes
    )
    solcast_share = 60.0 / cfg.recommendation_interval_minutes

    # Import price
    await _async_update_hourly_field(
        sensor,
        recommendations,
        cfg.energi_data_service_import,
        "import_price",
        eds_share,
        cfg.solcast_pv_forecast_forecast_likelihood,
        tz,
    )
    # Export price
    await _async_update_hourly_field(
        sensor,
        recommendations,
        cfg.energi_data_service_export,
        "export_price",
        eds_share,
        cfg.solcast_pv_forecast_forecast_likelihood,
        tz,
    )
    # Solcast today
    await _async_update_hourly_field(
        sensor,
        recommendations,
        cfg.solcast_pv_forecast_forecast_today,
        "solcast_pv_estimate",
        solcast_share,
        cfg.solcast_pv_forecast_forecast_likelihood,
        tz,
    )
    # Solcast tomorrow
    await _async_update_hourly_field(
        sensor,
        recommendations,
        cfg.solcast_pv_forecast_forecast_tomorrow,
        "solcast_pv_estimate",
        solcast_share,
        cfg.solcast_pv_forecast_forecast_likelihood,
        tz,
    )


async def async_populate_avg_house_consumption(
    sensor,
    recommendations: list[HourlyRecommendation],
    cfg: SensorConfig,
    entity_id_cache: dict[str, str],
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

        uid_1d = get_energy_average_sensor_unique_id(h, hour_end, 1)
        uid_3d = get_energy_average_sensor_unique_id(h, hour_end, 3)
        uid_7d = get_energy_average_sensor_unique_id(h, hour_end, 7)
        uid_14d = get_energy_average_sensor_unique_id(h, hour_end, 14)

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
        except Exception:
            v1 = v3 = v7 = v14 = None

        if None in (v1, v3, v7, v14):
            await async_logger(
                sensor,
                "One of the required sensors for average house consumptions load is "
                "not ready/found. Waiting for next update.",
            )
            return False

        if w_total_config == 0:
            await async_logger(sensor, "All weights sum to 0. Skipping calculation.")
            continue

        avg = _compute_weighted_average(
            v1,
            v3,
            v7,
            v14,
            int(w1),
            int(w3),
            int(w7),
            int(w14),
            w_total_config,
        )

        for obj in recommendations:
            if int(obj.start.hour) == int(h):
                obj.avg_house_consumption = round(avg / scale_to_interval, 3)
                obj.avg_house_consumption_1d = round(v1 / scale_to_interval, 3)
                obj.avg_house_consumption_3d = round(v3 / scale_to_interval, 3)
                obj.avg_house_consumption_7d = round(v7 / scale_to_interval, 3)
                obj.avg_house_consumption_14d = round(v14 / scale_to_interval, 3)

    return True


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _resolve_cached(
    sensor,
    cache: dict[str, str],
    unique_id: str,
) -> str | None:
    """Return the entity_id for ``unique_id``, resolving and caching if needed."""
    if unique_id not in cache:
        entity_id = await async_resolve_entity_id_from_unique_id(sensor, unique_id)
        if entity_id is not None:
            cache[unique_id] = entity_id
    return cache.get(unique_id)


async def _async_update_hourly_field(
    sensor,
    recommendations: list[HourlyRecommendation],
    sensor_id: str | None,
    field_name: str,
    share: float,
    solcast_likelihood_key: str,
    tz,
) -> None:
    """Match sensor attribute data to recommendation slots and write one field.

    Args:
        sensor: The ``HSEMWorkingModeSensor`` instance.
        recommendations: Mutable recommendation list.
        sensor_id: Entity ID to read attributes from, or None (no-op).
        field_name: Attribute name on :class:`HourlyRecommendation` to set.
        share: Divisor applied to each raw value (accounts for sub-hourly slots).
        solcast_likelihood_key: Attribute key for Solcast PV estimate field.
        tz: Local timezone for datetime normalization.
    """
    if sensor_id is None:
        return

    sensor_state = sensor.hass.states.get(sensor_id)
    if not sensor_state:
        await async_logger(sensor, f"Input sensor {sensor_id} was not found for data.")
        return

    # Each source exposes a different attribute key / time-key / value-key
    data_sources: dict[str, list[dict[str, str]]] = {
        "forecast": [{"k": "hour", "v": "price"}],
        "raw_tomorrow": [{"k": "hour", "v": "price"}],
        "raw_today": [{"k": "hour", "v": "price"}],
        "prices": [{"k": "start", "v": "price"}],
        "prices_today": [
            {"k": "start", "v": "price"},
            {"k": "time", "v": "price"},
        ],
        "prices_tomorrow": [
            {"k": "start", "v": "price"},
            {"k": "time", "v": "price"},
        ],
        "detailedHourly": [{"k": "period_start", "v": solcast_likelihood_key}],
        "detailedForecast": [{"k": "period_start", "v": solcast_likelihood_key}],
        "data": [{"k": "start_time", "v": "price_per_kwh"}],
    }

    for attr, kv_list in data_sources.items():
        sensor_data = sensor_state.attributes.get(attr) or []
        if not sensor_data:
            continue

        await async_logger(sensor, f"Updating data for {field_name}...")

        for data in sensor_data:
            for kv in kv_list:
                raw_time = data.get(kv["k"])
                if not raw_time:
                    continue

                if isinstance(raw_time, datetime):
                    dt_key = raw_time
                else:
                    dt_key = datetime.fromisoformat(str(raw_time))

                try:
                    dt_key = dt_key.replace(
                        minute=0, second=0, microsecond=0
                    ).astimezone(tz)
                except Exception:  # noqa: TRY302
                    continue

                value = convert_to_float(data.get(kv["v"]))
                if value is None:
                    continue

                value = value / share

                for obj in recommendations:
                    obj_hour = obj.start.replace(
                        minute=0, second=0, microsecond=0
                    ).astimezone(tz)
                    if obj.start.date() == dt_key.date() and obj_hour == dt_key:
                        setattr(obj, field_name, round(value, 5))


def _compute_weighted_average(
    v1: float,
    v3: float,
    v7: float,
    v14: float,
    w1: int,
    w3: int,
    w7: int,
    w14: int,
    w_total: int,
) -> float:
    """Apply spike-aware dynamic reweighting and return the weighted consumption average.

    This is the extracted pure-Python core of
    ``_async_calculate_avg_house_consumption`` — the HA I/O is handled by the
    caller; this function only does arithmetic.

    Args:
        v1..v14: Raw consumption values for 1/3/7/14-day windows (kWh/hour).
        w1..w14: Configured integer weights (percent, must sum to w_total).
        w_total: Expected total of all weights (used for normalisation).

    Returns:
        Weighted average consumption in kWh/hour.
    """
    # --- Mild capping between 7d and 14d ---
    v7_eff = max(CAP7_DOWN * v14, min(v7, CAP7_UP * v14))
    v14_eff = max(CAP14_DOWN * v7_eff, min(v14, CAP14_UP * v7_eff))

    # --- Baseline capping for 1d/3d ---
    baseline = BASELINE_7D_SHARE * v7_eff + BASELINE_14D_SHARE * v14_eff
    v1_eff = max(
        baseline * CHANGE_LIMIT_DOWN_FACTOR, min(v1, baseline * CHANGE_LIMIT_UP_FACTOR)
    )
    v3_eff = max(
        baseline * CHANGE3_LIMIT_DOWN_FACTOR,
        min(v3, baseline * CHANGE3_LIMIT_UP_FACTOR),
    )

    # --- Spike severity 0..1 ---
    def _sev(ratio, lo, hi):
        if ratio <= lo:
            return 0.0
        if ratio >= hi:
            return 1.0
        return (ratio - lo) / (hi - lo)

    r1 = (v1 / v7_eff) if v7_eff > 0 else 1.0
    r3 = (v3 / v7_eff) if v7_eff > 0 else 1.0
    r7 = (v7_eff / v14_eff) if v14_eff > 0 else 1.0
    r14 = (v14_eff / v7_eff) if v7_eff > 0 else 1.0

    sev1 = _sev(r1, SPIKE1_RATIO_MIN, SPIKE1_RATIO_MAX)
    sev3 = _sev(r3, SPIKE3_RATIO_MIN, SPIKE3_RATIO_MAX)
    sev7 = _sev(r7, SPIKE7_RATIO_MIN, SPIKE7_RATIO_MAX)
    sev14 = _sev(r14, SPIKE14_RATIO_MIN, SPIKE14_RATIO_MAX)

    # --- Dynamic reweighting ---
    freed1 = w1 * (SPIKE1_REDUCE_FRACTION_MAX * sev1)
    w1_eff = w1 - freed1
    w3_eff = w3 + freed1 * SPIKE1_REDIST_TO_3D
    w7_eff = w7 + freed1 * SPIKE1_REDIST_TO_7D
    w14_eff = w14 + freed1 * SPIKE1_REDIST_TO_14D

    freed3 = w3_eff * (SPIKE3_REDUCE_FRACTION_MAX * sev3)
    w3_eff -= freed3
    w7_eff += freed3 * SPIKE3_REDIST_TO_7D
    w14_eff += freed3 * SPIKE3_REDIST_TO_14D

    freed7 = w7_eff * (SPIKE7_REDUCE_FRACTION_MAX * sev7)
    w7_eff -= freed7
    w14_eff += freed7 * SPIKE7_REDIST_TO_14D

    freed14 = w14_eff * (SPIKE14_REDUCE_FRACTION_MAX * sev14)
    w14_eff -= freed14
    w7_eff += freed14 * SPIKE14_REDIST_TO_7D

    # --- Reliability scaling ---
    eps = RELIABILITY_EPS
    strength = RELIABILITY_SCALE_STRENGTH
    rel1 = 1.0 + (1.0 / (eps + abs(v1_eff - v7_eff)) - 1.0) * strength
    rel3 = 1.0 + (1.0 / (eps + abs(v3_eff - v7_eff)) - 1.0) * strength
    rel7 = 1.0 + (1.0 / (eps + abs(v7_eff - v14_eff)) - 1.0) * strength
    rel14 = 1.0 + (1.0 / (eps + abs(v14_eff - v7_eff)) - 1.0) * strength

    w1_eff *= rel1
    w3_eff *= rel3
    w7_eff *= rel7
    w14_eff *= rel14

    w_sum = w1_eff + w3_eff + w7_eff + w14_eff
    if w_sum > 0:
        scale = w_total / w_sum
        w1_eff *= scale
        w3_eff *= scale
        w7_eff *= scale
        w14_eff *= scale
    else:
        w1_eff, w3_eff, w7_eff, w14_eff = float(w1), float(w3), float(w7), float(w14)

    return round(
        v1_eff * (w1_eff / 100)
        + v3_eff * (w3_eff / 100)
        + v7_eff * (w7_eff / 100)
        + v14_eff * (w14_eff / 100),
        3,
    )

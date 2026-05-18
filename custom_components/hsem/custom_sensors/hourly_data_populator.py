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

from homeassistant.exceptions import HomeAssistantError

from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.sensor_config import SensorConfig

# Delegate the spike-aware weighting algorithm to the canonical implementation
# in planner.slot_population so the logic lives in exactly one place.
from custom_components.hsem.planner.slot_population import weighted_avg_consumption
from custom_components.hsem.utils.datetime_utils import normalize_datetime
from custom_components.hsem.utils.logger import async_logger
from custom_components.hsem.utils.misc import (
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
        tz: Timezone (``tzinfo`` instance) for datetime normalization.
    """
    # ---------------------------------------------------------------------------
    # Price interval semantics
    # ---------------------------------------------------------------------------
    # EDS prices are a *rate* (currency/kWh), not an energy quantity, so they
    # must NOT be summed across slots.  However, the EDS sensor publishes one
    # value per update interval (either 15 min or 60 min) while HSEM may plan
    # at a finer resolution (e.g. 15-min slots inside a 60-min EDS interval).
    #
    # `eds_share` converts between the two resolutions:
    #
    #   eds_share = energi_data_service_update_interval / recommendation_interval_minutes
    #
    # In `_async_update_hourly_field` (below) each raw EDS value is divided by
    # `eds_share` before writing to the per-slot recommendation object.  This
    # stores the price *scaled to one recommendation slot's share* of the EDS
    # update interval.
    #
    # The inverse multiply (`rec.import_price * eds_share`) is applied later in
    # `coordinator._build_planner_input` to recover the original price rate
    # before passing it to the planner engine.
    #
    # Common configurations and their eds_share values:
    #   EDS 60 min  / slots 15 min  →  eds_share = 4.0
    #   EDS 15 min  / slots 15 min  →  eds_share = 1.0  (no scaling)
    #   EDS 60 min  / slots 60 min  →  eds_share = 1.0  (no scaling)
    eds_share = (
        cfg.energi_data_service_update_interval / cfg.recommendation_interval_minutes
    )
    # Solcast forecasts are always given as hourly totals (Wh/h), so the share
    # factor is always relative to 60 minutes regardless of EDS configuration.
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
        tz: Local timezone (``tzinfo`` instance) for datetime normalization.
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
                    # Normalize to HA-local timezone, strip sub-minute precision
                    dt_key = normalize_datetime(dt_key).replace(minute=0, second=0)
                except (ValueError, OSError):  # noqa: TRY302
                    # Skip data points with unparseable or non-local timestamps
                    continue

                value = convert_to_float(data.get(kv["v"]))
                if value is None:
                    continue

                # Scale raw value down to one recommendation-slot's share of the
                # source update interval.
                #
                # For EDS prices:   share = eds_share (EDS interval / slot interval)
                #   EDS 60 min / slot 15 min → share=4 → store price/4 per slot
                #   EDS 15 min / slot 15 min → share=1 → store price unchanged
                #
                # For Solcast PV:   share = solcast_share (60 / slot interval)
                #   60-min hourly forecast / slot 15 min → share=4 → store Wh/4 per slot
                #   60-min hourly forecast / slot 60 min → share=1 → store Wh unchanged
                #
                # The coordinator's `_build_planner_input` applies the inverse multiply
                # (×eds_share) before handing prices/PV to the planner engine, so the
                # divide here and the multiply there cancel exactly and the planner always
                # receives the original hourly-equivalent rate or energy quantity.
                value = value / share

                for obj in recommendations:
                    obj_hour = normalize_datetime(obj.start).replace(minute=0, second=0)
                    if obj.start.date() == dt_key.date() and obj_hour == dt_key:
                        setattr(obj, field_name, round(value, 5))


# _compute_weighted_average has been removed. The canonical implementation lives
# in planner.slot_population.weighted_avg_consumption and is imported above.

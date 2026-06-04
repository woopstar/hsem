"""ML consumption populator — bridges ML predictions into the planner pipeline.

Called by the coordinator during the update cycle (step 5 alternative) when
``ml_consumption_enabled`` is ``True``.  Reads historical energy data from the
recorder, trains the predictor, and writes per-slot consumption predictions
into the :class:`HourlyRecommendation` list.

Analogous to
:func:`custom_sensors.hourly_data_populator.populate_avg_house_consumption_from_snapshot`
but uses ML predictions instead of rolling-average sensor states.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.core import HomeAssistant

from custom_components.hsem.ml.consumption_predictor import ConsumptionPredictor
from custom_components.hsem.ml.history_reader import HistoryReader
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.logger import HSEM_LOGGER

# Cache for full history fetch — avoids querying 90 days of recorder
# data on every 1–5 minute coordinator cycle.
_last_history_fetch: datetime | None = None
_cached_history: list[tuple[datetime, int, float]] | None = None
_last_temp_fetch: datetime | None = None
_cached_temps: dict[datetime, float] | None = None
_MIN_HISTORY_REFRESH = timedelta(minutes=60)


async def populate_ml_house_consumption(
    hass: HomeAssistant,
    recommendations: list[HourlyRecommendation],
    cfg: SensorConfig,
    predictor: ConsumptionPredictor | None = None,
) -> tuple[bool, ConsumptionPredictor | None]:
    """Populate per-slot house consumption using ML predictions from recorder history.

    The predictor is cached across cycles via the ``predictor`` parameter.
    A retrain gate skips the matrix solve when fewer than
    ``retrain_min_new_samples`` new observations have arrived since the last fit.

    Args:
        hass: The Home Assistant instance (used for recorder access).
        recommendations: Mutable list of recommendation slots to update.
        cfg: Current sensor configuration.
        predictor: The predictor instance from the previous cycle, or
            ``None`` on the first call.

    Returns:
        A ``(success, predictor)`` tuple.
    """
    if not cfg.grid_import_energy_entity:
        HSEM_LOGGER.warning(
            "ML populator: grid_import_energy_entity not configured. "
            "Configure it in the daily-tracking step. "
            "Falling back to legacy avg sensors."
        )
        return False, None

    slot_minutes = cfg.recommendation_interval_minutes
    slots_per_day = 24 * 60 // slot_minutes
    min_days = cfg.ml_consumption_history_days
    decay_days = float(min_days) / 2.0

    reader = HistoryReader(hass)

    # Full history fetch — cached for 60 minutes to avoid hammering the
    # recorder database on every 1–5 minute coordinator cycle.
    global _last_history_fetch, _cached_history
    now_ts = datetime.now().astimezone()
    cache_valid = (
        _last_history_fetch is not None
        and _cached_history is not None
        and (now_ts - _last_history_fetch) < _MIN_HISTORY_REFRESH
    )

    if cache_valid:
        import_history = _cached_history
        HSEM_LOGGER.debug(
            "ML populator: using cached history (%d samples, age %.0f min).",
            len(import_history),
            (now_ts - _last_history_fetch).total_seconds() / 60,
        )
    else:
        import_history = await reader.read_energy_history(
            entity_id=cfg.grid_import_energy_entity,
            days=min_days,
            slot_minutes=slot_minutes,
            max_days=min_days,
        )
        if import_history:
            _cached_history = import_history
            _last_history_fetch = now_ts
            HSEM_LOGGER.debug(
                "ML populator: fetched fresh history (%d samples).",
                len(import_history),
            )

    if not import_history:
        HSEM_LOGGER.info(
            "ML populator: insufficient import history for %s (need %d days). "
            "Falling back to legacy avg sensors.",
            cfg.grid_import_energy_entity,
            min_days,
        )
        return False, None

    # Build per-slot consumption history.
    # When using cached import, also skip the export fetch.
    if cfg.ml_consumption_net_consumption and cfg.grid_export_energy_entity:
        if cache_valid:
            # Use cached import directly — export fetch is also skipped.
            history = import_history
        else:
            export_history = await reader.read_energy_history(
                entity_id=cfg.grid_export_energy_entity,
                days=min_days,
                slot_minutes=slot_minutes,
                max_days=min_days,
            )
            if export_history:
                history = _compute_net_consumption(import_history, export_history)
            else:
                HSEM_LOGGER.info(
                    "ML populator: export history unavailable,"
                    " using import-only consumption."
                )
                history = import_history
    else:
        history = import_history

    if not history:
        HSEM_LOGGER.warning("ML populator: empty history after processing")
        return False, None

    # Create or reuse predictor.
    reference_time = datetime.now().astimezone()
    use_temp = bool(cfg.ml_consumption_temperature_entity)
    if predictor is None:
        predictor = ConsumptionPredictor(
            decay_days=decay_days,
            alpha=1.0,
            slots_per_day=slots_per_day,
            use_temperature=use_temp,
        )

    # Read temperature history if configured.
    # Expects an outdoor (ambient) temperature sensor in °C.
    # Indoor thermostats will NOT help predict weather-driven load.
    # Cached for 60 minutes (temperature changes slowly).
    temperatures: dict[datetime, float] | None = None
    if use_temp:
        global _last_temp_fetch, _cached_temps
        temp_cache_valid = (
            _last_temp_fetch is not None
            and _cached_temps is not None
            and (now_ts - _last_temp_fetch) < _MIN_HISTORY_REFRESH
        )
        if temp_cache_valid:
            temperatures = _cached_temps
        else:
            assert cfg.ml_consumption_temperature_entity is not None
            temperatures = await _read_temperature_history(
                reader, cfg.ml_consumption_temperature_entity, min_days
            )
            if temperatures:
                _cached_temps = temperatures
                _last_temp_fetch = now_ts

    # Train — retrain gate skips fitting when no new data has arrived.
    was_fitted_before = predictor.trained
    predictor.train(history, reference_time, temperatures)

    if predictor.trained and not was_fitted_before:
        HSEM_LOGGER.info(
            "ML populator: initial fit complete (%d samples, %d groups, decay=%.1fd).",
            predictor.last_fit_samples,
            predictor.group_count,
            decay_days,
        )
    elif predictor.trained:
        HSEM_LOGGER.debug(
            "ML populator: predictions from cached model "
            "(last fit: %s, %d samples, %d groups).",
            predictor.last_fit_time.isoformat() if predictor.last_fit_time else "?",
            predictor.last_fit_samples,
            predictor.group_count,
        )

    # Populate recommendations with safety buffer for uncertain slots.
    # The MILP receives `mean + safety_factor * std`, making the plan
    # robust against consumption exceeding the point prediction.
    #
    # For past slots (start < now), use today's actual consumption
    # from the energy sensor.  For future slots, use ML prediction.
    #
    # Set to 0.0 initially to isolate whether the cost increase is
    # from the safety buffer or from the raw predictions themselves.
    safety_factor = 0.0

    # Track stats for debug logging.
    total_mean = 0.0
    total_std = 0.0
    total_safe = 0.0

    # Read today's actual consumption for completed slots.
    today_actuals: dict[int, float] = {}
    if cfg.grid_import_energy_entity:
        today_actuals = await reader.read_today_actuals(
            entity_id=cfg.grid_import_energy_entity,
            slot_minutes=slot_minutes,
        )
        if cfg.ml_consumption_net_consumption and cfg.grid_export_energy_entity:
            export_actuals = await reader.read_today_actuals(
                entity_id=cfg.grid_export_energy_entity,
                slot_minutes=slot_minutes,
            )
            # Subtract export from import per slot.
            for slot_idx in list(today_actuals.keys()):
                today_actuals[slot_idx] = max(
                    today_actuals[slot_idx] - export_actuals.get(slot_idx, 0.0),
                    0.01,
                )

    actual_count = 0
    predicted_count = 0

    for rec in recommendations:
        rec_day_offset = (rec.start.date() - reference_time.date()).days
        slot_index = (rec.start.hour * 60 + rec.start.minute) // slot_minutes

        # Use actual consumption for past slots (day_offset == 0 and slot has ended).
        if rec_day_offset == 0 and slot_index in today_actuals:
            per_slot_kwh = round(today_actuals[slot_index], 4)
            actual_count += 1
        else:
            # Future slot: ML prediction with safety buffer.
            mean, std = predictor.predict_with_std(
                slot_index, rec_day_offset, reference_time
            )
            safe_kwh = mean + safety_factor * std
            total_mean += mean
            total_std += std if std > 0 else 0.0
            total_safe += safe_kwh
            per_slot_kwh = round(safe_kwh, 4)
            predicted_count += 1
        rec.avg_house_consumption_kwh = per_slot_kwh
        rec.avg_house_consumption_1d_kwh = per_slot_kwh
        rec.avg_house_consumption_3d_kwh = per_slot_kwh
        rec.avg_house_consumption_7d_kwh = per_slot_kwh
        rec.avg_house_consumption_14d_kwh = per_slot_kwh

    HSEM_LOGGER.info(
        "ML populator: populated %d slots (%d actuals, %d predicted,"
        " safety ×%.1f std).",
        len(recommendations),
        actual_count,
        predicted_count,
        safety_factor,
    )
    if predicted_count > 0:
        HSEM_LOGGER.info(
            "ML populator: future-slots total (mean=%.2f, std=%.2f,"
            " safe=%.2f kWh over %d slots).",
            total_mean,
            total_std,
            total_safe,
            predicted_count,
        )
    return True, predictor


def _compute_net_consumption(
    import_history: list[tuple[datetime, int, float]],
    export_history: list[tuple[datetime, int, float]],
) -> list[tuple[datetime, int, float]]:
    """Compute net consumption by subtracting export from import per slot."""
    export_map: dict[tuple[date, int], float] = {}
    for ts, slot, energy in export_history:
        key = (ts.date(), slot)
        export_map[key] = energy

    net: list[tuple[datetime, int, float]] = []
    for ts, slot, import_energy in import_history:
        key = (ts.date(), slot)
        export_energy = export_map.get(key, 0.0)
        net_energy = max(import_energy - export_energy, 0.01)
        net.append((ts, slot, round(net_energy, 4)))

    return net


async def _read_temperature_history(
    reader: HistoryReader,
    entity_id: str,
    days: int,
) -> dict[datetime, float]:
    """Read historical temperature values from the recorder.

    Returns a dict mapping timestamp → temperature (°C).
    """
    try:
        raw_states = await reader.read_instantaneous_history(
            entity_id=entity_id,
            days=days,
        )
    except Exception:
        HSEM_LOGGER.warning(
            "ML populator: failed to read temperature history for %s",
            entity_id,
        )
        return {}

    if not raw_states:
        return {}

    return dict(raw_states)

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
    # Resolve energy entity: dedicated ML entity first, fall back to grid import.
    energy_entity = cfg.ml_consumption_energy_entity or cfg.grid_import_energy_entity
    if not energy_entity:
        HSEM_LOGGER.warning(
            "ML populator: no energy entity configured. "
            "Set ml_consumption_energy_entity or grid_import_energy_entity. "
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
            entity_id=energy_entity,
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
            "ML populator: insufficient history for %s (need %d days). "
            "Falling back to legacy avg sensors.",
            energy_entity,
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
            use_sequential=cfg.ml_consumption_sequential,
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

    # Populate recommendations with adaptive safety buffer.
    # Each slot gets a buffer proportional to its uncertainty:
    #   σ/μ < 0.1  → 0.0   (trust the prediction)
    #   σ/μ < 0.3  → 0.5σ  (moderate buffer)
    #   σ/μ ≥ 0.3  → 1.0σ  (sparse or variable data)
    # Past slots use actual meter readings — zero uncertainty.
    #
    # Track stats for debug logging.
    total_mean = 0.0
    total_std = 0.0
    total_safe = 0.0
    buffer_0 = 0
    buffer_05 = 0
    buffer_1 = 0

    # Read today's actual consumption for completed slots.
    today_actuals: dict[int, float] = {}
    today_actuals = await reader.read_today_actuals(
        entity_id=energy_entity,
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

    # In sequential mode, precompute per-day predictions (each slot feeds
    # its output as lag input to the next).  Independent mode predicts
    # each slot separately.
    seq_predictions: dict[int, dict[int, float]] = {}
    if cfg.ml_consumption_sequential:
        seen_day_offsets: set[int] = set()
        for rec in recommendations:
            seen_day_offsets.add((rec.start.date() - reference_time.date()).days)
        for day_offset in sorted(seen_day_offsets):
            seq_predictions[day_offset] = predictor.predict_sequential(
                day_offset, reference_time
            )

    for rec in recommendations:
        rec_day_offset = (rec.start.date() - reference_time.date()).days
        slot_index = (rec.start.hour * 60 + rec.start.minute) // slot_minutes

        # Use actual consumption for past slots (day_offset == 0 and slot has ended).
        if rec_day_offset == 0 and slot_index in today_actuals:
            per_slot_kwh = round(today_actuals[slot_index], 4)
            actual_count += 1
        else:
            # Future slot: ML prediction.
            if seq_predictions:
                # Sequential mode: use precomputed chained prediction.
                day_preds = seq_predictions.get(rec_day_offset, {})
                mean = day_preds.get(slot_index, 0.0)
                # Safety buffer: use DOW-slot std from raw groups.
                std = 0.0
                if predictor.trained:
                    target_date = reference_time.date() + timedelta(days=rec_day_offset)
                    dow = target_date.weekday()
                    group = predictor._raw_groups.get((dow, slot_index), [])
                    if len(group) >= 2:
                        std = predictor._weighted_std(group)
                    elif mean > 0:
                        std = mean * 0.2
            else:
                # Independent mode: predict each slot separately.
                mean, std = predictor.predict_with_std(
                    slot_index, rec_day_offset, reference_time
                )
            rel_uncertainty = std / mean if mean > 0 else 0.0
            if rel_uncertainty < 0.1:
                safety_factor = 0.0
                buffer_0 += 1
            elif rel_uncertainty < 0.3:
                safety_factor = 0.5
                buffer_05 += 1
            else:
                safety_factor = 1.0
                buffer_1 += 1
            safe_kwh = mean + safety_factor * std
            total_mean += mean
            total_std += std
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
        " buffer ×0=%d, ×0.5=%d, ×1=%d).",
        len(recommendations),
        actual_count,
        predicted_count,
        buffer_0,
        buffer_05,
        buffer_1,
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

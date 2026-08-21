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

import math
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant

from custom_components.hsem.ml.consumption_predictor import ConsumptionPredictor
from custom_components.hsem.ml.history_reader import (
    DEFAULT_MAX_HISTORY_DAYS,
    HistoryReader,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.datetime_utils import (
    normalize_datetime,
    now as hsem_now,
    slot_key,
    utc_key,
)
from custom_components.hsem.utils.logger import HSEM_LOGGER

type _HistorySample = tuple[datetime, int, float]
type _HistoryCacheKey = tuple[int, str, str | None, bool, int, int]
type _TemperatureCacheKey = tuple[int, str, int]
type _PredictorContext = tuple[str, str | None, bool, int, int, str | None]

# Cache final, fully processed history rather than an import-only intermediate.
# The effective configuration is part of the key so another config entry,
# source entity, cadence, history window, or net/gross mode cannot reuse it.
_processed_history_cache: dict[
    _HistoryCacheKey, tuple[datetime, list[_HistorySample]]
] = {}
_temperature_history_cache: dict[
    _TemperatureCacheKey, tuple[datetime, dict[datetime, float]]
] = {}
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

    reader = HistoryReader(hass)

    # Full history fetch — cache the processed series for 60 minutes to avoid
    # hammering the recorder database on every 1–5 minute coordinator cycle.
    now_ts = hsem_now()
    net_enabled = cfg.ml_consumption_net_consumption
    if net_enabled and not cfg.grid_export_energy_entity:
        HSEM_LOGGER.warning(
            "ML populator: net consumption is enabled but no grid export "
            "energy entity is configured. Falling back to legacy avg sensors."
        )
        return False, None
    export_entity = cfg.grid_export_energy_entity if net_enabled else None
    cache_key: _HistoryCacheKey = (
        id(hass),
        energy_entity,
        export_entity,
        net_enabled,
        slot_minutes,
        min_days,
    )
    cached = _processed_history_cache.get(cache_key)
    cache_valid = cached is not None and _cache_is_fresh(cached[0], now_ts)

    history: list[_HistorySample]
    if cache_valid and cached is not None:
        history = cached[1]
        HSEM_LOGGER.debug(
            "ML populator: using cached history (%d samples, age %.0f min).",
            len(history),
            _physical_elapsed(now_ts, cached[0]).total_seconds() / 60,
        )
    else:
        import_history = await reader.read_energy_history(
            entity_id=energy_entity,
            days=min_days,
            slot_minutes=slot_minutes,
            max_days=DEFAULT_MAX_HISTORY_DAYS,
        )
        if not import_history:
            HSEM_LOGGER.info(
                "ML populator: insufficient history for %s (need %d days). "
                "Falling back to legacy avg sensors.",
                energy_entity,
                min_days,
            )
            return False, None

        history = import_history
        if net_enabled and export_entity is not None:
            export_history = await reader.read_energy_history(
                entity_id=export_entity,
                days=min_days,
                slot_minutes=slot_minutes,
                max_days=DEFAULT_MAX_HISTORY_DAYS,
            )
            if export_history:
                history = _compute_net_consumption(import_history, export_history)
                if not _history_meets_minimum_span(history, now_ts, min_days):
                    HSEM_LOGGER.info(
                        "ML populator: aligned import/export history is "
                        "insufficient for %d days. Falling back to legacy "
                        "avg sensors.",
                        min_days,
                    )
                    return False, None
            else:
                HSEM_LOGGER.info(
                    "ML populator: export history unavailable while net "
                    "consumption is enabled. Falling back to legacy avg sensors."
                )
                return False, None

        if history:
            _processed_history_cache[cache_key] = (now_ts, history)
        HSEM_LOGGER.debug(
            "ML populator: fetched fresh processed history (%d samples).",
            len(history),
        )

    if not history:
        HSEM_LOGGER.warning("ML populator: empty history after processing")
        return False, None

    # Create or reuse predictor.  Calendar calculations use HA local time;
    # physical sample ages remain correct because all timestamps are aware.
    reference_time = now_ts

    # Compute decay from the actual data span, not the configured window.
    # Half-life = actual_span / 2 gives the oldest data ~14% weight.
    oldest_age = max(
        _physical_elapsed(reference_time, ts).total_seconds() / 86400.0
        for ts, _slot, _energy in history
    )
    decay_days = max(oldest_age, 1.0) / 2.0

    # Read temperature history before creating the predictor.  If a configured
    # sensor has insufficient history, disable the feature safely rather than
    # fitting a temperature column and then omitting it during inference.
    temperatures: dict[datetime, float] | None = None
    temperature_entity = cfg.ml_consumption_temperature_entity
    if temperature_entity:
        temp_cache_key: _TemperatureCacheKey = (
            id(hass),
            temperature_entity,
            min_days,
        )
        cached_temps = _temperature_history_cache.get(temp_cache_key)
        temp_cache_valid = cached_temps is not None and _cache_is_fresh(
            cached_temps[0], now_ts
        )
        if temp_cache_valid and cached_temps is not None:
            temperatures = cached_temps[1]
        else:
            temperatures = await _read_temperature_history(
                reader, temperature_entity, min_days
            )
            if temperatures:
                _temperature_history_cache[temp_cache_key] = (
                    now_ts,
                    temperatures,
                )

    use_temp = bool(temperatures)
    if not use_temp:
        temperatures = None
    if temperature_entity and not use_temp:
        HSEM_LOGGER.info(
            "ML populator: temperature history unavailable;"
            " fitting without temperature."
        )

    training_context: _PredictorContext = (
        energy_entity,
        export_entity,
        net_enabled,
        slot_minutes,
        min_days,
        temperature_entity if use_temp else None,
    )
    if (
        predictor is None
        or predictor.slots_per_day != slots_per_day
        or predictor.use_temperature != use_temp
        or predictor.use_sequential != cfg.ml_consumption_sequential
        or predictor.training_context != training_context
    ):
        predictor = ConsumptionPredictor(
            decay_days=decay_days,
            alpha=1.0,
            slots_per_day=slots_per_day,
            use_temperature=use_temp,
            use_sequential=cfg.ml_consumption_sequential,
        )
    # A reused predictor must not retain the half-life from its initial fit.
    predictor.decay_days = decay_days
    predictor.training_context = training_context

    # Store the actual history span so it can be exposed to the user.
    predictor.actual_history_days = max(oldest_age, 0.0)

    # Train — retrain gate skips fitting when no new data has arrived.
    # The fit is CPU-bound (numpy ridge solve), so run it in HA's executor
    # pool to avoid blocking the event loop.
    was_fitted_before = predictor.trained
    await hass.async_add_executor_job(
        predictor.train, history, reference_time, temperatures
    )

    if not predictor.trained:
        HSEM_LOGGER.info(
            "ML populator: history did not produce a trained model;"
            " falling back to legacy avg sensors."
        )
        return False, None

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
    today_actuals: dict[datetime, float] = await reader.read_today_actuals(
        entity_id=energy_entity,
        slot_minutes=slot_minutes,
    )
    today_actuals = {
        key: value for key, value in today_actuals.items() if math.isfinite(value)
    }
    if cfg.ml_consumption_net_consumption and cfg.grid_export_energy_entity:
        export_actuals = await reader.read_today_actuals(
            entity_id=cfg.grid_export_energy_entity,
            slot_minutes=slot_minutes,
        )
        # A missing channel key is unknown, not zero.  Use only physical
        # slots observed in both meters so recorder gaps cannot silently
        # become import-only labels in a net-consumption model.
        aligned_actuals: dict[datetime, float] = {}
        for physical_key, import_energy in today_actuals.items():
            if physical_key not in export_actuals:
                continue
            export_energy = export_actuals[physical_key]
            if not math.isfinite(export_energy):
                continue
            aligned_actuals[physical_key] = max(
                import_energy - export_energy,
                0.01,
            )
        today_actuals = aligned_actuals

    actual_count = 0
    predicted_count = 0

    # Sequential mode follows the real recommendation instants in UTC order.
    # This naturally skips spring's nonexistent hour and preserves both
    # physical folds of autumn's repeated wall hour.
    seq_predictions: dict[datetime, float] = {}
    prediction_temperature = _nearest_temperature(temperatures, reference_time)
    if cfg.ml_consumption_sequential:
        sequence_keys = sorted(
            {
                slot_key(normalize_datetime(rec.start), slot_minutes)
                for rec in recommendations
            }
        )
        sequence_starts = [normalize_datetime(key) for key in sequence_keys]
        sequential_temperatures = (
            {key: prediction_temperature for key in sequence_keys}
            if prediction_temperature is not None
            else None
        )
        seq_predictions = predictor.predict_sequential(
            sequence_starts, sequential_temperatures
        )

    for rec in recommendations:
        rec_start = normalize_datetime(rec.start)
        rec_day_offset = (rec_start.date() - reference_time.date()).days
        slot_index = (rec_start.hour * 60 + rec_start.minute) // slot_minutes
        physical_key = slot_key(rec_start, slot_minutes)

        # Use actual consumption for past slots (day_offset == 0 and slot has ended).
        if rec_day_offset == 0 and physical_key in today_actuals:
            per_slot_kwh = round(today_actuals[physical_key], 4)
            actual_count += 1
        else:
            # Future slot: ML prediction.
            if seq_predictions:
                # Sequential mode: use precomputed chained prediction.
                mean = seq_predictions.get(physical_key, 0.0)
                # Safety buffer: use DOW-slot std from raw groups.
                std = 0.0
                if predictor.trained:
                    group = predictor._raw_groups.get(
                        (rec_start.weekday(), slot_index), []
                    )
                    if len(group) >= 2:
                        std = predictor._weighted_std(group)
                    elif mean > 0:
                        std = mean * 0.2
            else:
                # Independent mode: predict each slot separately.
                mean, std = predictor.predict_with_std(
                    slot_index,
                    rec_day_offset,
                    reference_time,
                    prediction_temperature,
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


def _physical_elapsed(later: datetime, earlier: datetime) -> timedelta:
    """Return elapsed time by UTC instant, retaining local calendar timestamps."""
    later_aware = later if later.tzinfo is not None else later.astimezone()
    earlier_aware = (
        earlier
        if earlier.tzinfo is not None
        else earlier.replace(tzinfo=later_aware.tzinfo)
    )
    return utc_key(later_aware) - utc_key(earlier_aware)


def _cache_is_fresh(cached_at: datetime, current: datetime) -> bool:
    """Return whether a cache timestamp is within the physical refresh window."""
    age = _physical_elapsed(current, cached_at)
    return timedelta(0) <= age < _MIN_HISTORY_REFRESH


def _history_meets_minimum_span(
    history: list[_HistorySample],
    reference_time: datetime,
    min_days: int,
) -> bool:
    """Return whether aligned history can train and spans the configured window."""
    if len(history) < 2:
        return False
    oldest_age_days = max(
        _physical_elapsed(reference_time, timestamp).total_seconds() / 86400.0
        for timestamp, _slot, _energy in history
    )
    return oldest_age_days >= min_days


def _compute_net_consumption(
    import_history: list[_HistorySample],
    export_history: list[_HistorySample],
) -> list[_HistorySample]:
    """Compute net consumption for physical slots present in both channels."""
    export_map = {
        utc_key(ts): energy
        for ts, _slot, energy in export_history
        if math.isfinite(energy)
    }

    net: list[_HistorySample] = []
    for ts, slot, import_energy in import_history:
        if not math.isfinite(import_energy):
            continue
        physical_key = utc_key(ts)
        if physical_key not in export_map:
            continue
        export_energy = export_map[physical_key]
        net_energy = max(import_energy - export_energy, 0.01)
        net.append((ts, slot, round(net_energy, 4)))

    return net


def _nearest_temperature(
    temperatures: dict[datetime, float] | None,
    target: datetime,
) -> float | None:
    """Return the temperature nearest to *target* by physical time.

    The configured entity provides history rather than a future weather
    forecast, so inference deliberately persists the newest nearby reading
    through the prediction horizon.
    """
    finite_temperatures = {
        timestamp: value
        for timestamp, value in (temperatures or {}).items()
        if math.isfinite(value)
    }
    if not finite_temperatures:
        return None
    target_key = utc_key(target)
    nearest = min(
        finite_temperatures,
        key=lambda timestamp: abs((utc_key(timestamp) - target_key).total_seconds()),
    )
    return finite_temperatures[nearest]


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

    # Canonical UTC keys preserve both folds of an autumn repeated hour;
    # local ZoneInfo datetimes with identical wall fields compare equal.
    return {
        utc_key(timestamp): value
        for timestamp, value in raw_states
        if math.isfinite(value)
    }

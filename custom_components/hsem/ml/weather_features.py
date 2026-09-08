"""Shared weather-derived feature helpers for the ML consumption predictor.

Provides cached recorder-history and forecast lookups for temperature and
wind speed, plus the generic interpolation/broadcast-fallback logic used to
resolve a value for a future prediction slot from either source. Both
features follow the exact same shape (history for training, optional
forecast for future slots, fallback to the nearest historical reading), so
the mechanics live here once rather than being duplicated per feature in
:mod:`ml.populator` — that module was already close to the repo's
30 KB/1000-line file-size limit before wind support was added (issue #943).
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from homeassistant.const import UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import SpeedConverter

from custom_components.hsem.ml.history_reader import HistoryReader
from custom_components.hsem.ml.weather_forecast_reader import (
    WeatherForecastPoints,
    read_weather_forecast,
)
from custom_components.hsem.utils.datetime_utils import cache_is_fresh, utc_key
from custom_components.hsem.utils.logger import HSEM_LOGGER

type _HistoryCacheKey = tuple[int, str, int]
type _ForecastCacheKey = tuple[int, str]

# Recorder history changes slowly — cache the processed series for 60 minutes
# to avoid hammering the database on every 1-5 minute coordinator cycle.
_temperature_history_cache: dict[
    _HistoryCacheKey, tuple[datetime, dict[datetime, float]]
] = {}
_wind_history_cache: dict[_HistoryCacheKey, tuple[datetime, dict[datetime, float]]] = {}
_MIN_HISTORY_REFRESH = timedelta(minutes=60)

# Forecast data changes more often than recorder history but a live
# ``weather.get_forecasts`` service call is cheap — cache briefly so a
# 1-5 minute coordinator cycle does not hammer the weather integration.
_forecast_cache: dict[_ForecastCacheKey, tuple[datetime, WeatherForecastPoints]] = {}
_MIN_FORECAST_REFRESH = timedelta(minutes=15)


async def get_temperature_history(
    hass: HomeAssistant,
    reader: HistoryReader,
    entity_id: str,
    min_days: int,
    now_ts: datetime,
) -> dict[datetime, float]:
    """Return cached or freshly read historical temperatures for *entity_id*.

    Normalizes each reading to Celsius via
    :func:`custom_components.hsem.utils.unit_normalize.normalize_to_unit` so a
    °F-reporting or unit-less template sensor cannot silently corrupt the
    temperature or wind-chill ML features (issue #945).
    """
    return await _get_cached_instantaneous_history(
        _temperature_history_cache,
        hass,
        now_ts,
        entity_id,
        min_days,
        lambda: reader.read_instantaneous_history(
            entity_id=entity_id,
            days=min_days,
            expected_unit=UnitOfTemperature.CELSIUS,
        ),
    )


async def get_wind_history(
    hass: HomeAssistant,
    reader: HistoryReader,
    entity_id: str,
    min_days: int,
    now_ts: datetime,
) -> dict[datetime, float]:
    """Return cached or freshly read historical wind speeds (km/h) for *entity_id*.

    Wind history is read from *entity_id*'s ``wind_speed`` attribute (a
    weather entity), not its raw state — see
    :meth:`HistoryReader.read_instantaneous_attribute_history`. The unit is
    resolved once from the entity's *current* ``wind_speed_unit`` attribute,
    the same simplification the forecast reader makes for a single call.
    """
    unit_state = hass.states.get(entity_id)
    wind_speed_unit = (
        unit_state.attributes.get("wind_speed_unit") if unit_state else None
    ) or hass.config.units.wind_speed_unit

    async def _read() -> list[tuple[datetime, float]]:
        raw = await reader.read_instantaneous_attribute_history(
            entity_id=entity_id, attribute="wind_speed", days=min_days
        )
        if wind_speed_unit == UnitOfSpeed.KILOMETERS_PER_HOUR:
            return raw
        return [
            (
                ts,
                SpeedConverter.convert(
                    value, wind_speed_unit, UnitOfSpeed.KILOMETERS_PER_HOUR
                ),
            )
            for ts, value in raw
        ]

    return await _get_cached_instantaneous_history(
        _wind_history_cache, hass, now_ts, entity_id, min_days, _read
    )


async def _get_cached_instantaneous_history(
    cache: dict[_HistoryCacheKey, tuple[datetime, dict[datetime, float]]],
    hass: HomeAssistant,
    now_ts: datetime,
    entity_id: str,
    min_days: int,
    reader_call: Callable[[], Awaitable[list[tuple[datetime, float]]]],
) -> dict[datetime, float]:
    cache_key: _HistoryCacheKey = (id(hass), entity_id, min_days)
    cached = cache.get(cache_key)
    if cached is not None and cache_is_fresh(cached[0], now_ts, _MIN_HISTORY_REFRESH):
        return cached[1]

    try:
        raw_states = await reader_call()
    except Exception:
        HSEM_LOGGER.warning(
            "ML populator: failed to read instantaneous history for %s", entity_id
        )
        return {}

    # Canonical UTC keys preserve both folds of an autumn repeated hour;
    # local ZoneInfo datetimes with identical wall fields compare equal.
    values = {
        utc_key(timestamp): value
        for timestamp, value in raw_states
        if math.isfinite(value)
    }
    if values:
        cache[cache_key] = (now_ts, values)
    return values


async def get_cached_weather_forecast(
    hass: HomeAssistant,
    entity_id: str,
    now_ts: datetime,
) -> WeatherForecastPoints | None:
    """Fetch forecast points for *entity_id*, cached briefly.

    Returns ``None`` when the entity is unavailable or has no usable
    forecast data — callers must treat that as "no forecast coverage" and
    fall back to the existing measured-value behaviour.
    """
    cache_key: _ForecastCacheKey = (id(hass), entity_id)
    cached = _forecast_cache.get(cache_key)
    if cached is not None and cache_is_fresh(cached[0], now_ts, _MIN_FORECAST_REFRESH):
        return cached[1]

    points = await read_weather_forecast(hass, entity_id)
    if points:
        _forecast_cache[cache_key] = (now_ts, points)
    return points


def interpolate_forecast_value(
    points: dict[datetime, float],
    target: datetime,
    max_gap: timedelta = timedelta(hours=3),
) -> float | None:
    """Linearly interpolate a forecast value at *target* physical time.

    Returns ``None`` when *target* falls outside the forecast's covered range
    (before the earliest or after the latest point — no extrapolation) or
    when the two bracketing points are farther apart than *max_gap*
    (sparse/stale forecast data). A genuine ``0.0`` forecast value is a valid
    result, never treated as missing.
    """
    if not points:
        return None

    ordered = sorted(points.items(), key=lambda item: utc_key(item[0]))
    keys_utc = [utc_key(timestamp) for timestamp, _value in ordered]
    target_utc = utc_key(target)

    for key_utc, (_timestamp, value) in zip(keys_utc, ordered, strict=True):
        if key_utc == target_utc:
            return value

    if target_utc < keys_utc[0] or target_utc > keys_utc[-1]:
        return None

    for index in range(len(ordered) - 1):
        lo_utc, hi_utc = keys_utc[index], keys_utc[index + 1]
        if lo_utc < target_utc < hi_utc:
            gap = hi_utc - lo_utc
            if gap > max_gap:
                return None
            fraction = (target_utc - lo_utc) / gap
            lo_value = ordered[index][1]
            hi_value = ordered[index + 1][1]
            return lo_value + (hi_value - lo_value) * fraction

    return None


def nearest_value(
    history: dict[datetime, float] | None,
    target: datetime,
) -> float | None:
    """Return the historical value nearest to *target* by physical time.

    Used as the broadcast fallback for a future slot when no forecast
    covers it: the configured entity provides history rather than a future
    weather forecast, so inference deliberately persists the newest nearby
    reading through the prediction horizon.
    """
    finite_values = {
        timestamp: value
        for timestamp, value in (history or {}).items()
        if math.isfinite(value)
    }
    if not finite_values:
        return None
    target_key = utc_key(target)
    nearest = min(
        finite_values,
        key=lambda timestamp: abs((utc_key(timestamp) - target_key).total_seconds()),
    )
    return finite_values[nearest]


def resolve_future_value(
    forecast_points: dict[datetime, float] | None,
    broadcast_value: float | None,
    target: datetime,
) -> tuple[float | None, bool]:
    """Return ``(value, used_forecast)`` for a future slot.

    Prefers a per-slot forecast value (interpolated to *target*'s exact
    start time); falls back to the broadcast (nearest-to-now) value when the
    forecast doesn't cover *target* or no forecast is available at all.
    """
    if forecast_points:
        forecast_value = interpolate_forecast_value(forecast_points, target)
        if forecast_value is not None:
            return forecast_value, True
    return broadcast_value, False

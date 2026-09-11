"""Weather forecast reader — pulls per-point forecast data from a HA weather entity.

Unlike :mod:`custom_components.hsem.ml.history_reader`, this does not query the
recorder — it calls the live ``weather.get_forecasts`` service, which returns
the weather entity's current forecast (hourly, or daily as a fallback for
integrations that do not support hourly forecasts).

Returns raw ``{timestamp: value}`` points per feature; alignment/interpolation
to HSEM's planning slots happens in :mod:`ml.weather_features`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import SpeedConverter, TemperatureConverter

from custom_components.hsem.utils.datetime_utils import normalize_datetime
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

# Forecast types to try, in order.  Most weather integrations support hourly;
# a handful (e.g. some public-data integrations) only support daily.
_FORECAST_TYPES = ("hourly", "daily")


@dataclass(frozen=True)
class WeatherForecastPoints:
    """Per-point forecast values extracted from one ``weather.get_forecasts`` call.

    Both maps are keyed by forecast timestamp.  A point missing one field
    (e.g. a daily forecast without wind data) does not exclude it from the
    other map — the two are independent.
    """

    temperatures: dict[datetime, float] = field(default_factory=dict)
    wind_speeds_kmh: dict[datetime, float] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.temperatures) or bool(self.wind_speeds_kmh)


async def read_weather_forecast(
    hass: HomeAssistant,
    entity_id: str,
) -> WeatherForecastPoints | None:
    """Fetch forecast temperature and wind-speed points from a HA weather entity.

    Args:
        hass: The Home Assistant instance.
        entity_id: The weather entity ID (e.g. ``weather.home``).

    Returns:
        A :class:`WeatherForecastPoints` with whatever fields the forecast
        actually provided, or ``None`` when the entity is unavailable, the
        service call fails, or no usable forecast data was returned at all.
        A genuine ``0.0`` forecast point (temperature or wind speed) is a
        valid entry, never treated as missing.
    """
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        _LOGGER.info(
            "ML populator: weather forecast entity %s is unavailable.",
            entity_id,
        )
        return None

    forecast_entries: list[dict[str, Any]] | None = None
    for forecast_type in _FORECAST_TYPES:
        try:
            response = await hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": forecast_type},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError:
            continue

        if not isinstance(response, dict):
            continue
        entity_response = response.get(entity_id)
        if not isinstance(entity_response, dict):
            continue
        entries = entity_response.get("forecast")
        if isinstance(entries, list) and entries:
            forecast_entries = cast(list[dict[str, Any]], entries)
            break

    if not forecast_entries:
        _LOGGER.info(
            "ML populator: no hourly or daily forecast data available from %s.",
            entity_id,
        )
        return None

    temperature_unit = state.attributes.get("temperature_unit") or (
        hass.config.units.temperature_unit
    )
    wind_speed_unit = state.attributes.get("wind_speed_unit") or (
        hass.config.units.wind_speed_unit
    )

    temperatures: dict[datetime, float] = {}
    wind_speeds_kmh: dict[datetime, float] = {}
    for entry in forecast_entries:
        raw_time = entry.get("datetime")
        timestamp = (
            dt_util.parse_datetime(raw_time)
            if isinstance(raw_time, str)
            else raw_time
            if isinstance(raw_time, datetime)
            else None
        )
        if timestamp is None:
            continue
        timestamp = normalize_datetime(timestamp)

        raw_temp = entry.get("temperature")
        if raw_temp is not None:
            try:
                temperature_c = TemperatureConverter.convert(
                    float(raw_temp), temperature_unit, UnitOfTemperature.CELSIUS
                )
            except TypeError, ValueError:
                temperature_c = math.nan
            if math.isfinite(temperature_c):
                temperatures[timestamp] = temperature_c

        raw_wind = entry.get("wind_speed")
        if raw_wind is not None:
            try:
                wind_kmh = SpeedConverter.convert(
                    float(raw_wind), wind_speed_unit, UnitOfSpeed.KILOMETERS_PER_HOUR
                )
            except TypeError, ValueError:
                wind_kmh = math.nan
            if math.isfinite(wind_kmh):
                wind_speeds_kmh[timestamp] = wind_kmh

    if not temperatures and not wind_speeds_kmh:
        _LOGGER.info(
            "ML populator: forecast data from %s had no usable temperature or"
            " wind-speed points.",
            entity_id,
        )
        return None

    return WeatherForecastPoints(
        temperatures=temperatures, wind_speeds_kmh=wind_speeds_kmh
    )

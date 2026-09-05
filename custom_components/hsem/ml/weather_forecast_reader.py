"""Weather forecast reader — pulls per-point forecast temperatures from a HA weather entity.

Unlike :mod:`custom_components.hsem.ml.history_reader`, this does not query the
recorder — it calls the live ``weather.get_forecasts`` service, which returns
the weather entity's current forecast (hourly, or daily as a fallback for
integrations that do not support hourly forecasts).

Returns raw ``{timestamp: temperature_celsius}`` points; alignment/
interpolation to HSEM's planning slots happens in :mod:`ml.populator`.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, cast

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from custom_components.hsem.utils.datetime_utils import normalize_datetime
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

# Forecast types to try, in order.  Most weather integrations support hourly;
# a handful (e.g. some public-data integrations) only support daily.
_FORECAST_TYPES = ("hourly", "daily")


async def read_weather_forecast_temperatures(
    hass: HomeAssistant,
    entity_id: str,
) -> dict[datetime, float] | None:
    """Fetch forecast temperature points from a HA weather entity.

    Args:
        hass: The Home Assistant instance.
        entity_id: The weather entity ID (e.g. ``weather.home``).

    Returns:
        A dict mapping forecast timestamp -> temperature in Celsius, or
        ``None`` when the entity is unavailable, the service call fails, or
        no usable forecast data was returned.  A genuine ``0.0 °C`` forecast
        point is a valid entry, never treated as missing.
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

    source_unit = state.attributes.get("temperature_unit") or (
        hass.config.units.temperature_unit
    )

    points: dict[datetime, float] = {}
    for entry in forecast_entries:
        raw_time = entry.get("datetime")
        raw_temp = entry.get("temperature")
        if raw_time is None or raw_temp is None:
            continue

        timestamp = (
            dt_util.parse_datetime(raw_time)
            if isinstance(raw_time, str)
            else raw_time
            if isinstance(raw_time, datetime)
            else None
        )
        if timestamp is None:
            continue

        try:
            temperature_native = float(raw_temp)
        except TypeError, ValueError:
            continue

        try:
            temperature_c = TemperatureConverter.convert(
                temperature_native, source_unit, UnitOfTemperature.CELSIUS
            )
        except TypeError, ValueError:
            continue

        if not math.isfinite(temperature_c):
            continue

        points[normalize_datetime(timestamp)] = temperature_c

    if not points:
        _LOGGER.info(
            "ML populator: forecast data from %s had no usable temperature points.",
            entity_id,
        )
        return None

    return points

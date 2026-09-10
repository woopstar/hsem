"""Tests for optional wind-chill support in the ML populator (issue #943).

Wind history and wind forecast are both derived from the SAME weather
entity already used for temperature forecasting -- no dedicated wind
sensor is configured anywhere in these tests. Mirrors the structure of
test_populator_forecast_temperature.py (issue #918): per-slot forecast
interpolation, fallback to the nearest measured wind reading, and that
wind chill stays inactive whenever one of its dependencies (the enabled
flag, an active temperature feature, the weather entity, or wind history
itself) is missing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.hsem.ml import populator, weather_features
from custom_components.hsem.ml.weather_forecast_reader import WeatherForecastPoints
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.sensor_config import SensorConfig
from tests.ml.test_populator_time_alignment import (
    NOW,
    _cfg,
    _FakeHass,
    _FakePredictor,
    _FakeReader,
    _ha_local_timezone,  # noqa: F401 -- autouse fixture, activated by import
    _history,
    _recommendation,
)

__all__ = ["_ha_local_timezone"]

WEATHER_ENTITY = "weather.home"


@pytest.fixture(autouse=True)
def _clear_ml_caches():
    populator._processed_history_cache.clear()
    weather_features._temperature_history_cache.clear()
    weather_features._wind_history_cache.clear()
    weather_features._forecast_cache.clear()
    yield
    populator._processed_history_cache.clear()
    weather_features._temperature_history_cache.clear()
    weather_features._wind_history_cache.clear()
    weather_features._forecast_cache.clear()


def _measured_history(
    reading_time: datetime, value: float
) -> list[tuple[datetime, float]]:
    return [
        (NOW - timedelta(days=15), value),
        (reading_time, value),
    ]


def _wind_cfg(
    *,
    wind_chill_enabled: bool = True,
    weather_entity: str | None = WEATHER_ENTITY,
    reference_temperature: float = 18.0,
    **cfg_kwargs: Any,
) -> SensorConfig:
    cfg = _cfg(**cfg_kwargs)
    cfg.ml_consumption_weather_forecast_entity = weather_entity
    cfg.ml_consumption_wind_chill_enabled = wind_chill_enabled
    cfg.ml_consumption_wind_chill_reference_temperature = reference_temperature
    return cfg


def _fake_weather_hass(wind_speed_unit: str = "km/h") -> _FakeHass:
    return _FakeHass(
        states={
            WEATHER_ENTITY: SimpleNamespace(
                attributes={"wind_speed_unit": wind_speed_unit}
            )
        }
    )


async def _populate_with_wind(
    reader: _FakeReader,
    cfg: SensorConfig,
    recommendations: list[HourlyRecommendation],
    *,
    forecast_temperatures: dict[datetime, float] | None = None,
    forecast_wind: dict[datetime, float] | None = None,
    now: datetime = NOW,
    hass: _FakeHass | None = None,
) -> tuple[tuple[bool, _FakePredictor | None], AsyncMock]:
    forecast_result = (
        WeatherForecastPoints(
            temperatures=forecast_temperatures or {},
            wind_speeds_kmh=forecast_wind or {},
        )
        if forecast_temperatures is not None or forecast_wind is not None
        else None
    )
    forecast_mock = AsyncMock(return_value=forecast_result)
    with (
        patch.object(populator, "HistoryReader", return_value=reader),
        patch.object(populator, "ConsumptionPredictor", _FakePredictor),
        patch.object(populator, "hsem_now", return_value=now),
        patch.object(weather_features, "read_weather_forecast", forecast_mock),
    ):
        result = await populator.populate_ml_house_consumption(
            cast(HomeAssistant, hass or _fake_weather_hass()),
            recommendations,
            cfg,
            None,
        )
    return cast(tuple[bool, _FakePredictor | None], result), forecast_mock


@pytest.mark.asyncio
async def test_wind_history_and_forecast_derive_from_temperature_weather_entity() -> (
    None
):
    """No dedicated wind entity is configured anywhere -- both history and
    forecast come from ml_consumption_weather_forecast_entity."""
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={
            "sensor.temperature": _measured_history(NOW - timedelta(minutes=5), 2.0)
        },
        wind_speeds={
            WEATHER_ENTITY: _measured_history(NOW - timedelta(minutes=5), 15.0)
        },
    )
    cfg = _wind_cfg(temperature_entity="sensor.temperature")
    recommendation = _recommendation(NOW + timedelta(minutes=15))

    (success, predictor), forecast_mock = await _populate_with_wind(
        reader, cfg, [recommendation]
    )

    assert success is True
    assert predictor is not None
    assert predictor.use_wind_chill is True
    assert reader.wind_calls == [(WEATHER_ENTITY, "wind_speed")]
    forecast_mock.assert_awaited_once()
    assert forecast_mock.await_args is not None
    assert forecast_mock.await_args.args[1] == WEATHER_ENTITY


@pytest.mark.asyncio
async def test_wind_forecast_interpolates_across_15_minute_slot() -> None:
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={
            "sensor.temperature": _measured_history(NOW - timedelta(minutes=5), 2.0)
        },
        wind_speeds={
            WEATHER_ENTITY: _measured_history(NOW - timedelta(minutes=5), 5.0)
        },
    )
    cfg = _wind_cfg(temperature_entity="sensor.temperature")
    recommendation = _recommendation(NOW + timedelta(minutes=15))

    forecast_wind = {
        NOW: 10.0,
        NOW + timedelta(hours=1): 30.0,
    }

    (success, predictor), _mock = await _populate_with_wind(
        reader,
        cfg,
        [recommendation],
        forecast_temperatures={NOW: 2.0, NOW + timedelta(hours=1): 2.0},
        forecast_wind=forecast_wind,
    )

    assert success is True
    assert predictor is not None
    # Linear interpolation 15 min into a 60-minute bracket: 10 + (30-10)*0.25
    assert predictor.prediction_wind_speeds == [pytest.approx(15.0)]
    assert predictor.forecast_wind_slots_used == 1
    assert predictor.fallback_wind_slots_used == 0


@pytest.mark.asyncio
async def test_wind_falls_back_to_measured_reading_outside_forecast_coverage() -> None:
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={
            "sensor.temperature": _measured_history(NOW - timedelta(minutes=1), 4.0)
        },
        wind_speeds={
            WEATHER_ENTITY: _measured_history(NOW - timedelta(minutes=1), 7.5)
        },
    )
    cfg = _wind_cfg(temperature_entity="sensor.temperature")
    covered_slot = NOW + timedelta(minutes=15)
    uncovered_slot = NOW + timedelta(hours=5)
    recommendations = [_recommendation(covered_slot), _recommendation(uncovered_slot)]

    forecast_wind = {
        NOW: 10.0,
        NOW + timedelta(minutes=30): 12.0,
    }

    (success, predictor), _mock = await _populate_with_wind(
        reader,
        cfg,
        recommendations,
        forecast_temperatures={NOW: 4.0, NOW + timedelta(minutes=30): 4.0},
        forecast_wind=forecast_wind,
    )

    assert success is True
    assert predictor is not None
    assert predictor.prediction_wind_speeds[0] == pytest.approx(11.0)
    # Uncovered slot falls back to the nearest measured reading (7.5).
    assert predictor.prediction_wind_speeds[1] == pytest.approx(7.5)
    assert predictor.forecast_wind_slots_used == 1
    assert predictor.fallback_wind_slots_used == 1


@pytest.mark.asyncio
async def test_wind_chill_inactive_when_flag_disabled() -> None:
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={
            "sensor.temperature": _measured_history(NOW - timedelta(minutes=1), 4.0)
        },
        wind_speeds={WEATHER_ENTITY: _measured_history(NOW, 20.0)},
    )
    cfg = _wind_cfg(temperature_entity="sensor.temperature", wind_chill_enabled=False)
    recommendation = _recommendation(NOW + timedelta(minutes=15))

    (success, predictor), _mock = await _populate_with_wind(
        reader, cfg, [recommendation]
    )

    assert success is True
    assert predictor is not None
    assert predictor.use_wind_chill is False
    assert reader.wind_calls == []


@pytest.mark.asyncio
async def test_wind_chill_inactive_when_temperature_feature_inactive() -> None:
    """Wind chill requires the temperature feature -- no measured-temperature
    entity/history means there's no temperature to feed the chill index."""
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        wind_speeds={WEATHER_ENTITY: _measured_history(NOW, 20.0)},
    )
    cfg = _wind_cfg()
    recommendation = _recommendation(NOW + timedelta(minutes=15))

    (success, predictor), _mock = await _populate_with_wind(
        reader, cfg, [recommendation]
    )

    assert success is True
    assert predictor is not None
    assert predictor.use_temperature is False
    assert predictor.use_wind_chill is False
    assert reader.wind_calls == []


@pytest.mark.asyncio
async def test_wind_chill_inactive_without_weather_entity_configured() -> None:
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={
            "sensor.temperature": _measured_history(NOW - timedelta(minutes=1), 4.0)
        },
    )
    cfg = _wind_cfg(temperature_entity="sensor.temperature", weather_entity=None)
    recommendation = _recommendation(NOW + timedelta(minutes=15))

    (success, predictor), _mock = await _populate_with_wind(
        reader, cfg, [recommendation]
    )

    assert success is True
    assert predictor is not None
    assert predictor.use_wind_chill is False
    assert reader.wind_calls == []


@pytest.mark.asyncio
async def test_wind_history_unavailable_disables_feature_safely() -> None:
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={
            "sensor.temperature": _measured_history(NOW - timedelta(minutes=1), 4.0)
        },
        # No wind_speeds entry for WEATHER_ENTITY -- history read returns [].
    )
    cfg = _wind_cfg(temperature_entity="sensor.temperature")
    recommendation = _recommendation(NOW + timedelta(minutes=15))

    (success, predictor), _mock = await _populate_with_wind(
        reader, cfg, [recommendation]
    )

    assert success is True
    assert predictor is not None
    assert predictor.use_wind_chill is False
    assert reader.wind_calls == [(WEATHER_ENTITY, "wind_speed")]


@pytest.mark.asyncio
async def test_sequential_mode_uses_per_slot_forecast_wind() -> None:
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={
            "sensor.temperature": _measured_history(NOW - timedelta(minutes=1), 1.0)
        },
        wind_speeds={
            WEATHER_ENTITY: _measured_history(NOW - timedelta(minutes=1), 5.0)
        },
    )
    cfg = _wind_cfg(temperature_entity="sensor.temperature", sequential=True)
    first_slot = NOW
    second_slot = NOW + timedelta(minutes=15)
    recommendations = [_recommendation(first_slot), _recommendation(second_slot)]

    (success, predictor), _mock = await _populate_with_wind(
        reader,
        cfg,
        recommendations,
        forecast_temperatures={first_slot: 1.0, second_slot: 1.0},
        forecast_wind={first_slot: 20.0, second_slot: 25.0},
    )

    assert success is True
    assert predictor is not None
    sequential_wind = predictor.sequential_wind_requests[-1]
    assert sequential_wind is not None
    values = sorted(sequential_wind.values())
    assert values == [pytest.approx(20.0), pytest.approx(25.0)]
    assert predictor.forecast_wind_slots_used == 2
    assert predictor.fallback_wind_slots_used == 0

"""Tests for optional forecast-temperature support in the ML populator (issue #918).

Covers per-slot forecast alignment/interpolation (including 15-minute planning
intervals), per-slot fallback to the existing measured-temperature behaviour
when forecast coverage is missing/stale, the 0 degC valid-value edge case, and
that installations without the forecast entity configured see zero change.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.hsem.ml import populator
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


@pytest.fixture(autouse=True)
def _clear_forecast_cache():
    populator._processed_history_cache.clear()
    populator._temperature_history_cache.clear()
    populator._forecast_cache.clear()
    yield
    populator._processed_history_cache.clear()
    populator._temperature_history_cache.clear()
    populator._forecast_cache.clear()


async def _populate_with_forecast(
    reader: _FakeReader,
    cfg: SensorConfig,
    recommendations: list[HourlyRecommendation],
    *,
    forecast_points: dict[datetime, float] | None,
    now: datetime = NOW,
) -> tuple[tuple[bool, _FakePredictor | None], AsyncMock]:
    forecast_mock = AsyncMock(return_value=forecast_points)
    with (
        patch.object(populator, "HistoryReader", return_value=reader),
        patch.object(populator, "ConsumptionPredictor", _FakePredictor),
        patch.object(populator, "hsem_now", return_value=now),
        patch.object(populator, "read_weather_forecast_temperatures", forecast_mock),
    ):
        result = await populator.populate_ml_house_consumption(
            cast(HomeAssistant, _FakeHass()),
            recommendations,
            cfg,
            None,
        )
    return cast(tuple[bool, _FakePredictor | None], result), forecast_mock


def _measured_temperature_history(
    reading_time: datetime, value: float
) -> list[tuple[datetime, float]]:
    return [
        (NOW - timedelta(days=15), value),
        (reading_time, value),
    ]


@pytest.mark.asyncio
async def test_forecast_interpolates_across_15_minute_slot() -> None:
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={
            "sensor.temperature": _measured_temperature_history(
                NOW - timedelta(minutes=5), 2.0
            )
        },
    )
    cfg = _cfg(temperature_entity="sensor.temperature")
    cfg.ml_consumption_weather_forecast_entity = "weather.home"
    recommendation = _recommendation(NOW + timedelta(minutes=15))

    forecast_points = {
        NOW: 10.0,
        NOW + timedelta(hours=1): 14.0,
    }

    (success, predictor), forecast_mock = await _populate_with_forecast(
        reader, cfg, [recommendation], forecast_points=forecast_points
    )

    assert success is True
    assert predictor is not None
    # Linear interpolation 15 min into a 60-minute bracket: 10 + (14-10)*0.25
    assert predictor.prediction_temperatures == [pytest.approx(11.0)]
    assert predictor.forecast_temperature_slots_used == 1
    assert predictor.fallback_temperature_slots_used == 0
    assert predictor.forecast_temperature_entity_configured is True
    forecast_mock.assert_awaited_once()
    assert forecast_mock.await_args is not None
    assert forecast_mock.await_args.args[1] == "weather.home"


@pytest.mark.asyncio
async def test_forecast_zero_celsius_is_treated_as_valid() -> None:
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={
            "sensor.temperature": _measured_temperature_history(
                NOW - timedelta(minutes=5), 9.9
            )
        },
    )
    cfg = _cfg(temperature_entity="sensor.temperature")
    cfg.ml_consumption_weather_forecast_entity = "weather.home"
    slot_start = NOW + timedelta(minutes=15)
    recommendation = _recommendation(slot_start)

    # Forecast point exactly at the slot's start, value is a genuine 0.0 C.
    forecast_points = {slot_start: 0.0}

    (success, predictor), _mock = await _populate_with_forecast(
        reader, cfg, [recommendation], forecast_points=forecast_points
    )

    assert success is True
    assert predictor is not None
    assert predictor.prediction_temperatures == [0.0]
    assert predictor.forecast_temperature_slots_used == 1
    assert predictor.fallback_temperature_slots_used == 0


@pytest.mark.asyncio
async def test_slot_outside_forecast_coverage_falls_back_to_measured() -> None:
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={
            "sensor.temperature": _measured_temperature_history(
                NOW - timedelta(minutes=1), 7.5
            )
        },
    )
    cfg = _cfg(temperature_entity="sensor.temperature")
    cfg.ml_consumption_weather_forecast_entity = "weather.home"
    covered_slot = NOW + timedelta(minutes=15)
    uncovered_slot = NOW + timedelta(hours=5)
    recommendations = [_recommendation(covered_slot), _recommendation(uncovered_slot)]

    # Forecast horizon only reaches 30 minutes out -- the second slot (5h
    # out) has no coverage and must fall back to the measured reading.
    forecast_points = {
        NOW: 10.0,
        NOW + timedelta(minutes=30): 11.0,
    }

    (success, predictor), _mock = await _populate_with_forecast(
        reader, cfg, recommendations, forecast_points=forecast_points
    )

    assert success is True
    assert predictor is not None
    # First call: interpolated forecast. Second call: fallback to the
    # nearest-to-"now" measured reading (7.5), matching pre-#918 behaviour.
    assert predictor.prediction_temperatures[0] == pytest.approx(10.5)
    assert predictor.prediction_temperatures[1] == pytest.approx(7.5)
    assert predictor.forecast_temperature_slots_used == 1
    assert predictor.fallback_temperature_slots_used == 1


@pytest.mark.asyncio
async def test_missing_forecast_data_falls_back_for_every_slot() -> None:
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={
            "sensor.temperature": _measured_temperature_history(
                NOW - timedelta(minutes=1), 3.3
            )
        },
    )
    cfg = _cfg(temperature_entity="sensor.temperature")
    cfg.ml_consumption_weather_forecast_entity = "weather.home"
    recommendation = _recommendation(NOW + timedelta(minutes=15))

    (success, predictor), forecast_mock = await _populate_with_forecast(
        reader, cfg, [recommendation], forecast_points=None
    )

    assert success is True
    assert predictor is not None
    assert predictor.prediction_temperatures == [pytest.approx(3.3)]
    assert predictor.forecast_temperature_slots_used == 0
    assert predictor.fallback_temperature_slots_used == 1
    forecast_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_forecast_entity_configured_is_unchanged_behaviour() -> None:
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={
            "sensor.temperature": _measured_temperature_history(
                NOW - timedelta(minutes=5), 11.5
            )
        },
    )
    cfg = _cfg(temperature_entity="sensor.temperature")
    assert cfg.ml_consumption_weather_forecast_entity is None
    recommendation = _recommendation(NOW + timedelta(minutes=15))

    (success, predictor), forecast_mock = await _populate_with_forecast(
        reader, cfg, [recommendation], forecast_points={NOW: 99.0}
    )

    assert success is True
    assert predictor is not None
    # Byte-for-byte pre-#918 behaviour: the constant nearest-to-now reading.
    assert predictor.prediction_temperatures == [pytest.approx(11.5)]
    assert predictor.forecast_temperature_slots_used == 0
    assert predictor.fallback_temperature_slots_used == 1
    assert predictor.forecast_temperature_entity_configured is False
    forecast_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_forecast_entity_without_measured_temperature_is_inactive() -> None:
    """Forecast temperature only matters when the model has a temperature
    coefficient -- that requires the measured-temperature entity to also be
    configured (and to have produced sufficient history)."""
    reader = _FakeReader({"sensor.import": _history(NOW)})
    cfg = _cfg()
    cfg.ml_consumption_weather_forecast_entity = "weather.home"
    recommendation = _recommendation(NOW + timedelta(minutes=15))

    (success, predictor), forecast_mock = await _populate_with_forecast(
        reader, cfg, [recommendation], forecast_points={NOW: 5.0}
    )

    assert success is True
    assert predictor is not None
    assert predictor.use_temperature is False
    assert predictor.forecast_temperature_slots_used == 0
    assert predictor.fallback_temperature_slots_used == 0
    assert predictor.forecast_temperature_entity_configured is True
    forecast_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_sequential_mode_uses_per_slot_forecast_temperatures() -> None:
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={
            "sensor.temperature": _measured_temperature_history(
                NOW - timedelta(minutes=1), 1.0
            )
        },
    )
    cfg = _cfg(temperature_entity="sensor.temperature", sequential=True)
    cfg.ml_consumption_weather_forecast_entity = "weather.home"
    first_slot = NOW
    second_slot = NOW + timedelta(minutes=15)
    recommendations = [_recommendation(first_slot), _recommendation(second_slot)]

    forecast_points = {first_slot: 20.0, second_slot: 22.0}

    (success, predictor), _mock = await _populate_with_forecast(
        reader, cfg, recommendations, forecast_points=forecast_points
    )

    assert success is True
    assert predictor is not None
    sequential_temperatures = predictor.sequential_temperature_requests[-1]
    assert sequential_temperatures is not None
    values = sorted(sequential_temperatures.values())
    assert values == [pytest.approx(20.0), pytest.approx(22.0)]
    assert predictor.forecast_temperature_slots_used == 2
    assert predictor.fallback_temperature_slots_used == 0

"""Tests for the weather forecast reader (issue #918)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.hsem.ml.weather_forecast_reader import (
    read_weather_forecast_temperatures,
)
from custom_components.hsem.utils.datetime_utils import utc_key

STOCKHOLM = ZoneInfo("Europe/Stockholm")


class _FakeState:
    def __init__(self, state: str, attributes: dict[str, Any] | None = None) -> None:
        self.state = state
        self.attributes = attributes or {}


class _FakeStates:
    def __init__(self, states: dict[str, _FakeState]) -> None:
        self._states = states

    def get(self, entity_id: str) -> _FakeState | None:
        return self._states.get(entity_id)


class _FakeServices:
    def __init__(
        self,
        *,
        responses: dict[str, dict[str, Any]] | None = None,
        raises: set[str] | None = None,
    ) -> None:
        # responses keyed by forecast "type" ("hourly"/"daily")
        self._responses = responses or {}
        self._raises = raises or set()
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def async_call(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        blocking: bool = False,
        return_response: bool = False,
    ) -> dict[str, Any] | None:
        del blocking, return_response
        self.calls.append((domain, service, dict(service_data)))
        forecast_type = service_data["type"]
        if forecast_type in self._raises:
            raise HomeAssistantError(f"unsupported: {forecast_type}")
        return self._responses.get(forecast_type)


class _FakeConfig:
    class _Units:
        temperature_unit = "°C"

    units = _Units()


class _FakeHass:
    def __init__(
        self,
        states: dict[str, _FakeState],
        *,
        responses: dict[str, dict[str, Any]] | None = None,
        raises: set[str] | None = None,
    ) -> None:
        self.states = _FakeStates(states)
        self.services = _FakeServices(responses=responses, raises=raises)
        self.config = _FakeConfig()


def _hourly_response(
    entity_id: str, entries: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    return {"hourly": {entity_id: {"forecast": entries}}}


async def _read(
    hass: _FakeHass, entity_id: str = "weather.home"
) -> dict[datetime, float] | None:
    return await read_weather_forecast_temperatures(
        cast(HomeAssistant, hass), entity_id
    )


@pytest.mark.asyncio
async def test_hourly_forecast_parsed_to_celsius_points() -> None:
    hass = _FakeHass(
        {"weather.home": _FakeState("sunny", {"temperature_unit": "°C"})},
        responses=_hourly_response(
            "weather.home",
            [
                {"datetime": "2026-08-20T12:00:00+00:00", "temperature": 5.0},
                {"datetime": "2026-08-20T13:00:00+00:00", "temperature": 6.5},
            ],
        ),
    )

    points = await _read(hass)

    assert points is not None
    assert len(points) == 2
    values = {utc_key(ts): value for ts, value in points.items()}
    assert values[datetime(2026, 8, 20, 12, 0, tzinfo=UTC)] == pytest.approx(5.0)
    assert values[datetime(2026, 8, 20, 13, 0, tzinfo=UTC)] == pytest.approx(6.5)


@pytest.mark.asyncio
async def test_zero_celsius_forecast_point_is_valid() -> None:
    hass = _FakeHass(
        {"weather.home": _FakeState("snowy", {"temperature_unit": "°C"})},
        responses=_hourly_response(
            "weather.home",
            [{"datetime": "2026-08-20T12:00:00+00:00", "temperature": 0.0}],
        ),
    )

    points = await _read(hass)

    assert points is not None
    assert len(points) == 1
    assert next(iter(points.values())) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_fahrenheit_unit_is_converted_to_celsius() -> None:
    hass = _FakeHass(
        {"weather.home": _FakeState("sunny", {"temperature_unit": "°F"})},
        responses=_hourly_response(
            "weather.home",
            [{"datetime": "2026-08-20T12:00:00+00:00", "temperature": 32.0}],
        ),
    )

    points = await _read(hass)

    assert points is not None
    assert next(iter(points.values())) == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_unavailable_entity_returns_none() -> None:
    hass = _FakeHass({"weather.home": _FakeState("unavailable")})

    points = await _read(hass)

    assert points is None
    assert hass.services.calls == []


@pytest.mark.asyncio
async def test_missing_entity_returns_none() -> None:
    hass = _FakeHass({})

    points = await _read(hass)

    assert points is None


@pytest.mark.asyncio
async def test_hourly_unsupported_falls_back_to_daily() -> None:
    hass = _FakeHass(
        {"weather.home": _FakeState("sunny", {"temperature_unit": "°C"})},
        responses={
            "daily": {
                "weather.home": {
                    "forecast": [
                        {"datetime": "2026-08-20T00:00:00+00:00", "temperature": 3.0}
                    ]
                }
            }
        },
        raises={"hourly"},
    )

    points = await _read(hass)

    assert points is not None
    assert next(iter(points.values())) == pytest.approx(3.0)
    assert [call[2]["type"] for call in hass.services.calls] == ["hourly", "daily"]


@pytest.mark.asyncio
async def test_no_forecast_data_returns_none() -> None:
    hass = _FakeHass(
        {"weather.home": _FakeState("sunny", {"temperature_unit": "°C"})},
        responses={"hourly": {"weather.home": {"forecast": []}}, "daily": {}},
    )

    points = await _read(hass)

    assert points is None


@pytest.mark.asyncio
async def test_entries_missing_temperature_or_time_are_skipped() -> None:
    hass = _FakeHass(
        {"weather.home": _FakeState("sunny", {"temperature_unit": "°C"})},
        responses=_hourly_response(
            "weather.home",
            [
                {"datetime": "2026-08-20T12:00:00+00:00", "temperature": None},
                {"datetime": None, "temperature": 5.0},
                {"datetime": "2026-08-20T14:00:00+00:00", "temperature": 4.0},
            ],
        ),
    )

    points = await _read(hass)

    assert points is not None
    assert len(points) == 1
    assert next(iter(points.values())) == pytest.approx(4.0)

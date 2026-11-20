"""Hourly long-term statistics reader (issue #1433)."""

from __future__ import annotations

from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from homeassistant.core import HomeAssistant

from custom_components.hsem.ml.slot_deltas import MAX_SLOT_KWH
from custom_components.hsem.ml.statistics_reader import (
    StatisticsReader,
    hourly_energy_to_slots,
)

STOCKHOLM = ZoneInfo("Europe/Stockholm")
ENTITY_ID = "sensor.house_energy"
START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)

_MODULE = "custom_components.hsem.ml.statistics_reader"


@pytest.fixture(autouse=True)
def long_term_statistics() -> None:
    """Use the real reader here instead of the empty one from ``conftest``."""


@pytest.fixture(autouse=True)
def _ha_local_timezone() -> Generator[None]:
    def as_stockholm(value: datetime) -> datetime:
        return value.astimezone(STOCKHOLM)

    with patch(
        "custom_components.hsem.utils.datetime_utils.dt_util.as_local",
        side_effect=as_stockholm,
    ):
        yield


class _Statistics:
    """A recorder that answers ``statistics_during_period`` from fixed rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[Any, ...]] = []
        self.executor_jobs = 0

    def query(self, _hass: Any, *args: Any) -> dict[str, list[dict[str, Any]]]:
        self.calls.append(args)
        start, end = args[0], args[1]
        inside = [
            row
            for row in self.rows
            if start.timestamp() <= row["start"] < end.timestamp()
        ]
        return {ENTITY_ID: inside} if inside else {}

    async def run(self, target: Callable[..., Any], *args: Any) -> Any:
        self.executor_jobs += 1
        return target(*args)


async def _read(
    statistics: _Statistics, method: str, *args: Any
) -> dict[datetime, float]:
    instance = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=statistics.run)
    )
    hass: HomeAssistant = SimpleNamespace()  # type: ignore[assignment]
    with (
        patch(f"{_MODULE}.get_instance", return_value=instance),
        patch(f"{_MODULE}.statistics_during_period", statistics.query),
    ):
        hours: dict[datetime, float] = await getattr(StatisticsReader(hass), method)(
            ENTITY_ID, *args
        )
    return hours


def _row(hour: int, **values: float | None) -> dict[str, Any]:
    return {"start": (START + hour * HOUR).timestamp(), **values}


@pytest.mark.asyncio
async def test_hourly_energy_is_the_difference_between_consecutive_sums() -> None:
    statistics = _Statistics(
        [_row(-1, sum=100.0), _row(0, sum=100.5), _row(1, sum=101.75)]
    )

    hours = await _read(
        statistics, "read_hourly_energy", START, START + 2 * HOUR, "kWh"
    )

    assert hours == {START: pytest.approx(0.5), START + HOUR: pytest.approx(1.25)}
    # The read starts one hour early to get the sum before the first hour,
    # asks for kWh and runs in the recorder executor.
    assert statistics.calls == [
        (
            START - HOUR,
            START + 2 * HOUR,
            {ENTITY_ID},
            "hour",
            {"energy": "kWh"},
            {"sum"},
        )
    ]
    assert statistics.executor_jobs == 1


@pytest.mark.asyncio
async def test_a_gap_in_the_statistics_is_not_turned_into_one_large_hour() -> None:
    """The hour after a gap has no sum before it and stays missing."""
    statistics = _Statistics(
        [
            _row(0, sum=100.0),
            _row(1, sum=101.0),
            _row(2, sum=None),
            _row(3, sum=float("nan")),
            # hours 4 and 5 have no row at all
            _row(6, sum=109.0),
            _row(7, sum=110.0),
        ]
    )

    hours = await _read(statistics, "read_hourly_energy", START, START + 8 * HOUR)

    assert hours == {
        START + HOUR: pytest.approx(1.0),
        START + 7 * HOUR: pytest.approx(1.0),
    }
    assert statistics.calls[0][4] is None  # no unit requested, none converted


@pytest.mark.asyncio
async def test_a_falling_sum_is_dropped_and_a_flat_one_is_kept() -> None:
    statistics = _Statistics(
        [_row(0, sum=100.0), _row(1, sum=100.0), _row(2, sum=90.0)]
    )

    hours = await _read(statistics, "read_hourly_energy", START, START + 3 * HOUR)

    assert hours == {START + HOUR: pytest.approx(0.0)}


@pytest.mark.asyncio
async def test_no_statistics_gives_no_hours() -> None:
    statistics = _Statistics([])

    assert await _read(statistics, "read_hourly_energy", START, START + HOUR) == {}
    assert await _read(statistics, "read_hourly_mean", START, START + HOUR) == {}


@pytest.mark.asyncio
async def test_hourly_mean_returns_the_hours_that_have_one() -> None:
    statistics = _Statistics(
        [_row(0, mean=400.0), _row(1, mean=None), _row(2, mean=650.0)]
    )

    hours = await _read(statistics, "read_hourly_mean", START, START + 3 * HOUR, "W")

    assert hours == {
        START: pytest.approx(400.0),
        START + 2 * HOUR: pytest.approx(650.0),
    }
    assert statistics.calls == [
        (START, START + 3 * HOUR, {ENTITY_ID}, "hour", {"power": "W"}, {"mean"})
    ]


@pytest.mark.asyncio
async def test_hourly_mean_without_a_unit_keeps_the_stored_unit() -> None:
    statistics = _Statistics([_row(0, mean=0.4)])

    hours = await _read(statistics, "read_hourly_mean", START, START + HOUR)

    assert hours == {START: pytest.approx(0.4)}
    assert statistics.calls[0][4] is None


def test_an_hour_is_spread_evenly_over_its_slots_in_local_time() -> None:
    # 00:00 UTC on 1 August is 02:00 in Stockholm: slots 8 to 11 of the day.
    samples = hourly_energy_to_slots({START: 1.0}, 15)

    assert [slot for _start, slot, _energy in samples] == [8, 9, 10, 11]
    assert [energy for _start, _slot, energy in samples] == [pytest.approx(0.25)] * 4
    assert samples[0][0] == START
    assert samples[0][0].utcoffset() == timedelta(hours=2)
    assert samples[3][0] == START + timedelta(minutes=45)


def test_hourly_slots_keep_the_whole_hour() -> None:
    samples = hourly_energy_to_slots({START + HOUR: 0.8, START: 0.4}, 60)

    assert [(slot, energy) for _start, slot, energy in samples] == [
        (2, pytest.approx(0.4)),
        (3, pytest.approx(0.8)),
    ]


def test_hours_the_raw_reader_would_reject_are_left_out() -> None:
    """Zero, non-finite and over-cap hours follow the raw-state rules."""
    samples = hourly_energy_to_slots(
        {
            START: 0.0,
            START + HOUR: float("inf"),
            START + 2 * HOUR: MAX_SLOT_KWH * 4 + 1.0,
            START + 3 * HOUR: 2.0,
        },
        15,
    )

    assert len(samples) == 4
    assert [energy for _start, _slot, energy in samples] == [pytest.approx(0.5)] * 4

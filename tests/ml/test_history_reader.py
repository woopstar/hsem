"""Regression tests for recorder-history time and physical-slot alignment."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.ml.history_reader import HistoryReader
from custom_components.hsem.utils.datetime_utils import utc_key

STOCKHOLM = ZoneInfo("Europe/Stockholm")
ENTITY_ID = "sensor.house_consumption_energy_total"


@pytest.fixture(autouse=True)
def _ha_local_timezone():
    """Make HA-local conversion deterministic without a running HA instance."""

    def as_stockholm(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=STOCKHOLM)
        return value.astimezone(STOCKHOLM)

    with patch(
        "custom_components.hsem.utils.datetime_utils.dt_util.as_local",
        side_effect=as_stockholm,
    ):
        yield


def _state(timestamp: datetime, value: float) -> SimpleNamespace:
    return SimpleNamespace(last_updated=timestamp, state=str(value))


def _deltas(
    readings: list[tuple[datetime, float]],
    now: datetime,
) -> list[tuple[datetime, int, float]]:
    return HistoryReader._compute_slot_deltas(readings, now, 15, 96)


@pytest.mark.parametrize(
    ("readings", "now"),
    [
        (
            [
                (datetime(2026, 8, 20, 7, 44, 50, tzinfo=UTC), 100.0),
                (datetime(2026, 8, 20, 7, 59, 50, tzinfo=UTC), 100.25),
            ],
            datetime(2026, 8, 20, 8, 15, tzinfo=UTC),
        ),
        (
            [
                (datetime(2026, 1, 20, 8, 44, 50, tzinfo=UTC), 100.0),
                (datetime(2026, 1, 20, 8, 59, 50, tzinfo=UTC), 100.25),
            ],
            datetime(2026, 1, 20, 9, 15, tzinfo=UTC),
        ),
    ],
)
def test_delta_is_attributed_to_current_ha_local_slot_in_cest_and_cet(
    readings: list[tuple[datetime, float]],
    now: datetime,
) -> None:
    """The same 09:45 local load must not move with the UTC offset."""
    history = _deltas(readings, now)

    assert len(history) == 1
    slot_start, slot_index, energy = history[0]
    assert (slot_start.hour, slot_start.minute) == (9, 45)
    assert slot_index == 39
    assert energy == pytest.approx(0.25)


def test_recorder_gap_is_not_converted_into_one_slot() -> None:
    readings = [
        (datetime(2026, 8, 20, 7, 44, 50, tzinfo=UTC), 100.0),
        # The 07:45 UTC physical slot has no readings.
        (datetime(2026, 8, 20, 8, 14, 50, tzinfo=UTC), 100.5),
    ]

    assert _deltas(readings, datetime(2026, 8, 20, 8, 30, tzinfo=UTC)) == []


def test_spring_forward_skips_nonexistent_wall_hour() -> None:
    readings = [
        (datetime(2026, 3, 29, 0, 44, 50, tzinfo=UTC), 100.0),
        (datetime(2026, 3, 29, 0, 59, 50, tzinfo=UTC), 100.1),
        (datetime(2026, 3, 29, 1, 14, 50, tzinfo=UTC), 100.2),
        (datetime(2026, 3, 29, 1, 29, 50, tzinfo=UTC), 100.3),
    ]

    history = _deltas(readings, datetime(2026, 3, 29, 1, 45, tzinfo=UTC))

    assert [(slot.hour, slot.minute, slot.fold) for slot, _idx, _energy in history] == [
        (1, 45, 0),
        (3, 0, 0),
        (3, 15, 0),
    ]
    assert [idx for _slot, idx, _energy in history] == [7, 12, 13]


def test_autumn_fallback_preserves_both_physical_folds() -> None:
    readings = [
        (datetime(2026, 10, 25, 0, 44, 50, tzinfo=UTC), 100.0),
        (datetime(2026, 10, 25, 0, 59, 50, tzinfo=UTC), 100.1),
        (datetime(2026, 10, 25, 1, 14, 50, tzinfo=UTC), 100.2),
        (datetime(2026, 10, 25, 1, 29, 50, tzinfo=UTC), 100.3),
    ]

    history = _deltas(readings, datetime(2026, 10, 25, 1, 45, tzinfo=UTC))
    starts = [slot for slot, _idx, _energy in history]

    assert [(slot.hour, slot.minute, slot.fold) for slot in starts] == [
        (2, 45, 0),
        (2, 0, 1),
        (2, 15, 1),
    ]
    assert [utc_key(slot) for slot in starts] == sorted(
        utc_key(slot) for slot in starts
    )


async def _read_actuals(
    *,
    now: datetime,
    states: list[SimpleNamespace],
) -> tuple[dict[datetime, float], AsyncMock]:
    executor = AsyncMock(return_value={ENTITY_ID: states})
    recorder = SimpleNamespace(async_add_executor_job=executor)
    with (
        patch(
            "custom_components.hsem.ml.history_reader.get_instance",
            return_value=recorder,
        ),
        patch(
            "custom_components.hsem.ml.history_reader.hsem_now",
            return_value=now,
        ),
    ):
        actuals = await HistoryReader(MagicMock()).read_today_actuals(ENTITY_ID)
    return actuals, executor


@pytest.mark.asyncio
async def test_today_actuals_use_local_midnight_and_exclude_current_or_prior_day() -> (
    None
):
    now = datetime(2026, 8, 20, 0, 40, tzinfo=STOCKHOLM)
    states = [
        _state(datetime(2026, 8, 19, 21, 44, 50, tzinfo=UTC), 99.9),
        _state(datetime(2026, 8, 19, 21, 59, 50, tzinfo=UTC), 100.0),
        _state(datetime(2026, 8, 19, 22, 14, 50, tzinfo=UTC), 100.2),
        _state(datetime(2026, 8, 19, 22, 29, 50, tzinfo=UTC), 100.5),
        # In-progress local 00:30 slot must be excluded.
        _state(datetime(2026, 8, 19, 22, 34, 50, tzinfo=UTC), 100.6),
    ]

    actuals, executor = await _read_actuals(now=now, states=states)

    assert actuals == {
        datetime(2026, 8, 19, 22, 0, tzinfo=UTC): pytest.approx(0.2),
        datetime(2026, 8, 19, 22, 15, tzinfo=UTC): pytest.approx(0.3),
    }
    assert executor.await_args is not None
    call_args = executor.await_args.args
    assert call_args[2] == datetime(2026, 8, 19, 21, 45, tzinfo=UTC)
    assert call_args[3] == datetime(2026, 8, 19, 22, 40, tzinfo=UTC)


@pytest.mark.asyncio
async def test_today_actuals_keep_repeated_hour_folds_as_distinct_keys() -> None:
    now = datetime(2026, 10, 25, 3, 0, tzinfo=STOCKHOLM)
    states = [
        _state(datetime(2026, 10, 25, 0, 44, 50, tzinfo=UTC), 100.0),
        _state(datetime(2026, 10, 25, 0, 59, 50, tzinfo=UTC), 100.1),
        _state(datetime(2026, 10, 25, 1, 14, 50, tzinfo=UTC), 100.2),
        _state(datetime(2026, 10, 25, 1, 29, 50, tzinfo=UTC), 100.3),
    ]

    actuals, _executor = await _read_actuals(now=now, states=states)

    assert datetime(2026, 10, 25, 0, 45, tzinfo=UTC) in actuals
    assert datetime(2026, 10, 25, 1, 0, tzinfo=UTC) in actuals
    assert len(actuals) == 3


@pytest.mark.asyncio
async def test_instantaneous_history_orders_repeated_hour_by_physical_time() -> None:
    now = datetime(2026, 10, 25, 3, 0, tzinfo=STOCKHOLM)
    # Recorder order is deliberately reversed.  Both normalize to local
    # 02:00, but they are one physical hour apart.
    states = [
        _state(datetime(2026, 10, 25, 1, 0, tzinfo=UTC), 20.0),
        _state(datetime(2026, 10, 25, 0, 0, tzinfo=UTC), 10.0),
    ]
    executor = AsyncMock(return_value={ENTITY_ID: states})
    recorder = SimpleNamespace(async_add_executor_job=executor)

    with (
        patch(
            "custom_components.hsem.ml.history_reader.get_instance",
            return_value=recorder,
        ),
        patch(
            "custom_components.hsem.ml.history_reader.hsem_now",
            return_value=now,
        ),
    ):
        readings = await HistoryReader(MagicMock()).read_instantaneous_history(
            ENTITY_ID,
            days=0,
        )

    assert [(timestamp.fold, value) for timestamp, value in readings] == [
        (0, 10.0),
        (1, 20.0),
    ]
    assert [utc_key(timestamp) for timestamp, _value in readings] == sorted(
        utc_key(timestamp) for timestamp, _value in readings
    )


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_boundary_is_dropped_without_bridging_recorder_gap(
    bad_value: float,
) -> None:
    readings = [
        (datetime(2026, 8, 20, 7, 44, 50, tzinfo=UTC), 100.0),
        (datetime(2026, 8, 20, 7, 59, 50, tzinfo=UTC), bad_value),
        (datetime(2026, 8, 20, 8, 14, 50, tzinfo=UTC), 100.5),
    ]

    assert _deltas(readings, datetime(2026, 8, 20, 8, 30, tzinfo=UTC)) == []


@pytest.mark.asyncio
async def test_energy_history_with_nonfinite_states_falls_back() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=STOCKHOLM)
    states = [
        _state(datetime(2026, 8, 20, 7, 29, 50, tzinfo=UTC), 100.0),
        _state(datetime(2026, 8, 20, 7, 44, 50, tzinfo=UTC), float("nan")),
        _state(datetime(2026, 8, 20, 7, 59, 50, tzinfo=UTC), float("inf")),
    ]
    executor = AsyncMock(return_value={ENTITY_ID: states})
    recorder = SimpleNamespace(async_add_executor_job=executor)

    with (
        patch(
            "custom_components.hsem.ml.history_reader.get_instance",
            return_value=recorder,
        ),
        patch(
            "custom_components.hsem.ml.history_reader.hsem_now",
            return_value=now,
        ),
    ):
        history = await HistoryReader(MagicMock()).read_energy_history(
            ENTITY_ID,
            days=0,
        )

    assert history == []


@pytest.mark.asyncio
async def test_today_actuals_do_not_publish_nonfinite_boundary() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=STOCKHOLM)
    states = [
        _state(datetime(2026, 8, 20, 7, 29, 50, tzinfo=UTC), 100.0),
        _state(datetime(2026, 8, 20, 7, 44, 50, tzinfo=UTC), float("nan")),
        _state(datetime(2026, 8, 20, 7, 59, 50, tzinfo=UTC), 100.5),
    ]

    actuals, _executor = await _read_actuals(now=now, states=states)

    assert actuals == {}


@pytest.mark.asyncio
async def test_instantaneous_history_filters_nonfinite_temperature() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=STOCKHOLM)
    finite_timestamp = datetime(2026, 8, 20, 7, 45, tzinfo=UTC)
    states = [
        _state(datetime(2026, 8, 20, 7, 30, tzinfo=UTC), float("nan")),
        _state(finite_timestamp, 18.5),
        _state(datetime(2026, 8, 20, 8, 0, tzinfo=UTC), float("-inf")),
    ]
    executor = AsyncMock(return_value={ENTITY_ID: states})
    recorder = SimpleNamespace(async_add_executor_job=executor)

    with (
        patch(
            "custom_components.hsem.ml.history_reader.get_instance",
            return_value=recorder,
        ),
        patch(
            "custom_components.hsem.ml.history_reader.hsem_now",
            return_value=now,
        ),
    ):
        readings = await HistoryReader(MagicMock()).read_instantaneous_history(
            ENTITY_ID,
            days=0,
        )

    assert readings == [(finite_timestamp.astimezone(STOCKHOLM), 18.5)]

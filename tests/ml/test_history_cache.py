"""Incremental energy history refresh (#1355)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from custom_components.hsem.ml import history_cache
from custom_components.hsem.ml.history_cache import (
    FULL_READ_INTERVAL,
    UnitTracker,
    get_energy_history,
    history_ready_at,
)
from custom_components.hsem.ml.history_reader import HistoryReader
from custom_components.hsem.utils.datetime_utils import utc_key

STOCKHOLM = ZoneInfo("Europe/Stockholm")
ENTITY_ID = "sensor.house_energy"


@pytest.fixture(autouse=True)
def _ha_local_timezone():
    def as_stockholm(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=STOCKHOLM)
        return value.astimezone(STOCKHOLM)

    with patch(
        "custom_components.hsem.utils.datetime_utils.dt_util.as_local",
        side_effect=as_stockholm,
    ):
        yield


@pytest.fixture(autouse=True)
def _clear_cache():
    history_cache._energy_history_cache.clear()
    yield
    history_cache._energy_history_cache.clear()


def _state(timestamp: datetime, value: float, unit: str = "kWh") -> SimpleNamespace:
    return SimpleNamespace(
        last_updated=timestamp,
        state=str(value),
        attributes={"unit_of_measurement": unit},
    )


class _Recorder:
    """A recorder whose query honours the requested window like HA does."""

    def __init__(self, states: list[SimpleNamespace]) -> None:
        self.states = sorted(states, key=lambda s: s.last_updated)
        self.windows: list[tuple[datetime, datetime]] = []
        self.flags: list[dict[str, Any]] = []

    def query(
        self,
        _hass: Any,
        start_time: datetime,
        end_time: datetime,
        _entity_ids: list[str],
        **kwargs: Any,
    ) -> dict[str, list[SimpleNamespace]]:
        self.windows.append((start_time, end_time))
        self.flags.append(kwargs)
        before = [s for s in self.states if s.last_updated < start_time]
        inside = [s for s in self.states if start_time < s.last_updated < end_time]
        start_state = before[-1:] if kwargs.get("include_start_time_state") else []
        return {ENTITY_ID: start_state + inside}

    async def run(self, target: Callable[..., Any], *args: Any) -> Any:
        with patch(
            "custom_components.hsem.ml.history_reader.get_significant_states",
            self.query,
        ):
            return target(*args)


def _hass(unit: str | None = "kWh") -> HomeAssistant:
    state = SimpleNamespace(attributes={"unit_of_measurement": unit})
    return SimpleNamespace(states=SimpleNamespace(get=lambda _e: state))  # type: ignore[return-value]


async def _read(
    hass: HomeAssistant,
    recorder: _Recorder,
    now: datetime,
    *,
    min_days: int = 0,
    max_days: int = 90,
) -> list[tuple[datetime, int, float]]:
    instance = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=recorder.run)
    )
    with (
        patch(
            "custom_components.hsem.ml.history_reader.get_instance",
            return_value=instance,
        ),
        patch("custom_components.hsem.ml.history_reader.hsem_now", return_value=now),
    ):
        result = await get_energy_history(
            hass,
            HistoryReader(hass),
            ENTITY_ID,
            min_days=min_days,
            max_days=max_days,
            slot_minutes=15,
            expected_unit=UnitOfEnergy.KILO_WATT_HOUR,
            now_ts=now,
        )
    return result.samples


def _meter_states(
    start: datetime, end: datetime, *, reset_at: datetime | None = None
) -> list[SimpleNamespace]:
    """A meter reporting every 10 minutes, +0.1 kWh per report, optionally reset."""
    states = []
    value = 100.0
    ts = start
    while ts <= end:
        if reset_at is not None and ts >= reset_at and value >= 100.0:
            value = 0.0
        states.append(_state(ts, round(value, 4)))
        value += 0.1
        ts += timedelta(minutes=10)
    return states


@pytest.mark.asyncio
async def test_refresh_within_the_day_reads_only_from_the_last_completed_slot() -> None:
    """The second read starts one slot before the anchor, not 90 days back."""
    first = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    states = _meter_states(first - timedelta(hours=2), first + timedelta(hours=1))
    recorder = _Recorder(states)
    hass = _hass()

    samples = await _read(hass, recorder, first)
    later = first + timedelta(minutes=61)
    extended = await _read(hass, recorder, later)

    assert recorder.windows[0][0] == first - timedelta(days=90)
    # Anchor: the last report inside the last completed slot (09:45-10:00).
    anchor = datetime(2026, 8, 20, 9, 50, tzinfo=UTC)
    assert recorder.windows[1][0] == anchor - timedelta(minutes=15)
    assert recorder.flags[1]["no_attributes"] is True
    assert [utc_key(ts) for ts, _slot, _kwh in extended[: len(samples)]] == [
        utc_key(ts) for ts, _slot, _kwh in samples
    ]
    assert [utc_key(ts) for ts, _slot, _kwh in extended[len(samples) :]] == [
        datetime(2026, 8, 20, 10, 0, tzinfo=UTC) + timedelta(minutes=15 * i)
        for i in range(4)
    ]


@pytest.mark.asyncio
async def test_incremental_refresh_equals_a_full_read_across_reset_and_dst() -> None:
    """Cached slots plus the new ones match one full read of the same window."""
    start = datetime(2026, 10, 24, 20, 0, tzinfo=UTC)
    end = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)  # autumn change at 01:00 UTC
    states = _meter_states(
        start, end, reset_at=datetime(2026, 10, 25, 0, 35, tzinfo=UTC)
    )
    hass = _hass()

    stepwise = _Recorder(states)
    await _read(hass, stepwise, datetime(2026, 10, 25, 0, 20, tzinfo=UTC))
    await _read(hass, stepwise, datetime(2026, 10, 25, 1, 50, tzinfo=UTC))
    incremental = await _read(hass, stepwise, end)

    history_cache._energy_history_cache.clear()
    full = await _read(hass, _Recorder(states), end)

    assert len(stepwise.windows) == 3
    assert incremental == full
    assert len(full) > 20
    # The reset slot is absent from both, never a huge negative delta.
    assert all(kwh > 0 for _ts, _slot, kwh in full)


@pytest.mark.asyncio
async def test_samples_older_than_the_window_are_dropped_on_refresh() -> None:
    """Cached slots that have left the window are pruned, new ones appended."""
    now = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    recorder = _Recorder(_meter_states(now - timedelta(hours=3), now))
    hass = _hass()

    cached = await _read(hass, recorder, now, max_days=1)
    later = now + timedelta(hours=23)
    kept = await _read(hass, recorder, later, max_days=1)

    cutoff = later - timedelta(days=1)
    assert len(recorder.windows) == 2
    assert any(utc_key(ts) < cutoff for ts, _slot, _kwh in cached)
    # The slot that was in progress at the first read is complete now.
    assert kept[:-1] == [s for s in cached if utc_key(s[0]) >= cutoff]
    assert utc_key(kept[-1][0]) == now
    assert kept[:-1]


@pytest.mark.asyncio
async def test_full_read_repeats_once_a_day() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    states = _meter_states(now - timedelta(hours=2), now + timedelta(days=2))
    recorder = _Recorder(states)
    hass = _hass()

    await _read(hass, recorder, now)
    await _read(hass, recorder, now + timedelta(hours=1))
    await _read(hass, recorder, now + FULL_READ_INTERVAL)

    assert [w[0] for w in recorder.windows] == [
        now - timedelta(days=90),
        datetime(2026, 8, 20, 9, 50, tzinfo=UTC) - timedelta(minutes=15),
        now + FULL_READ_INTERVAL - timedelta(days=90),
    ]


@pytest.mark.asyncio
async def test_unit_change_forces_full_reads_with_attributes() -> None:
    """A unit change reads the whole window with per-state units, and keeps doing so."""
    now = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    states = _meter_states(now - timedelta(hours=2), now + timedelta(days=3))
    recorder = _Recorder(states)
    state = SimpleNamespace(attributes={"unit_of_measurement": "kWh"})
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _e: state))

    await _read(hass, recorder, now)  # type: ignore[arg-type]
    state.attributes["unit_of_measurement"] = "Wh"
    await _read(hass, recorder, now + timedelta(hours=1))  # type: ignore[arg-type]
    await _read(hass, recorder, now + timedelta(hours=2))  # type: ignore[arg-type]
    await _read(hass, recorder, now + timedelta(days=1, hours=2))  # type: ignore[arg-type]

    assert [f["no_attributes"] for f in recorder.flags] == [True, False, False, False]
    assert [w[0] for w in recorder.windows][1:] == [
        now + timedelta(hours=1) - timedelta(days=90),  # full read on the change
        # an incremental read may follow within the day, still with attributes
        recorder.windows[2][0],
        now + timedelta(days=1, hours=2) - timedelta(days=90),
    ]
    assert recorder.windows[2][0] > now  # incremental, not a full re-read


@pytest.mark.asyncio
async def test_insufficient_full_read_caches_nothing() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    recorder = _Recorder(_meter_states(now - timedelta(hours=2), now))
    hass = _hass()

    samples = await _read(hass, recorder, now, min_days=14)

    assert samples == []
    assert history_cache._energy_history_cache == {}


@pytest.mark.asyncio
async def test_cached_history_shorter_than_a_raised_window_reports_ready_at() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    recorder = _Recorder(
        _meter_states(now - timedelta(days=15), now + timedelta(hours=1))
    )
    hass = _hass()
    instance = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=recorder.run)
    )

    await _read(hass, recorder, now, min_days=14)
    later = now + timedelta(hours=1)
    with (
        patch(
            "custom_components.hsem.ml.history_reader.get_instance",
            return_value=instance,
        ),
        patch("custom_components.hsem.ml.history_reader.hsem_now", return_value=later),
    ):
        result = await get_energy_history(
            hass,
            HistoryReader(hass),
            ENTITY_ID,
            min_days=21,
            max_days=90,
            slot_minutes=15,
            expected_unit=UnitOfEnergy.KILO_WATT_HOUR,
            now_ts=later,
        )

    assert result.samples == []
    assert result.ready_at == history_ready_at(
        history_cache._energy_history_cache[(id(hass), ENTITY_ID, 15)].samples, 21
    )
    assert result.ready_at is not None
    assert result.ready_at > utc_key(later)


def test_unit_tracker_needs_attributes_for_max_days_after_a_change() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    tracker = UnitTracker("kWh")

    assert tracker.needs_attributes("kWh", now, 90) is False
    assert tracker.needs_attributes("Wh", now, 90) is True
    assert tracker.needs_attributes("Wh", now + timedelta(days=89), 90) is True
    assert tracker.needs_attributes("Wh", now + timedelta(days=90), 90) is False
    assert tracker.unit == "Wh"


def test_history_ready_at_is_none_for_empty_history() -> None:
    assert history_ready_at([], 14) is None

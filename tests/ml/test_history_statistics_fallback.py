"""ML history falls back to long-term statistics for purged days (issue #1433).

A default recorder keeps raw states for 10 days, fewer than the 14 the ML
predictor needs.  The days before the oldest raw sample come from hourly
statistics instead.  The raw recorder and the statistics rows are faked here;
the history reader, the history cache and the status are the real ones.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from custom_components.hsem.ml import history_cache, history_status
from custom_components.hsem.ml.history_cache import get_energy_history
from custom_components.hsem.ml.history_reader import HistoryReader, HistoryResult
from custom_components.hsem.ml.history_status import (
    MLHistoryState,
    MLHistoryStatus,
    last_history_status,
    record_history_status,
)
from custom_components.hsem.utils.datetime_utils import utc_key
from tests.ml.test_history_cache import (
    ENTITY_ID,
    STOCKHOLM,
    _hass,
    _meter_states,
    _Recorder,
)
from tests.ml.test_populator_time_alignment import (
    NOW as POPULATE_NOW,
    _cfg,
    _FakeHass,
    _FakeReader,
    _populate,
    _reset_ml_caches,
)

NOW = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _from_statistics(sample: tuple[datetime, int, float]) -> bool:
    return abs(sample[2] - 0.3) < 1e-9


@pytest.fixture(autouse=True)
def _ha_local_timezone() -> Generator[None]:
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
def _clear_caches() -> Generator[None]:
    _reset_ml_caches()
    history_status._statistics_days.clear()
    yield
    _reset_ml_caches()
    history_status._statistics_days.clear()


def _hours(first: datetime, last: datetime) -> dict[datetime, float]:
    """Hourly statistics of 1.2 kWh from *first* up to, not including, *last*.

    That is 0.3 kWh per 15-minute slot; the raw meter of
    ``_meter_states`` gives 0.1 or 0.2 kWh, so the two can be told apart.
    """
    energy = 1.2
    hours = {}
    hour_start = first
    while hour_start < last:
        hours[hour_start] = energy
        hour_start += HOUR
    return hours


async def _read(
    hass: HomeAssistant,
    recorder: _Recorder,
    now: datetime,
    *,
    min_days: int = 14,
) -> HistoryResult[tuple[datetime, int, float]]:
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
        return await get_energy_history(
            hass,
            HistoryReader(hass),
            ENTITY_ID,
            min_days=min_days,
            max_days=90,
            slot_minutes=15,
            expected_unit=UnitOfEnergy.KILO_WATT_HOUR,
            now_ts=now,
        )


@pytest.mark.asyncio
async def test_purged_days_are_filled_from_statistics(
    long_term_statistics: AsyncMock,
) -> None:
    """Ten days of raw states plus older statistics reach the 14-day minimum."""
    raw_start = NOW - timedelta(days=10)
    recorder = _Recorder(_meter_states(raw_start, NOW))
    long_term_statistics.return_value = _hours(NOW - timedelta(days=30), NOW)
    hass = _hass()

    result = await _read(hass, recorder, NOW)

    samples = result.samples
    assert samples
    assert utc_key(samples[0][0]) == NOW - timedelta(days=30)
    # Statistics end where the raw samples begin: no slot is counted twice.
    keys = [utc_key(sample[0]) for sample in samples]
    assert len(keys) == len(set(keys))
    assert keys == sorted(keys)
    assert all(
        _from_statistics(sample) for sample in samples if utc_key(sample[0]) < raw_start
    )
    assert not any(
        _from_statistics(sample)
        for sample in samples
        if utc_key(sample[0]) >= raw_start + HOUR
    )
    # The window asked of the statistics is the 90-day history window, in kWh.
    long_term_statistics.assert_awaited_once()
    entity_id, start, _until, unit = long_term_statistics.await_args_list[0].args
    assert (entity_id, start, unit) == (ENTITY_ID, NOW - timedelta(days=90), "kWh")
    # The merged history is cached like any other full read.
    entry = history_cache._energy_history_cache[(id(hass), ENTITY_ID, 15)]
    assert entry.anchor is not None
    assert entry.statistics_until is not None
    assert history_status._statistics_days[(id(hass), ENTITY_ID)] == pytest.approx(
        20.0, abs=0.1
    )


@pytest.mark.asyncio
async def test_statistics_alone_are_enough(long_term_statistics: AsyncMock) -> None:
    """A recorder without any raw state still yields a usable history."""
    recorder = _Recorder([])
    long_term_statistics.return_value = _hours(NOW - timedelta(days=20), NOW)

    result = await _read(_hass(), recorder, NOW)

    assert len(result.samples) == 20 * 96
    assert long_term_statistics.await_args_list[0].args[2] == NOW


@pytest.mark.asyncio
async def test_an_hour_reaching_into_the_raw_history_is_not_used(
    long_term_statistics: AsyncMock,
) -> None:
    recorder = _Recorder(_meter_states(NOW - timedelta(hours=2), NOW))
    # The reader may return an hour that starts before the raw history and
    # ends inside it; only whole hours before the oldest raw sample count.
    long_term_statistics.return_value = _hours(NOW - timedelta(days=20), NOW)

    result = await _read(_hass(), recorder, NOW)

    oldest_raw = min(
        utc_key(sample[0]) for sample in result.samples if not _from_statistics(sample)
    )
    newest_statistics = max(
        utc_key(sample[0]) for sample in result.samples if _from_statistics(sample)
    )
    assert newest_statistics + timedelta(minutes=15) <= oldest_raw
    assert oldest_raw - newest_statistics < HOUR + timedelta(minutes=15)


@pytest.mark.asyncio
async def test_too_few_statistics_move_the_ready_date_forward(
    long_term_statistics: AsyncMock,
) -> None:
    recorder = _Recorder(_meter_states(NOW - timedelta(hours=2), NOW))
    long_term_statistics.return_value = _hours(NOW - timedelta(days=5), NOW)
    hass = _hass()

    result = await _read(hass, recorder, NOW)

    assert result.samples == []
    assert result.ready_at == NOW + timedelta(days=9)
    assert history_cache._energy_history_cache == {}
    assert history_status._statistics_days[(id(hass), ENTITY_ID)] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_without_statistics_the_raw_read_is_returned_unchanged(
    long_term_statistics: AsyncMock,
) -> None:
    recorder = _Recorder(_meter_states(NOW - timedelta(hours=2), NOW))
    hass = _hass()

    result = await _read(hass, recorder, NOW)

    assert result.samples == []
    # The ready date is the raw reader's: oldest raw sample plus 14 days.
    assert result.ready_at is not None
    assert result.ready_at > NOW + timedelta(days=13, hours=21)
    assert result.partial
    assert history_cache._energy_history_cache == {}
    long_term_statistics.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_statistics_read_leaves_the_raw_read_unchanged(
    long_term_statistics: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A recorder error in the fallback must not fail the history read."""
    recorder = _Recorder(_meter_states(NOW - timedelta(hours=2), NOW))
    long_term_statistics.side_effect = RuntimeError("database is locked")

    with caplog.at_level("WARNING"):
        result = await _read(_hass(), recorder, NOW)

    assert result.samples == []
    assert result.partial
    assert result.ready_at is not None
    assert history_cache._energy_history_cache == {}
    assert "reading long-term statistics of" in caplog.text


@pytest.mark.asyncio
async def test_sufficient_raw_history_never_reads_statistics(
    long_term_statistics: AsyncMock,
) -> None:
    recorder = _Recorder(_meter_states(NOW - timedelta(days=15), NOW))
    hass = _hass()

    result = await _read(hass, recorder, NOW)

    assert result.samples
    long_term_statistics.assert_not_awaited()
    assert history_status._statistics_days[(id(hass), ENTITY_ID)] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_refresh_keeps_the_statistics_and_appends_raw_slots(
    long_term_statistics: AsyncMock,
) -> None:
    recorder = _Recorder(_meter_states(NOW - timedelta(days=2), NOW + HOUR))
    long_term_statistics.return_value = _hours(NOW - timedelta(days=20), NOW)
    hass = _hass()

    first = await _read(hass, recorder, NOW)
    second = await _read(hass, recorder, NOW + HOUR)

    assert len(second.samples) > len(first.samples)
    assert second.samples[: len(first.samples)] == first.samples
    long_term_statistics.assert_awaited_once()  # the refresh is incremental
    assert history_status._statistics_days[(id(hass), ENTITY_ID)] == pytest.approx(
        18.0, abs=0.1
    )


@pytest.mark.asyncio
async def test_ml_status_is_ready_from_statistics_alone(
    long_term_statistics: AsyncMock,
) -> None:
    """The populator trains on statistics and the sensor attribute says so."""
    cfg = _cfg()
    reader = _FakeReader({"sensor.import": []})
    hass = _FakeHass()
    long_term_statistics.return_value = _hours(
        utc_key(POPULATE_NOW) - timedelta(days=20), utc_key(POPULATE_NOW)
    )

    success, predictor = await _populate(hass, reader, cfg, [])

    assert success is True
    assert predictor is not None
    assert len(predictor.training_histories[0]) == 20 * 96
    status = last_history_status(cast(HomeAssistant, hass), cfg)
    assert status.state is MLHistoryState.READY
    assert status.as_attributes()["ml_statistics_history_days"] == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_ml_status_without_statistics_is_unchanged() -> None:
    cfg = _cfg()
    reader = _FakeReader({"sensor.import": []})
    hass = _FakeHass()

    success, _predictor = await _populate(hass, reader, cfg, [])

    assert success is False
    status = last_history_status(cast(HomeAssistant, hass), cfg)
    assert status.state is MLHistoryState.RECORDER_EMPTY
    assert status.as_attributes()["ml_statistics_history_days"] == pytest.approx(0.0)


def test_net_mode_reports_the_larger_statistics_share() -> None:
    hass = cast(HomeAssistant, _FakeHass())
    cfg = _cfg(export_entity="sensor.export", net=True)
    key = history_status.history_cache_key(hass, cfg)
    assert key is not None
    record_history_status(key, MLHistoryStatus(MLHistoryState.READY, 20.0))
    history_status.record_statistics_days(hass, "sensor.import", 4.0)
    history_status.record_statistics_days(hass, "sensor.export", 6.5)

    status = last_history_status(hass, cfg)

    assert status.statistics_history_days == pytest.approx(6.5)
    assert status.available_history_days == pytest.approx(20.0)


def test_status_before_any_read_has_no_statistics_share() -> None:
    status = last_history_status(cast(HomeAssistant, _FakeHass()), _cfg())

    assert status.state is MLHistoryState.PENDING
    assert status.as_attributes()["ml_statistics_history_days"] is None

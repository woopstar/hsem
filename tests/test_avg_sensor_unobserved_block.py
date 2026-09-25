"""Regression tests for issue #1101 — unobserved hour blocks stored as 0 kWh.

Bug
---
``HSEMAvgSensor._async_store_utility_meter_value`` stored the tracked daily
utility meter's value as soon as the hour block was complete *by the clock*.
When Home Assistant was down across a block, the utility meter fired its
missed daily reset on restart and read ``0``; the average sensor then stored
``0.0`` as a real measurement.  Reported after an HA restart with a fresh
MariaDB: blocks 15-16 … 20-21 all published ``Energy Average 1d = 0,0 kWh``
and dragged the 3d/7d/14d averages down (missing ≠ zero, issues #988/#1056).

Fix
---
A complete block is only stored when it was observed end to end: the meter's
``last_reset`` lies within ``_BLOCK_RESET_TOLERANCE`` of that block's start
*and* the current HA session was already running at the block start.
Otherwise the sample is skipped and existing measurements are left untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.custom_sensors.avg_sensor import (
    _BLOCK_RESET_TOLERANCE,
    HSEMAvgSensor,
    _block_observed,
    _meter_last_reset,
)
from tests.test_avg_sensor_negative_guard import _avg_sensor

_AVG_MODULE = "custom_components.hsem.custom_sensors.avg_sensor"
_METER = "sensor.hsem_house_consumption_energy_15_16_utility_meter"
#: A session that has been running since well before every block under test.
_LONG_RUNNING = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)


def _make_sensor(
    hour_start: int,
    hour_end: int,
    last_reset: object,
    measurements: dict[str, float] | None = None,
    session_started_at: datetime | None = _LONG_RUNNING,
) -> MagicMock:
    """Return a spec'd avg sensor whose tracked meter publishes ``last_reset``."""
    sensor = MagicMock(spec=HSEMAvgSensor)
    sensor.hass = MagicMock()
    attributes = {} if last_reset is None else {"last_reset": last_reset}
    sensor.hass.states.get.return_value = MagicMock(attributes=attributes)
    sensor._session_started_at = session_started_at
    sensor._tracked_entity = _METER
    sensor._measurements = measurements if measurements is not None else {}
    sensor._average = 14
    sensor._hour_start = hour_start
    sensor._hour_end = hour_end
    sensor._async_cleanup_old_measurements = AsyncMock()
    return sensor


async def _store(sensor: MagicMock, now: datetime, meter_value: float) -> None:
    with (
        patch(f"{_AVG_MODULE}.dt_util.now", return_value=now),
        patch(
            f"{_AVG_MODULE}.ha_get_entity_state_and_convert",
            return_value=meter_value,
        ),
    ):
        await HSEMAvgSensor._async_store_utility_meter_value(sensor)


class TestBlockObservedHelper:
    """Unit tests for the pure ``_block_observed`` predicate."""

    _BLOCK_START = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        "offset",
        [timedelta(0), _BLOCK_RESET_TOLERANCE, -_BLOCK_RESET_TOLERANCE],
    )
    def test_reset_at_block_start_is_observed(self, offset: timedelta) -> None:
        last_reset = (self._BLOCK_START + offset).isoformat()
        assert _block_observed(last_reset, _LONG_RUNNING, self._BLOCK_START) is True

    def test_datetime_value_accepted(self) -> None:
        assert (
            _block_observed(self._BLOCK_START, _LONG_RUNNING, self._BLOCK_START) is True
        )

    def test_other_timezone_same_instant_is_observed(self) -> None:
        """A local-time ``last_reset`` equal to the UTC block start matches."""
        last_reset = "2026-09-25T17:00:00+02:00"
        assert _block_observed(last_reset, _LONG_RUNNING, self._BLOCK_START) is True

    def test_session_started_within_tolerance_is_observed(self) -> None:
        """HA (re)started seconds after the block start still saw the block."""
        started = self._BLOCK_START + timedelta(minutes=2)
        last_reset = self._BLOCK_START.isoformat()
        assert _block_observed(last_reset, started, self._BLOCK_START) is True

    @pytest.mark.parametrize(
        "started",
        [
            # Restarted mid-block: the first part of the hour was missed.
            datetime(2026, 9, 25, 15, 20, tzinfo=UTC),
            # Restarted after the block: nothing of it was seen live.
            datetime(2026, 9, 25, 21, 26, tzinfo=UTC),
            None,
        ],
    )
    def test_session_started_after_block_start_is_not_observed(
        self, started: datetime | None
    ) -> None:
        """An on-time ``last_reset`` does not prove the rest of the hour was seen."""
        last_reset = self._BLOCK_START.isoformat()
        assert _block_observed(last_reset, started, self._BLOCK_START) is False

    @pytest.mark.parametrize(
        "last_reset",
        [
            # Missed reset fired on restart after the block ended.
            "2026-09-25T21:26:00+00:00",
            # Reset mid-block: only part of the hour was observed.
            "2026-09-25T15:30:00+00:00",
            # Reset yesterday: value still belongs to an earlier day.
            "2026-09-24T15:00:00+00:00",
        ],
    )
    def test_reset_outside_tolerance_is_not_observed(self, last_reset: str) -> None:
        assert _block_observed(last_reset, _LONG_RUNNING, self._BLOCK_START) is False

    @pytest.mark.parametrize("last_reset", [None, "", "not-a-date", 12345])
    def test_missing_or_unparseable_is_not_observed(self, last_reset: object) -> None:
        assert _block_observed(last_reset, _LONG_RUNNING, self._BLOCK_START) is False


class TestMeterLastReset:
    """``_meter_last_reset`` reads the attribute or returns None."""

    def test_no_tracked_entity_returns_none(self) -> None:
        hass = MagicMock()
        assert _meter_last_reset(hass, None) is None
        hass.states.get.assert_not_called()

    def test_returns_attribute(self) -> None:
        hass = MagicMock()
        hass.states.get.return_value = MagicMock(
            attributes={"last_reset": "2026-09-25T15:00:00+00:00"}
        )
        assert _meter_last_reset(hass, _METER) == "2026-09-25T15:00:00+00:00"


class TestSessionStart:
    """The session start is captured when the sensor joins Home Assistant."""

    @pytest.mark.asyncio
    async def test_added_to_hass_records_session_start(self) -> None:
        sensor = _avg_sensor()
        assert sensor._session_started_at is None
        started = datetime(2026, 9, 25, 21, 26, tzinfo=UTC)
        sensor.async_get_last_state = AsyncMock(return_value=None)  # type: ignore[method-assign]
        sensor._async_handle_update = AsyncMock()  # type: ignore[method-assign]
        with (
            patch(f"{_AVG_MODULE}.dt_util.now", return_value=started),
            patch(f"{_AVG_MODULE}.async_track_time_interval", return_value=MagicMock()),
            patch.object(RestoreEntity, "async_added_to_hass", new_callable=AsyncMock),
        ):
            await sensor.async_added_to_hass()
        assert sensor._session_started_at == started


class TestUnobservedBlockNotStored:
    """Samples from blocks the meter did not observe must be skipped."""

    @pytest.mark.asyncio
    async def test_missed_reset_after_downtime_not_stored_as_zero(self) -> None:
        """Reporter scenario: HA down 15:00-21:26, meter reset on restart."""
        sensor = _make_sensor(
            hour_start=15,
            hour_end=16,
            last_reset="2026-09-25T21:26:00+00:00",
            session_started_at=datetime(2026, 9, 25, 21, 26, tzinfo=UTC),
        )
        await _store(sensor, datetime(2026, 9, 25, 21, 30, tzinfo=UTC), 0.0)
        assert sensor._measurements == {}

    @pytest.mark.asyncio
    async def test_crash_mid_block_with_on_time_reset_not_stored(self) -> None:
        """Meter reset at 15:00, HA crashed 15:20, back at 21:26.

        The restored ``last_reset`` is on time but the value covers only 20
        minutes — the session start proves the block was not fully seen.
        """
        sensor = _make_sensor(
            hour_start=15,
            hour_end=16,
            last_reset="2026-09-25T15:00:00+00:00",
            session_started_at=datetime(2026, 9, 25, 21, 26, tzinfo=UTC),
        )
        await _store(sensor, datetime(2026, 9, 25, 21, 30, tzinfo=UTC), 0.27)
        assert sensor._measurements == {}

    @pytest.mark.asyncio
    async def test_skip_keeps_existing_measurements(self) -> None:
        """Skipping never removes previous valid days from the window."""
        previous = {"2026-09-23": 1.2, "2026-09-24": 1.4}
        sensor = _make_sensor(
            hour_start=15,
            hour_end=16,
            last_reset="2026-09-25T21:26:00+00:00",
            measurements=dict(previous),
        )
        await _store(sensor, datetime(2026, 9, 25, 21, 30, tzinfo=UTC), 0.0)
        assert sensor._measurements == previous

    @pytest.mark.asyncio
    async def test_partial_block_after_mid_block_restart_not_stored(self) -> None:
        """Meter reset at 22:26 inside the 22-23 block: partial hour skipped."""
        sensor = _make_sensor(
            hour_start=22,
            hour_end=23,
            last_reset="2026-09-25T22:26:00+00:00",
            session_started_at=datetime(2026, 9, 25, 22, 26, tzinfo=UTC),
        )
        await _store(sensor, datetime(2026, 9, 25, 23, 5, tzinfo=UTC), 0.31)
        assert sensor._measurements == {}

    @pytest.mark.asyncio
    async def test_stale_restored_value_from_yesterday_not_stored(self) -> None:
        """Before the missed reset runs the meter still holds yesterday's value."""
        sensor = _make_sensor(
            hour_start=15,
            hour_end=16,
            last_reset="2026-09-24T15:00:00+00:00",
            session_started_at=datetime(2026, 9, 25, 21, 26, tzinfo=UTC),
        )
        await _store(sensor, datetime(2026, 9, 25, 21, 26, tzinfo=UTC), 1.33)
        assert sensor._measurements == {}

    @pytest.mark.asyncio
    async def test_missing_last_reset_not_stored(self) -> None:
        sensor = _make_sensor(hour_start=15, hour_end=16, last_reset=None)
        await _store(sensor, datetime(2026, 9, 25, 16, 5, tzinfo=UTC), 0.8)
        assert sensor._measurements == {}

    @pytest.mark.asyncio
    async def test_missing_meter_state_not_stored(self) -> None:
        sensor = _make_sensor(hour_start=15, hour_end=16, last_reset=None)
        sensor.hass.states.get.return_value = None
        await _store(sensor, datetime(2026, 9, 25, 16, 5, tzinfo=UTC), 0.8)
        assert sensor._measurements == {}


class TestObservedBlockStored:
    """A meter that reset at the block start still produces a sample."""

    @pytest.mark.asyncio
    async def test_normal_block_stored(self) -> None:
        sensor = _make_sensor(
            hour_start=15,
            hour_end=16,
            last_reset="2026-09-25T15:00:00.123456+00:00",
        )
        await _store(sensor, datetime(2026, 9, 25, 16, 5, tzinfo=UTC), 0.82)
        assert sensor._measurements == {"2026-09-25": pytest.approx(0.82)}

    @pytest.mark.asyncio
    async def test_genuine_zero_still_stored(self) -> None:
        """An observed block with zero consumption is a real measured zero."""
        sensor = _make_sensor(
            hour_start=15,
            hour_end=16,
            last_reset="2026-09-25T15:00:00+00:00",
        )
        await _store(sensor, datetime(2026, 9, 25, 16, 5, tzinfo=UTC), 0.0)
        assert sensor._measurements == {"2026-09-25": pytest.approx(0.0)}

    @pytest.mark.asyncio
    async def test_overnight_block_uses_previous_date_block_start(self) -> None:
        """23→00: the post-midnight sample checks the previous day's 23:00."""
        sensor = _make_sensor(
            hour_start=23,
            hour_end=0,
            last_reset="2026-09-24T23:00:00+00:00",
        )
        await _store(sensor, datetime(2026, 9, 25, 0, 5, tzinfo=UTC), 0.58)
        assert sensor._measurements == {"2026-09-24": pytest.approx(0.58)}

    @pytest.mark.asyncio
    async def test_restart_later_in_day_keeps_earlier_blocks_and_stores_new(
        self,
    ) -> None:
        """A normal restart loses nothing.

        Blocks completed before the restart were stored by the previous
        session and survive via restore; blocks that start after the restart
        are observed by the new session and stored normally.
        """
        sensor = _make_sensor(
            hour_start=15,
            hour_end=16,
            last_reset="2026-09-25T15:00:00+00:00",
            measurements={"2026-09-24": 0.9},
            session_started_at=datetime(2026, 9, 25, 12, 0, tzinfo=UTC),
        )
        await _store(sensor, datetime(2026, 9, 25, 16, 5, tzinfo=UTC), 0.8)
        assert sensor._measurements == {
            "2026-09-24": pytest.approx(0.9),
            "2026-09-25": pytest.approx(0.8),
        }

    @pytest.mark.asyncio
    async def test_overnight_block_missed_reset_not_stored(self) -> None:
        """23→00 reset fired at 07:00 after overnight downtime is skipped."""
        sensor = _make_sensor(
            hour_start=23,
            hour_end=0,
            last_reset="2026-09-25T07:00:00+00:00",
        )
        await _store(sensor, datetime(2026, 9, 25, 7, 5, tzinfo=UTC), 0.0)
        assert sensor._measurements == {}

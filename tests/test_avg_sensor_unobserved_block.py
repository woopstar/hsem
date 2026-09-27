"""Regression tests for unobserved hour blocks (issues #1101 and #1110).

Issue #1101
-----------
``HSEMAvgSensor._async_store_utility_meter_value`` stored the tracked daily
utility meter's value as soon as the hour block was complete *by the clock*.
When Home Assistant was down across a block, the utility meter fired its
missed daily reset on restart and read ``0``; the average sensor then stored
``0.0`` as a real measurement (missing ≠ zero, issues #988/#1056).

Issue #1110
-----------
The #1101 fix also required "this session started before the block". Any
restart or HSEM reload after a block began therefore discarded a fully metered
block. Reporter: the 09-10 block (11.64 kWh) and the 17-18 block (2.15 kWh) were
metered with on-time resets but skipped after quick restarts. With a young
window those hours had no sample and the load forecast stayed
``source_unavailable`` for more than 24 h.

Fix
---
A complete block is stored when the meter's ``last_reset`` matches the block
start *and* the persisted unobserved (downtime) intervals overlap the block by
at most ``BLOCK_RESET_TOLERANCE``. Without a heartbeat the whole past is
unobserved, which is the original strict rule.
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
from custom_components.hsem.custom_sensors.block_observation import (
    MAX_UNOBSERVED_INTERVALS,
    AvgSensorExtraData,
    prune_unobserved,
    restore_unobserved,
    unobserved_overlap,
)
from tests.test_avg_sensor_negative_guard import _avg_sensor

_AVG_MODULE = "custom_components.hsem.custom_sensors.avg_sensor"
_METER = "sensor.hsem_house_consumption_energy_15_16_utility_meter"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _down(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Return a single unobserved interval."""
    return [(start, end)]


#: A sensor that has been observing continuously since well before every block.
_ALWAYS_UP: list[tuple[datetime, datetime]] = _down(
    _EPOCH, datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
)


def _make_sensor(
    hour_start: int,
    hour_end: int,
    last_reset: object,
    measurements: dict[str, float] | None = None,
    unobserved: list[tuple[datetime, datetime]] | None = _ALWAYS_UP,
) -> MagicMock:
    """Return a spec'd avg sensor whose tracked meter publishes ``last_reset``."""
    sensor = MagicMock(spec=HSEMAvgSensor)
    sensor.hass = MagicMock()
    attributes = {} if last_reset is None else {"last_reset": last_reset}
    sensor.hass.states.get.return_value = MagicMock(attributes=attributes)
    sensor._unobserved = unobserved
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
    """Unit tests for the pure ``block_observed`` predicate."""

    _BLOCK_START = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
    _BLOCK_END = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)

    def _observed(
        self, last_reset: object, unobserved: list | None = _ALWAYS_UP
    ) -> bool:
        return _block_observed(
            last_reset, unobserved, self._BLOCK_START, self._BLOCK_END
        )

    @pytest.mark.parametrize(
        "offset",
        [timedelta(0), _BLOCK_RESET_TOLERANCE, -_BLOCK_RESET_TOLERANCE],
    )
    def test_reset_at_block_start_is_observed(self, offset: timedelta) -> None:
        assert self._observed((self._BLOCK_START + offset).isoformat()) is True

    def test_datetime_value_accepted(self) -> None:
        assert self._observed(self._BLOCK_START) is True

    def test_other_timezone_same_instant_is_observed(self) -> None:
        """A local-time ``last_reset`` equal to the UTC block start matches."""
        assert self._observed("2026-09-25T17:00:00+02:00") is True

    def test_session_started_within_tolerance_is_observed(self) -> None:
        """HA (re)started seconds after the block start still saw the block."""
        unobserved = _down(_EPOCH, self._BLOCK_START + timedelta(minutes=2))
        assert self._observed(self._BLOCK_START.isoformat(), unobserved) is True

    @pytest.mark.parametrize(
        "restart_at",
        [
            # Reporter (issue #1110): restart at 17:26:30 inside 17-18.
            datetime(2026, 9, 25, 15, 26, 30, tzinfo=UTC),
            # Restart after the block but before the post-block storage tick.
            datetime(2026, 9, 25, 16, 1, tzinfo=UTC),
        ],
    )
    def test_quick_restart_keeps_the_block(self, restart_at: datetime) -> None:
        """A restart lasting seconds is not downtime (issue #1110)."""
        unobserved = _down(restart_at - timedelta(seconds=40), restart_at)
        assert self._observed(self._BLOCK_START.isoformat(), unobserved) is True

    @pytest.mark.parametrize(
        "unobserved",
        [
            # Crash at 15:20, back at 21:26: most of the hour unobserved.
            _down(
                datetime(2026, 9, 25, 15, 20, tzinfo=UTC),
                datetime(2026, 9, 25, 21, 26, tzinfo=UTC),
            ),
            # Down from before the block until mid-block.
            _down(_EPOCH, datetime(2026, 9, 25, 15, 20, tzinfo=UTC)),
            # Unknown history (no heartbeat): the whole past is unobserved.
            _down(_EPOCH, datetime(2026, 9, 25, 21, 26, tzinfo=UTC)),
            # Several short outages that add up to more than the tolerance.
            [
                (
                    datetime(2026, 9, 25, 15, 10, tzinfo=UTC),
                    datetime(2026, 9, 25, 15, 13, tzinfo=UTC),
                ),
                (
                    datetime(2026, 9, 25, 15, 40, tzinfo=UTC),
                    datetime(2026, 9, 25, 15, 43, tzinfo=UTC),
                ),
            ],
        ],
    )
    def test_real_downtime_in_block_is_not_observed(self, unobserved: list) -> None:
        """An on-time ``last_reset`` does not prove the rest of the hour was seen."""
        assert self._observed(self._BLOCK_START.isoformat(), unobserved) is False

    def test_sensor_not_yet_added_is_not_observed(self) -> None:
        assert self._observed(self._BLOCK_START.isoformat(), None) is False

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
        assert self._observed(last_reset) is False

    @pytest.mark.parametrize(
        "last_reset", [None, "", "not-a-date", 12345, "2026-09-25T15:00:00"]
    )
    def test_missing_or_unparseable_is_not_observed(self, last_reset: object) -> None:
        assert self._observed(last_reset) is False


class TestUnobservedIntervals:
    """Heartbeat persistence and downtime reconstruction (issue #1110)."""

    _START = datetime(2026, 9, 26, 15, 26, 30, tzinfo=UTC)

    def test_no_payload_is_strict(self) -> None:
        """Without a heartbeat the whole past is unobserved (fail closed)."""
        assert restore_unobserved(None, self._START) == [(_EPOCH, self._START)]

    @pytest.mark.parametrize(
        "payload",
        [{}, {"last_alive": "garbage"}, {"last_alive": 3}, "not-a-dict"],
    )
    def test_unreadable_heartbeat_is_strict(self, payload: object) -> None:
        assert restore_unobserved(payload, self._START) == [(_EPOCH, self._START)]

    def test_downtime_since_last_heartbeat_is_added(self) -> None:
        alive = self._START - timedelta(seconds=35)
        payload = AvgSensorExtraData(alive, []).as_dict()
        assert restore_unobserved(payload, self._START) == [(alive, self._START)]

    def test_round_trip_keeps_previous_intervals(self) -> None:
        earlier = (
            self._START - timedelta(hours=3),
            self._START - timedelta(hours=3) + timedelta(seconds=20),
        )
        alive = self._START - timedelta(seconds=30)
        payload = AvgSensorExtraData(alive, [earlier]).as_dict()
        assert restore_unobserved(payload, self._START) == [
            earlier,
            (alive, self._START),
        ]

    def test_malformed_intervals_are_ignored(self) -> None:
        alive = self._START - timedelta(seconds=30)
        payload = {
            "last_alive": alive.isoformat(),
            "unobserved": [["bad"], ["x", "y"], 5, [alive.isoformat(), "2000-01-01"]],
        }
        assert restore_unobserved(payload, self._START) == [(alive, self._START)]

    def test_heartbeat_after_start_adds_no_interval(self) -> None:
        """Clock skew must not produce a negative interval."""
        payload = AvgSensorExtraData(self._START + timedelta(seconds=5), []).as_dict()
        assert restore_unobserved(payload, self._START) == []

    def test_prune_drops_expired_and_bounds_length(self) -> None:
        now = self._START
        expired = (now - timedelta(days=5), now - timedelta(days=5, seconds=-10))
        recent = [
            (now - timedelta(minutes=i + 1), now - timedelta(minutes=i))
            for i in range(MAX_UNOBSERVED_INTERVALS + 5)
        ]
        kept = prune_unobserved([expired, *recent], now)
        assert expired not in kept
        assert len(kept) == MAX_UNOBSERVED_INTERVALS
        assert kept[-1] == recent[0]

    def test_overlap_is_clipped_to_the_block(self) -> None:
        start = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
        intervals = [
            (start - timedelta(minutes=10), start + timedelta(minutes=2)),
            (start + timedelta(minutes=58), start + timedelta(hours=2)),
        ]
        assert unobserved_overlap(
            intervals, start, start + timedelta(hours=1)
        ) == timedelta(minutes=4)


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


class TestSessionLifecycle:
    """The sensor restores and persists its downtime history."""

    async def _add(
        self, extra: AvgSensorExtraData | None, started: datetime
    ) -> HSEMAvgSensor:
        sensor = _avg_sensor()
        sensor.async_get_last_state = AsyncMock(return_value=None)  # type: ignore[method-assign]
        sensor.async_get_last_extra_data = AsyncMock(return_value=extra)  # type: ignore[method-assign]
        sensor._async_handle_update = AsyncMock()  # type: ignore[method-assign]
        with (
            patch(f"{_AVG_MODULE}.dt_util.now", return_value=started),
            patch(f"{_AVG_MODULE}.async_track_time_interval", return_value=MagicMock()),
            patch.object(RestoreEntity, "async_added_to_hass", new_callable=AsyncMock),
        ):
            await sensor.async_added_to_hass()
        return sensor

    @pytest.mark.asyncio
    async def test_first_run_is_strict(self) -> None:
        sensor = _avg_sensor()
        assert sensor._unobserved is None
        started = datetime(2026, 9, 25, 21, 26, tzinfo=UTC)
        sensor = await self._add(None, started)
        assert sensor._unobserved == [(_EPOCH, started)]

    @pytest.mark.asyncio
    async def test_restart_records_only_the_real_downtime(self) -> None:
        started = datetime(2026, 9, 26, 15, 26, 30, tzinfo=UTC)
        alive = started - timedelta(seconds=40)
        sensor = await self._add(AvgSensorExtraData(alive, []), started)
        assert sensor._unobserved == [(alive, started)]

    def test_extra_restore_data_carries_heartbeat_and_intervals(self) -> None:
        sensor = _avg_sensor()
        now = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
        down = (now - timedelta(hours=1), now - timedelta(minutes=59))
        sensor._unobserved = [down]
        with patch(
            "custom_components.hsem.custom_sensors.avg_sensor.dt_util.utcnow",
            return_value=now,
        ):
            data = sensor.extra_restore_state_data.as_dict()
        assert data == {
            "last_alive": now.isoformat(),
            "unobserved": [[down[0].isoformat(), down[1].isoformat()]],
        }


class TestUnobservedBlockNotStored:
    """Samples from blocks the meter did not observe must be skipped."""

    @pytest.mark.asyncio
    async def test_missed_reset_after_downtime_not_stored_as_zero(self) -> None:
        """#1101 reporter scenario: HA down 15:00-21:26, meter reset on restart."""
        sensor = _make_sensor(
            hour_start=15,
            hour_end=16,
            last_reset="2026-09-25T21:26:00+00:00",
            unobserved=_down(
                datetime(2026, 9, 25, 14, 59, tzinfo=UTC),
                datetime(2026, 9, 25, 21, 26, tzinfo=UTC),
            ),
        )
        await _store(sensor, datetime(2026, 9, 25, 21, 30, tzinfo=UTC), 0.0)
        assert sensor._measurements == {}

    @pytest.mark.asyncio
    async def test_crash_mid_block_with_on_time_reset_not_stored(self) -> None:
        """Meter reset at 15:00, HA crashed 15:20, back at 21:26.

        The restored ``last_reset`` is on time, but the value covers only 20
        minutes. The downtime interval shows the block was not fully seen.
        """
        sensor = _make_sensor(
            hour_start=15,
            hour_end=16,
            last_reset="2026-09-25T15:00:00+00:00",
            unobserved=_down(
                datetime(2026, 9, 25, 15, 20, tzinfo=UTC),
                datetime(2026, 9, 25, 21, 26, tzinfo=UTC),
            ),
        )
        await _store(sensor, datetime(2026, 9, 25, 21, 30, tzinfo=UTC), 0.27)
        assert sensor._measurements == {}

    @pytest.mark.asyncio
    async def test_first_run_after_upgrade_is_strict(self) -> None:
        """No heartbeat yet: a block that began before the session is skipped."""
        sensor = _make_sensor(
            hour_start=15,
            hour_end=16,
            last_reset="2026-09-25T15:00:00+00:00",
            unobserved=_down(_EPOCH, datetime(2026, 9, 25, 15, 26, tzinfo=UTC)),
        )
        await _store(sensor, datetime(2026, 9, 25, 16, 5, tzinfo=UTC), 1.1)
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
    async def test_reporter_block_after_quick_restart_is_stored(self) -> None:
        """Issue #1110: 09-10 local block with 11.64 kWh, HA restarted for ~40 s.

        The restart came after the block, before the next storage tick. The
        meter reset on time, so the block was fully metered and is stored.
        The test clock is UTC: 09:00 CEST is 07:00 UTC.
        """
        sensor = _make_sensor(
            hour_start=7,
            hour_end=8,
            last_reset="2026-09-26T07:00:00.001973+00:00",
            unobserved=[
                (
                    datetime(2026, 9, 26, 8, 1, 0, tzinfo=UTC),
                    datetime(2026, 9, 26, 8, 1, 40, tzinfo=UTC),
                )
            ],
        )
        await _store(sensor, datetime(2026, 9, 26, 8, 5, tzinfo=UTC), 11.64)
        assert sensor._measurements == {"2026-09-26": pytest.approx(11.64)}

    @pytest.mark.asyncio
    async def test_restart_inside_block_is_stored(self) -> None:
        """Issue #1110: HSEM reloaded at 17:26:30 inside 17-18, seconds of downtime.

        The test clock is UTC: 17:00 CEST is 15:00 UTC.
        """
        restart = datetime(2026, 9, 26, 15, 26, 30, tzinfo=UTC)
        sensor = _make_sensor(
            hour_start=15,
            hour_end=16,
            last_reset="2026-09-26T15:00:00.000953+00:00",
            unobserved=[(restart - timedelta(seconds=15), restart)],
        )
        await _store(sensor, datetime(2026, 9, 26, 16, 5, tzinfo=UTC), 2.15)
        assert sensor._measurements == {"2026-09-26": pytest.approx(2.15)}

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
            unobserved=_down(_EPOCH, datetime(2026, 9, 25, 12, 0, tzinfo=UTC)),
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

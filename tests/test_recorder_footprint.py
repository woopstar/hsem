"""Regression tests for issue #1099 — HSEM recorder database bloat.

Bug
---
``HSEMAvgSensor`` (96 instances) wrote its state on every 30 s poll, every
5-minute timer tick and every utility-meter change, and each write carried a
fresh ``last_updated`` timestamp plus the full ``measurements`` dict.  HA's
recorder stores a new ``states`` row and a new, non-deduplicable
``state_attributes`` row whenever any attribute changes, so each sensor
produced ~3,200 rows/day for a value that changes about once a day.
``HSEMForecastAccuracySensor`` also recorded its ~9 KB
``_forecast_tracker_data`` persistence blob every cycle.

Fix
---
- The average sensor writes only when its average or measurements change
  (plus once per session), and no longer polls.
- Volatile timestamps and persistence blobs are listed in
  ``_unrecorded_attributes``.  Restart restore is unaffected because
  ``RestoreEntity`` storage is independent of the recorder.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.custom_sensors.avg_sensor import (
    HSEMAvgSensor,
    _float_changed,
    _measurements_changed,
)
from custom_components.hsem.custom_sensors.forecast_accuracy_sensor import (
    HSEMForecastAccuracySensor,
)
from custom_components.hsem.custom_sensors.house_consumption_power_sensor import (
    HSEMHouseConsumptionPowerSensor,
)
from tests.test_avg_sensor_negative_guard import _avg_sensor

_AVG_MODULE = "custom_components.hsem.custom_sensors.avg_sensor"


def _ready_sensor(average: int = 3) -> HSEMAvgSensor:
    """Return an average sensor with listeners and HA writes stubbed out."""
    sensor = _avg_sensor(average=average)
    sensor._async_track_entities = AsyncMock()  # type: ignore[method-assign]
    sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]
    return sensor


async def _tick(sensor: HSEMAvgSensor, now: datetime, meter_value: float) -> None:
    """Run one update cycle at ``now`` with the utility meter at ``meter_value``."""
    with (
        patch(f"{_AVG_MODULE}.dt_util.now", return_value=now),
        patch(
            f"{_AVG_MODULE}.ha_get_entity_state_and_convert",
            return_value=meter_value,
        ),
    ):
        await sensor._async_handle_update()


def _write_count(sensor: HSEMAvgSensor) -> int:
    write = sensor.async_write_ha_state
    assert isinstance(write, MagicMock)
    return write.call_count


class TestUnrecordedAttributes:
    """Volatile and persistence-only attributes stay out of the recorder."""

    def test_avg_sensor_excludes_timestamp_and_measurements(self) -> None:
        assert "last_updated" in HSEMAvgSensor._unrecorded_attributes
        assert "measurements" in HSEMAvgSensor._unrecorded_attributes

    def test_power_sensor_excludes_timestamp(self) -> None:
        assert "last_updated" in HSEMHouseConsumptionPowerSensor._unrecorded_attributes

    def test_forecast_accuracy_excludes_persistence_blob(self) -> None:
        assert (
            "_forecast_tracker_data"
            in HSEMForecastAccuracySensor._unrecorded_attributes
        )

    def test_avg_sensor_still_publishes_restore_attributes(self) -> None:
        """Unrecorded attributes are still exposed, so RestoreEntity keeps them."""
        sensor = _avg_sensor()
        sensor._measurements = {"2026-08-08": 0.42}
        sensor._last_updated = "2026-08-08T15:05:00+00:00"

        attributes = sensor.extra_state_attributes

        assert attributes["measurements"] == {"2026-08-08": 0.42}
        assert attributes["last_updated"] == "2026-08-08T15:05:00+00:00"


class TestAvgSensorWritesOnlyOnChange:
    """No-op update ticks must not produce state writes."""

    def test_avg_sensor_does_not_poll(self) -> None:
        assert _avg_sensor().should_poll is False

    @pytest.mark.asyncio
    async def test_first_update_always_writes(self) -> None:
        """The first update of a session publishes the state, even if empty."""
        sensor = _ready_sensor()

        await _tick(sensor, datetime(2026, 8, 8, 10, 0, tzinfo=UTC), 0.1)

        assert _write_count(sensor) == 1
        assert sensor.state is None

    @pytest.mark.asyncio
    async def test_repeated_ticks_without_change_do_not_write(self) -> None:
        """Mid-day ticks while the block is incomplete are no-ops."""
        sensor = _ready_sensor()
        sensor._measurements = {"2026-08-07": 0.4}

        await _tick(sensor, datetime(2026, 8, 8, 10, 0, tzinfo=UTC), 0.1)
        first_last_updated = sensor._last_updated
        for minute in (5, 10, 15, 20):
            await _tick(sensor, datetime(2026, 8, 8, 10, minute, tzinfo=UTC), 0.1)

        assert _write_count(sensor) == 1
        assert sensor._last_updated == first_last_updated

    @pytest.mark.asyncio
    async def test_completed_block_writes_once(self) -> None:
        """Storing the completed block's sample writes; re-sampling does not."""
        sensor = _ready_sensor()
        sensor._measurements = {"2026-08-07": 0.4}
        await _tick(sensor, datetime(2026, 8, 8, 10, 0, tzinfo=UTC), 0.1)

        await _tick(sensor, datetime(2026, 8, 8, 15, 5, tzinfo=UTC), 0.6)
        await _tick(sensor, datetime(2026, 8, 8, 15, 10, tzinfo=UTC), 0.6)
        await _tick(sensor, datetime(2026, 8, 8, 18, 0, tzinfo=UTC), 0.6)

        assert _write_count(sensor) == 2
        assert sensor.state == pytest.approx(0.5)
        assert sensor._measurements == {"2026-08-07": 0.4, "2026-08-08": 0.6}

    @pytest.mark.asyncio
    async def test_meter_update_after_block_close_writes(self) -> None:
        """A late meter update that changes the stored sample is published."""
        sensor = _ready_sensor()
        await _tick(sensor, datetime(2026, 8, 8, 15, 1, tzinfo=UTC), 0.60)
        await _tick(sensor, datetime(2026, 8, 8, 15, 2, tzinfo=UTC), 0.62)

        assert _write_count(sensor) == 2
        assert sensor.state == pytest.approx(0.62)

    @pytest.mark.asyncio
    async def test_window_rollover_writes(self) -> None:
        """Dropping the oldest day changes measurements and is published."""
        sensor = _ready_sensor(average=1)
        sensor._measurements = {"2026-08-07": 0.5}
        await _tick(sensor, datetime(2026, 8, 8, 10, 0, tzinfo=UTC), 0.1)

        await _tick(sensor, datetime(2026, 8, 8, 15, 5, tzinfo=UTC), 0.5)

        assert _write_count(sensor) == 2
        assert sensor._measurements == {"2026-08-08": 0.5}
        assert sensor.state == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_restart_writes_restored_state_once(self) -> None:
        """After restore, the first update publishes; later no-ops do not."""
        sensor = _ready_sensor()
        sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(
                state="0.4",
                attributes={
                    "measurements": {"2026-08-07": 0.4},
                    "last_updated": "2026-08-07T15:05:00+00:00",
                },
            )
        )
        with (
            patch(f"{_AVG_MODULE}.async_track_time_interval", return_value=MagicMock()),
            patch.object(RestoreEntity, "async_added_to_hass", new_callable=AsyncMock),
            patch(
                f"{_AVG_MODULE}.dt_util.now",
                return_value=datetime(2026, 8, 8, 10, 0, tzinfo=UTC),
            ),
            patch(f"{_AVG_MODULE}.ha_get_entity_state_and_convert", return_value=0.1),
        ):
            await sensor.async_added_to_hass()

        assert sensor._measurements == {"2026-08-07": 0.4}
        assert sensor.state == pytest.approx(0.4)
        assert _write_count(sensor) == 1

        await _tick(sensor, datetime(2026, 8, 8, 10, 5, tzinfo=UTC), 0.1)
        assert _write_count(sensor) == 1


class TestChangeHelpers:
    """The change detectors use an epsilon, never float equality."""

    @pytest.mark.parametrize(
        ("previous", "current", "expected"),
        [
            (None, None, False),
            (None, 0.0, True),
            (0.0, None, True),
            (0.5, 0.5, False),
            (0.5, 0.5 + 1e-12, False),
            (0.5, 0.51, True),
        ],
    )
    def test_float_changed(
        self, previous: float | None, current: float | None, expected: bool
    ) -> None:
        assert _float_changed(previous, current) is expected

    def test_measurements_changed(self) -> None:
        base = {"2026-08-07": 0.4}

        assert _measurements_changed(base, {"2026-08-07": 0.4}) is False
        assert _measurements_changed(base, {"2026-08-07": 0.45}) is True
        assert _measurements_changed(base, {"2026-08-08": 0.4}) is True
        assert _measurements_changed({}, base) is True

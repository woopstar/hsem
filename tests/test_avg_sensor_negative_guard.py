"""Regression tests for issue #938 — stale negative rolling-average sample.

Bug
---
``HSEMAvgSensor._async_store_utility_meter_value`` never validated that the
tracked utility meter's value was finite and non-negative before persisting
it as a day's sample.  A misconfigured net-consumption accounting mode
produced a negative reading for one hour block; once persisted it became the
sole "1d" rolling-average sample (the "1d" window holds only 1 entry) and
kept ``assess_load_forecast()`` (``coordinator_helpers.py``) failing closed
with ``reason="invalid_future_values"`` — engaging ``safety_hold`` — even
after the source misconfiguration was corrected, because the window would
not refresh until that specific hour block completed again on a later day.

Fix
---
Reject non-finite/negative readings both when storing a new sample
(``_async_store_utility_meter_value``) and when replaying a persisted
sample across restart (``async_added_to_hass``), so a bad reading is
skipped (logged) rather than poisoning the window, and the average
recovers on the very next completed block.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator_helpers import assess_load_forecast
from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.utils.recommendations import Recommendations


def _make_sensor(
    hour_start: int,
    hour_end: int,
    measurements: dict[str, float] | None = None,
    average: int = 1,
) -> MagicMock:
    sensor = MagicMock(spec=HSEMAvgSensor)
    sensor.hass = MagicMock()
    sensor._tracked_entity = "sensor.daily_kwh"
    sensor._measurements = measurements if measurements is not None else {}
    sensor._average = average
    sensor._hour_start = hour_start
    sensor._hour_end = hour_end
    sensor._async_cleanup_old_measurements = AsyncMock()
    return sensor


async def _store(sensor: MagicMock, now: datetime, meter_value: float) -> None:
    with (
        patch(
            "custom_components.hsem.custom_sensors.avg_sensor.dt_util.now",
            return_value=now,
        ),
        patch(
            "custom_components.hsem.custom_sensors.avg_sensor"
            ".ha_get_entity_state_and_convert",
            return_value=meter_value,
        ),
    ):
        await HSEMAvgSensor._async_store_utility_meter_value(sensor)


def _avg_sensor(average: int = 1) -> HSEMAvgSensor:
    """Return a real average sensor with its HA dependencies mocked."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    entry.data = {}
    sensor = HSEMAvgSensor(
        config_entry=entry,
        hour_start=14,
        hour_end=15,
        avg=average,
        tracked_entity="sensor.utility",
        name="Test average",
        unique_id="test_average",
        entity_id="sensor.test_average",
    )
    sensor.hass = MagicMock()
    return sensor


class TestNegativeReadingRejectedAtWriteTime:
    """A bad reading from a misconfigured utility meter must be skipped."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_value", [-0.205, float("nan"), float("inf"), float("-inf")]
    )
    async def test_bad_reading_not_stored(self, bad_value: float) -> None:
        sensor = _make_sensor(hour_start=14, hour_end=15)
        await _store(sensor, datetime(2026, 8, 8, 15, 5, tzinfo=UTC), bad_value)
        assert sensor._measurements == {}

    @pytest.mark.asyncio
    async def test_negative_reading_does_not_overwrite_existing_sample(self) -> None:
        """A glitched reading must not clobber a previously valid sample."""
        sensor = _make_sensor(
            hour_start=14, hour_end=15, measurements={"2026-08-07": 0.42}
        )
        await _store(sensor, datetime(2026, 8, 8, 15, 5, tzinfo=UTC), -0.205)
        assert sensor._measurements == {"2026-08-07": 0.42}

    @pytest.mark.asyncio
    async def test_valid_reading_after_rejected_negative_recovers(self) -> None:
        """Once the source config is corrected, the next block stores fine."""
        sensor = _make_sensor(hour_start=14, hour_end=15, average=1)
        # Day 1: misconfigured net-consumption accounting — rejected.
        await _store(sensor, datetime(2026, 8, 8, 15, 5, tzinfo=UTC), -0.205)
        assert sensor._measurements == {}
        # Day 2: source config corrected, a real positive reading is stored.
        await _store(sensor, datetime(2026, 8, 9, 15, 5, tzinfo=UTC), 0.35)
        assert sensor._measurements == {"2026-08-09": 0.35}


class TestNegativeMeasurementRejectedAtRestoreTime:
    """A negative value persisted by a pre-fix version must not be replayed."""

    @pytest.mark.asyncio
    async def test_negative_restored_measurement_is_discarded(self) -> None:
        sensor = _avg_sensor()
        sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(
                state="-0.21",
                attributes={"measurements": {"2026-08-08": -0.205}},
            )
        )
        sensor._async_handle_update = AsyncMock()  # type: ignore[method-assign]
        with (
            patch(
                "custom_components.hsem.custom_sensors.avg_sensor"
                ".async_track_time_interval",
                return_value=MagicMock(),
            ),
            patch.object(RestoreEntity, "async_added_to_hass", new_callable=AsyncMock),
        ):
            await sensor.async_added_to_hass()
        assert sensor._measurements == {}

    @pytest.mark.asyncio
    async def test_mixed_restored_measurements_only_valid_kept(self) -> None:
        sensor = _avg_sensor(average=3)
        sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(
                state="0.4",
                attributes={
                    "measurements": {
                        "2026-08-06": 0.3,
                        "2026-08-07": -0.205,
                        "2026-08-08": float("nan"),
                    }
                },
            )
        )
        sensor._async_handle_update = AsyncMock()  # type: ignore[method-assign]
        with (
            patch(
                "custom_components.hsem.custom_sensors.avg_sensor"
                ".async_track_time_interval",
                return_value=MagicMock(),
            ),
            patch.object(RestoreEntity, "async_added_to_hass", new_callable=AsyncMock),
        ):
            await sensor.async_added_to_hass()
        assert sensor._measurements == {"2026-08-06": 0.3}


class TestRecoveryAfterCorrection:
    """End-to-end: bad reading never reaches the load forecast; recovers."""

    @pytest.mark.asyncio
    async def test_negative_reading_leaves_sensor_unavailable_not_negative(
        self,
    ) -> None:
        """The bad reading must never surface as the published average."""
        sensor = _avg_sensor(average=1)
        sensor._async_track_entities = AsyncMock()  # type: ignore[method-assign]
        sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]
        with (
            patch(
                "custom_components.hsem.custom_sensors.avg_sensor.dt_util.now",
                return_value=datetime(2026, 8, 8, 15, 5, tzinfo=UTC),
            ),
            patch(
                "custom_components.hsem.custom_sensors.avg_sensor"
                ".ha_get_entity_state_and_convert",
                return_value=-0.205,
            ),
        ):
            await sensor._async_handle_update()
        assert sensor._measurements == {}
        assert sensor.state is None
        assert sensor.available is False

    @pytest.mark.asyncio
    async def test_recovers_and_load_forecast_becomes_ready_on_next_valid_sample(
        self,
    ) -> None:
        """After the source config is fixed, the next completed block
        produces a valid sample and ``assess_load_forecast`` reports
        ``ready=True`` again — ``safety_hold`` clears (issue #938)."""
        sensor = _avg_sensor(average=1)
        sensor._async_track_entities = AsyncMock()  # type: ignore[method-assign]
        sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]

        # Day 1 (misconfigured): negative reading rejected, stays unavailable.
        with (
            patch(
                "custom_components.hsem.custom_sensors.avg_sensor.dt_util.now",
                return_value=datetime(2026, 8, 8, 15, 5, tzinfo=UTC),
            ),
            patch(
                "custom_components.hsem.custom_sensors.avg_sensor"
                ".ha_get_entity_state_and_convert",
                return_value=-0.205,
            ),
        ):
            await sensor._async_handle_update()
        assert sensor.state is None

        # Day 2 (corrected): a valid positive reading replaces it.
        with (
            patch(
                "custom_components.hsem.custom_sensors.avg_sensor.dt_util.now",
                return_value=datetime(2026, 8, 9, 15, 5, tzinfo=UTC),
            ),
            patch(
                "custom_components.hsem.custom_sensors.avg_sensor"
                ".ha_get_entity_state_and_convert",
                return_value=0.35,
            ),
        ):
            await sensor._async_handle_update()
        assert sensor.state == pytest.approx(0.35)
        avg_value = sensor.state
        assert avg_value is not None

        now = datetime(2026, 8, 21, 12, 5, tzinfo=UTC)
        rec = HourlyRecommendation(
            start=now + timedelta(minutes=10),
            end=now + timedelta(minutes=25),
            recommendation=Recommendations.BatteriesWaitMode.value,
            avg_house_consumption_kwh=avg_value,
            avg_house_consumption_1d_kwh=avg_value,
            avg_house_consumption_3d_kwh=avg_value,
            avg_house_consumption_7d_kwh=avg_value,
            avg_house_consumption_14d_kwh=avg_value,
            batteries_charged_kwh=0.0,
            batteries_discharged_kwh=0.0,
            estimated_battery_capacity_kwh=0.0,
            estimated_battery_soc_pct=0.0,
            estimated_cost_currency=0.0,
            estimated_net_consumption_kwh=0.0,
            export_price=0.0,
            grid_export_kwh=0.0,
            grid_import_kwh=0.0,
            import_price=0.0,
            solcast_pv_estimate_kwh=0.0,
        )
        result = assess_load_forecast(
            [rec],
            now,
            population_succeeded=True,
            live_house_demand_w=0.0,
        )
        assert result.ready is True
        assert result.reason is None

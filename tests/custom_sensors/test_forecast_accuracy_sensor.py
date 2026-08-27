"""Restart/restore-correctness tests for HSEMForecastAccuracySensor (issue #832).

Covers:
- Restored sensor state is only trusted when it parses as a finite float
  (replacing the naive STATE_UNAVAILABLE/STATE_UNKNOWN gating).
- Tracker restore failures are caught and do not crash entity setup.
- ``restored_unfinalised_count`` is surfaced via extra_state_attributes.
- ``_forecast_tracker_data`` is built from ``to_persistence_dict`` (bounded
  persistence), not the unbounded ``to_dict``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.hsem.custom_sensors.forecast_accuracy_sensor import (
    HSEMForecastAccuracySensor,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity
from custom_components.hsem.utils.forecast_tracker import ForecastTracker


def _sensor(*, tracker: ForecastTracker | None) -> HSEMForecastAccuracySensor:
    sensor = object.__new__(HSEMForecastAccuracySensor)
    sensor.coordinator = SimpleNamespace(  # type: ignore[assignment]
        _forecast_tracker=tracker,
        data=SimpleNamespace(),
        last_update_success=True,
    )
    sensor._restored_state = None
    return sensor


async def _add_to_hass(sensor: HSEMForecastAccuracySensor, restored: Any) -> None:
    sensor.async_get_last_state = AsyncMock(return_value=restored)  # type: ignore[method-assign]
    with patch.object(HSEMCoordinatorEntity, "async_added_to_hass", new=AsyncMock()):
        await sensor.async_added_to_hass()


class TestRestoredStateValidation:
    """Only finite-float restored states should be trusted (DoD bullet 2)."""

    @pytest.mark.asyncio
    async def test_unavailable_state_is_rejected(self) -> None:
        tracker = ForecastTracker()
        sensor = _sensor(tracker=tracker)
        restored = SimpleNamespace(state="unavailable", attributes={})

        await _add_to_hass(sensor, restored)

        assert sensor._restored_state is None

    @pytest.mark.asyncio
    async def test_non_numeric_garbage_state_is_rejected(self) -> None:
        """A corrupted/garbage restored state must not crash native_value later."""
        tracker = ForecastTracker()
        sensor = _sensor(tracker=tracker)
        restored = SimpleNamespace(state="not-a-number", attributes={})

        await _add_to_hass(sensor, restored)

        assert sensor._restored_state is None

    @pytest.mark.asyncio
    async def test_valid_numeric_state_is_trusted(self) -> None:
        tracker = ForecastTracker()
        sensor = _sensor(tracker=tracker)
        restored = SimpleNamespace(state="1.234", attributes={})

        await _add_to_hass(sensor, restored)

        assert sensor._restored_state == "1.234"


class TestTrackerRestoreFailureHandling:
    """Restore failures must be caught and never propagate (DoD bullet 3)."""

    @pytest.mark.asyncio
    async def test_load_from_dict_exception_does_not_crash_setup(self) -> None:
        tracker = ForecastTracker()

        def _raise(data: object) -> None:
            raise RuntimeError("boom")

        tracker.load_from_dict = _raise  # type: ignore[method-assign]
        sensor = _sensor(tracker=tracker)
        restored = SimpleNamespace(
            state="unknown",
            attributes={"_forecast_tracker_data": {"records": []}},
        )

        with patch(
            "custom_components.hsem.custom_sensors.forecast_accuracy_sensor._LOGGER"
        ) as mock_logger:
            await _add_to_hass(sensor, restored)
            mock_logger.exception.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_tracker_data_is_a_noop(self) -> None:
        tracker = ForecastTracker()
        sensor = _sensor(tracker=tracker)
        restored = SimpleNamespace(state="unknown", attributes={})

        await _add_to_hass(sensor, restored)

        assert tracker.records == []

    @pytest.mark.asyncio
    async def test_no_tracker_on_coordinator_is_a_noop(self) -> None:
        sensor = _sensor(tracker=None)
        restored = SimpleNamespace(
            state="unknown",
            attributes={"_forecast_tracker_data": {"records": []}},
        )

        # Must not raise even though the coordinator has no tracker yet.
        await _add_to_hass(sensor, restored)


class TestRestoredUnfinalisedSurfacing:
    """restored_unfinalised_keys must reach the entity's attributes."""

    @pytest.mark.asyncio
    async def test_restored_unfinalised_count_reflects_tracker(self) -> None:
        start = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
        end = datetime(2026, 8, 20, 10, 15, tzinfo=UTC)
        source = ForecastTracker()
        source.get_or_create_record(start, end)
        source.set_forecasts(start, 4.0, 2.0)
        # Deliberately left unfinalised to simulate an interrupted slot.
        payload = source.to_persistence_dict(now=end, max_records=24)

        tracker = ForecastTracker()
        sensor = _sensor(tracker=tracker)
        restored = SimpleNamespace(
            state="unknown",
            attributes={"_forecast_tracker_data": payload},
        )

        await _add_to_hass(sensor, restored)

        assert tracker.restored_unfinalised_keys == {start}

        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["restored_unfinalised_count"] == 1

    def test_restored_unfinalised_count_is_zero_by_default(self) -> None:
        tracker = ForecastTracker()
        sensor = _sensor(tracker=tracker)

        attrs = sensor.extra_state_attributes

        assert attrs is not None
        assert attrs["restored_unfinalised_count"] == 0


class TestPersistenceDictUsage:
    """extra_state_attributes must serialise via to_persistence_dict."""

    def test_forecast_tracker_data_matches_to_persistence_dict(self) -> None:
        fixed_now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        tracker = ForecastTracker()
        tracker.get_or_create_record(
            datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
            datetime(2026, 8, 20, 10, 15, tzinfo=UTC),
        )
        sensor = _sensor(tracker=tracker)

        with patch(
            "custom_components.hsem.custom_sensors.forecast_accuracy_sensor.hsem_now",
            return_value=fixed_now,
        ):
            attrs = sensor.extra_state_attributes

        assert attrs is not None
        assert attrs["_forecast_tracker_data"] == tracker.to_persistence_dict(
            now=fixed_now, max_records=24
        )

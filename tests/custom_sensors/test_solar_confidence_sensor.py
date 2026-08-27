"""Restart/restore-correctness tests for HSEMSolarConfidenceSensor (issue #832).

Covers:
- Restored sensor state is only trusted when it parses as a finite float.
- ``corrector.load_from_dict`` is called with ``restored_at=hsem_now()``.
- Restore failures are caught and do not crash entity setup.
- ``processed_through`` is surfaced via extra_state_attributes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.custom_sensors.solar_confidence_sensor import (
    HSEMSolarConfidenceSensor,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector


def _sensor(
    *, corrector: SolarForecastCorrector | None, data: Any = None
) -> HSEMSolarConfidenceSensor:
    sensor = object.__new__(HSEMSolarConfidenceSensor)
    sensor.coordinator = SimpleNamespace(  # type: ignore[assignment]
        _solar_corrector=corrector,
        data=data if data is not None else SimpleNamespace(solar_hour_factors={}),
        last_update_success=True,
    )
    sensor._restored_state = None
    return sensor


async def _add_to_hass(sensor: HSEMSolarConfidenceSensor, restored: Any) -> None:
    sensor.async_get_last_state = AsyncMock(return_value=restored)  # type: ignore[method-assign]
    with patch.object(HSEMCoordinatorEntity, "async_added_to_hass", new=AsyncMock()):
        await sensor.async_added_to_hass()


class TestRestoredStateValidation:
    """Only finite-float restored states should be trusted."""

    @pytest.mark.asyncio
    async def test_unavailable_state_is_rejected(self) -> None:
        sensor = _sensor(corrector=SolarForecastCorrector())
        restored = SimpleNamespace(state="unavailable", attributes={})

        await _add_to_hass(sensor, restored)

        assert sensor._restored_state is None

    @pytest.mark.asyncio
    async def test_non_numeric_garbage_state_is_rejected(self) -> None:
        sensor = _sensor(corrector=SolarForecastCorrector())
        restored = SimpleNamespace(state="garbage", attributes={})

        await _add_to_hass(sensor, restored)

        assert sensor._restored_state is None

    @pytest.mark.asyncio
    async def test_valid_numeric_state_is_trusted(self) -> None:
        sensor = _sensor(corrector=SolarForecastCorrector())
        restored = SimpleNamespace(state="0.987", attributes={})

        await _add_to_hass(sensor, restored)

        assert sensor._restored_state == "0.987"


class TestRestoredAtWiring:
    """load_from_dict must receive the app's own clock, not system UTC now."""

    @pytest.mark.asyncio
    async def test_load_from_dict_called_with_hsem_now(self) -> None:
        fixed_now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        corrector = SolarForecastCorrector()
        corrector.load_from_dict = MagicMock()  # type: ignore[method-assign]
        sensor = _sensor(corrector=corrector)
        corrector_data = {"schema_version": 3}
        restored = SimpleNamespace(
            state="unknown",
            attributes={"_solar_corrector_data": corrector_data},
        )

        with patch(
            "custom_components.hsem.custom_sensors.solar_confidence_sensor.hsem_now",
            return_value=fixed_now,
        ):
            await _add_to_hass(sensor, restored)

        corrector.load_from_dict.assert_called_once_with(
            corrector_data, restored_at=fixed_now
        )

    @pytest.mark.asyncio
    async def test_missing_corrector_data_is_a_noop(self) -> None:
        corrector = SolarForecastCorrector()
        corrector.load_from_dict = MagicMock()  # type: ignore[method-assign]
        sensor = _sensor(corrector=corrector)
        restored = SimpleNamespace(state="unknown", attributes={})

        await _add_to_hass(sensor, restored)

        corrector.load_from_dict.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_corrector_on_coordinator_is_a_noop(self) -> None:
        sensor = _sensor(corrector=None)
        restored = SimpleNamespace(
            state="unknown",
            attributes={"_solar_corrector_data": {"schema_version": 3}},
        )

        # Must not raise even though the coordinator has no corrector yet.
        await _add_to_hass(sensor, restored)


class TestRestoreFailureHandling:
    """Restore failures must be caught and never propagate."""

    @pytest.mark.asyncio
    async def test_load_from_dict_exception_does_not_crash_setup(self) -> None:
        corrector = SolarForecastCorrector()

        def _raise(data: object, *, restored_at: object | None = None) -> None:
            raise RuntimeError("boom")

        corrector.load_from_dict = _raise  # type: ignore[method-assign]
        sensor = _sensor(corrector=corrector)
        restored = SimpleNamespace(
            state="unknown",
            attributes={"_solar_corrector_data": {"schema_version": 3}},
        )

        with patch(
            "custom_components.hsem.custom_sensors.solar_confidence_sensor._LOGGER"
        ) as mock_logger:
            await _add_to_hass(sensor, restored)
            mock_logger.exception.assert_called_once()


class TestProcessedThroughSurfacing:
    """corrector.processed_through must reach extra_state_attributes."""

    def test_processed_through_is_none_before_any_processing(self) -> None:
        sensor = _sensor(corrector=SolarForecastCorrector())

        attrs = sensor.extra_state_attributes

        assert attrs is not None
        assert attrs["processed_through"] is None

    def test_processed_through_is_exposed_after_marking(self) -> None:
        slot = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
        corrector = SolarForecastCorrector()
        corrector.mark_processed(slot)
        sensor = _sensor(corrector=corrector)

        attrs = sensor.extra_state_attributes

        assert attrs is not None
        assert attrs["processed_through"] == slot.isoformat()

    def test_processed_through_is_none_without_a_corrector(self) -> None:
        sensor = _sensor(corrector=None)

        attrs = sensor.extra_state_attributes

        assert attrs is not None
        assert attrs["processed_through"] is None

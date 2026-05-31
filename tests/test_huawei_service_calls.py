"""Tests for utils/huawei.py service call behaviour (Tasks 3 & 4).

Acceptance criteria
-------------------
1. When a Huawei service is NOT registered in HA, the function raises
   ``ServiceNotFound`` immediately (instead of silently logging and continuing).
2. Service calls are made with ``blocking=True`` so exceptions raised by the
   service propagate back to the caller (write-and-verify can record them).
3. ``HomeAssistantError`` and ``ServiceValidationError`` raised by the service
   are re-raised so callers observe the failure.
4. Validation errors (``voluptuous.Invalid``) are wrapped in ``HomeAssistantError``
   and re-raised.
5. The three functions covered: ``async_set_grid_export_power_pct``,
   ``async_set_tou_periods``, ``async_set_forcible_discharge``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol

from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

from custom_components.hsem.utils.huawei import (
    async_set_forcible_discharge,
    async_set_grid_export_power_pct,
    async_set_tou_periods,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_sensor(has_service: bool = True, service_exception: Any = None) -> Any:
    """Return a minimal sensor mock whose ``hass`` object mimics HA services.

    Args:
        has_service: Whether ``hass.services.has_service`` returns True.
        service_exception: If set, ``hass.services.async_call`` raises this.
    """
    hass = MagicMock()
    hass.services.has_service.return_value = has_service

    call_mock = AsyncMock()
    if service_exception is not None:
        call_mock.side_effect = service_exception
    hass.services.async_call = call_mock

    sensor = MagicMock()
    sensor.hass = hass
    return sensor


# ---------------------------------------------------------------------------
# async_set_grid_export_power_pct
# ---------------------------------------------------------------------------


class TestSetGridExportPowerPct:
    """Tests for async_set_grid_export_power_pct."""

    @pytest.mark.asyncio
    async def test_missing_service_raises_service_not_found(self):
        """Must raise ServiceNotFound when the service is absent — not silently continue."""
        sensor = _make_sensor(has_service=False)
        with pytest.raises(ServiceNotFound):
            await async_set_grid_export_power_pct(sensor, "device1", 80)

    @pytest.mark.asyncio
    async def test_service_called_with_blocking_true(self):
        """Service call must use blocking=True so exceptions propagate."""
        sensor = _make_sensor(has_service=True)
        await async_set_grid_export_power_pct(sensor, "device1", 80)
        sensor.hass.services.async_call.assert_called_once()
        _, kwargs = sensor.hass.services.async_call.call_args
        assert kwargs.get("blocking") is True, "Expected blocking=True"

    @pytest.mark.asyncio
    async def test_home_assistant_error_is_reraised(self):
        """HomeAssistantError from the service must propagate to the caller."""
        sensor = _make_sensor(
            has_service=True, service_exception=HomeAssistantError("fail")
        )
        with pytest.raises(HomeAssistantError):
            await async_set_grid_export_power_pct(sensor, "device1", 80)

    @pytest.mark.asyncio
    async def test_service_validation_error_is_reraised(self):
        """ServiceValidationError must propagate to the caller."""
        sensor = _make_sensor(
            has_service=True,
            service_exception=ServiceValidationError(
                "bad data", translation_domain="test", translation_key="bad"
            ),
        )
        with pytest.raises(ServiceValidationError):
            await async_set_grid_export_power_pct(sensor, "device1", 80)

    @pytest.mark.asyncio
    async def test_vol_invalid_wrapped_as_ha_error(self):
        """voluptuous.Invalid must be wrapped in HomeAssistantError."""
        sensor = _make_sensor(
            has_service=True, service_exception=vol.Invalid("bad schema")
        )
        with pytest.raises(HomeAssistantError):
            await async_set_grid_export_power_pct(sensor, "device1", -1)

    @pytest.mark.asyncio
    async def test_success_path_does_not_raise(self):
        """Happy-path call completes without raising."""
        sensor = _make_sensor(has_service=True)
        # Should not raise
        await async_set_grid_export_power_pct(sensor, "device1", 100)


# ---------------------------------------------------------------------------
# async_set_tou_periods
# ---------------------------------------------------------------------------


class TestSetTouPeriods:
    """Tests for async_set_tou_periods."""

    @pytest.mark.asyncio
    async def test_missing_service_raises_service_not_found(self):
        """Must raise ServiceNotFound when the service is absent."""
        sensor = _make_sensor(has_service=False)
        with pytest.raises(ServiceNotFound):
            await async_set_tou_periods(sensor, "bat1", ["mode1"])

    @pytest.mark.asyncio
    async def test_service_called_with_blocking_true(self):
        """Service call must use blocking=True."""
        sensor = _make_sensor(has_service=True)
        await async_set_tou_periods(sensor, "bat1", ["mode1", "mode2"])
        sensor.hass.services.async_call.assert_called_once()
        _, kwargs = sensor.hass.services.async_call.call_args
        assert kwargs.get("blocking") is True

    @pytest.mark.asyncio
    async def test_home_assistant_error_is_reraised(self):
        """HomeAssistantError must propagate."""
        sensor = _make_sensor(
            has_service=True, service_exception=HomeAssistantError("tou fail")
        )
        with pytest.raises(HomeAssistantError):
            await async_set_tou_periods(sensor, "bat1", ["mode1"])

    @pytest.mark.asyncio
    async def test_service_validation_error_is_reraised(self):
        """ServiceValidationError must propagate."""
        sensor = _make_sensor(
            has_service=True,
            service_exception=ServiceValidationError(
                "bad", translation_domain="test", translation_key="bad"
            ),
        )
        with pytest.raises(ServiceValidationError):
            await async_set_tou_periods(sensor, "bat1", ["mode1"])

    @pytest.mark.asyncio
    async def test_vol_invalid_wrapped_as_ha_error(self):
        """voluptuous.Invalid must be wrapped in HomeAssistantError."""
        sensor = _make_sensor(
            has_service=True, service_exception=vol.Invalid("bad schema")
        )
        with pytest.raises(HomeAssistantError):
            await async_set_tou_periods(sensor, "bat1", [])

    @pytest.mark.asyncio
    async def test_success_path_does_not_raise(self):
        """Happy-path call completes without raising."""
        sensor = _make_sensor(has_service=True)
        await async_set_tou_periods(sensor, "bat1", ["TOU_MODE_1"])


# ---------------------------------------------------------------------------
# async_set_forcible_discharge
# ---------------------------------------------------------------------------


class TestSetForcibleDischarge:
    """Tests for async_set_forcible_discharge."""

    @pytest.mark.asyncio
    async def test_missing_service_raises_service_not_found(self):
        """Must raise ServiceNotFound when the service is absent."""
        sensor = _make_sensor(has_service=False)
        with pytest.raises(ServiceNotFound):
            await async_set_forcible_discharge(sensor, "bat_dev", 20, 3000)

    @pytest.mark.asyncio
    async def test_service_called_with_blocking_true(self):
        """Service call must use blocking=True."""
        sensor = _make_sensor(has_service=True)
        await async_set_forcible_discharge(sensor, "bat_dev", 20, 3000)
        sensor.hass.services.async_call.assert_called_once()
        _, kwargs = sensor.hass.services.async_call.call_args
        assert kwargs.get("blocking") is True

    @pytest.mark.asyncio
    async def test_home_assistant_error_is_reraised(self):
        """HomeAssistantError must propagate."""
        sensor = _make_sensor(
            has_service=True, service_exception=HomeAssistantError("discharge fail")
        )
        with pytest.raises(HomeAssistantError):
            await async_set_forcible_discharge(sensor, "bat_dev", 20, 3000)

    @pytest.mark.asyncio
    async def test_service_validation_error_is_reraised(self):
        """ServiceValidationError must propagate."""
        sensor = _make_sensor(
            has_service=True,
            service_exception=ServiceValidationError(
                "bad soc", translation_domain="test", translation_key="bad"
            ),
        )
        with pytest.raises(ServiceValidationError):
            await async_set_forcible_discharge(sensor, "bat_dev", 20, 3000)

    @pytest.mark.asyncio
    async def test_vol_invalid_wrapped_as_ha_error(self):
        """voluptuous.Invalid must be wrapped in HomeAssistantError."""
        sensor = _make_sensor(
            has_service=True, service_exception=vol.Invalid("bad schema")
        )
        with pytest.raises(HomeAssistantError):
            await async_set_forcible_discharge(sensor, "bat_dev", 20, 3000)

    @pytest.mark.asyncio
    async def test_invalid_target_soc_raises_value_error(self):
        """Passing a non-integer or out-of-range target_soc must raise ValueError."""
        sensor = _make_sensor(has_service=True)
        with pytest.raises(ValueError):
            await async_set_forcible_discharge(
                sensor,
                "bat_dev",
                150,
                3000,  # > 100  # NOSONAR
            )
        with pytest.raises(ValueError):
            await async_set_forcible_discharge(sensor, "bat_dev", -1, 3000)  # < 0
        with pytest.raises(ValueError):
            await async_set_forcible_discharge(sensor, "bat_dev", 50.5, 3000)  # float

    @pytest.mark.asyncio
    async def test_invalid_power_raises_value_error(self):
        """Negative power must raise ValueError."""
        sensor = _make_sensor(has_service=True)
        with pytest.raises(ValueError):
            await async_set_forcible_discharge(sensor, "bat_dev", 20, -100)

    @pytest.mark.asyncio
    async def test_success_path_does_not_raise(self):
        """Happy-path call completes without raising."""
        sensor = _make_sensor(has_service=True)
        await async_set_forcible_discharge(sensor, "bat_dev", 20, 3000)

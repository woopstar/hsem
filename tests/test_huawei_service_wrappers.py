"""Tests for the Huawei Solar service wrappers used by the applier.

Each wrapper must refuse to pretend a write happened: a missing
``huawei_solar`` service raises before any call, and a rejected call is
logged and re-raised so write-and-verify can record the failure and retry.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

from custom_components.hsem.utils.huawei import (
    async_set_grid_export_power_watt,
    async_stop_forcible_discharge,
)

_MODULE = "custom_components.hsem.utils.huawei"
_DEVICE_ID = "battery_device"


def _caller(*, has_service: bool = True, error: Exception | None = None) -> MagicMock:
    """Return a coordinator stand-in exposing a mock HA service registry."""
    caller = MagicMock()
    caller.hass.services.has_service.return_value = has_service
    caller.hass.services.async_call = AsyncMock(side_effect=error)
    return caller


class TestSetGridExportPowerWatt:
    """The absolute export-power floor is written through ``huawei_solar``."""

    @pytest.mark.asyncio
    async def test_writes_the_absolute_power_limit(self) -> None:
        """The service is called blocking, with the device and power."""
        caller = _caller()

        await async_set_grid_export_power_watt(caller, _DEVICE_ID, 100)

        caller.hass.services.async_call.assert_awaited_once_with(
            "huawei_solar",
            "set_maximum_feed_grid_power",
            {"device_id": _DEVICE_ID, "power": 100},
            blocking=True,
        )

    @pytest.mark.asyncio
    async def test_missing_service_raises_before_calling(self) -> None:
        """Without the integration loaded the write fails loudly."""
        caller = _caller(has_service=False)

        with pytest.raises(ServiceNotFound):
            await async_set_grid_export_power_watt(caller, _DEVICE_ID, 100)

        caller.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_input_is_reported_as_a_ha_error(self) -> None:
        """A schema rejection is surfaced as a Home Assistant error."""
        caller = _caller(error=vol.Invalid("bad power"))

        with (
            patch(f"{_MODULE}._LOGGER") as logger,
            pytest.raises(HomeAssistantError, match="Invalid input data"),
        ):
            await async_set_grid_export_power_watt(caller, _DEVICE_ID, 100)

        logger.exception.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(
                ServiceNotFound("huawei_solar", "set_maximum_feed_grid_power"),
                id="service_not_found",
            ),
            pytest.param(ServiceValidationError("rejected"), id="validation_error"),
            pytest.param(HomeAssistantError("write failed"), id="ha_error"),
        ],
    )
    async def test_write_failures_are_logged_and_reraised(
        self, error: Exception
    ) -> None:
        """A failed write is never swallowed."""
        caller = _caller(error=error)

        with (
            patch(f"{_MODULE}._LOGGER") as logger,
            pytest.raises(type(error)),
        ):
            await async_set_grid_export_power_watt(caller, _DEVICE_ID, 100)

        logger.exception.assert_called_once()


class TestStopForcibleDischarge:
    """Stopping a forced charge or discharge uses one shared service."""

    @pytest.mark.asyncio
    async def test_stops_the_forcible_session(self) -> None:
        """The stop service is called blocking for the given battery."""
        caller = _caller()

        await async_stop_forcible_discharge(caller, _DEVICE_ID)

        caller.hass.services.async_call.assert_awaited_once_with(
            "huawei_solar",
            "stop_forcible_charge",
            {"device_id": _DEVICE_ID},
            blocking=True,
        )

    @pytest.mark.asyncio
    async def test_missing_service_raises_before_calling(self) -> None:
        """Without the integration loaded the stop fails loudly."""
        caller = _caller(has_service=False)

        with pytest.raises(ServiceNotFound):
            await async_stop_forcible_discharge(caller, _DEVICE_ID)

        caller.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(ServiceValidationError("rejected"), id="validation_error"),
            pytest.param(HomeAssistantError("write failed"), id="ha_error"),
        ],
    )
    async def test_failures_are_logged_and_reraised(self, error: Exception) -> None:
        """A failed stop is never reported as success."""
        caller = _caller(error=error)

        with patch(f"{_MODULE}._LOGGER") as logger, pytest.raises(type(error)):
            await async_stop_forcible_discharge(caller, _DEVICE_ID)

        logger.exception.assert_called_once()

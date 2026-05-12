"""This module provides utility functions for interacting with Huawei solar inverters
via Home Assistant services. It includes functions to set the maximum grid export
power percentage and to configure Time-of-Use (TOU) periods for batteries.

Functions:
    async_set_grid_export_power_pct(self, device_id, power_percentage):
        Asynchronously sets the maximum grid export power percentage for a specified device.

    async_set_tou_periods(self, batteries_id, tou_modes):
        Asynchronously sets the Time-of-Use (TOU) periods for specified batteries.

Dependencies:
    - logging: For logging error and success messages.
    - voluptuous as vol: For input validation.
    - homeassistant.core: For Home Assistant core functionalities.
    - homeassistant.exceptions: For Home Assistant specific exceptions.

Usage:
    These functions are designed to be used within a Home Assistant custom component
    to interact with Huawei solar inverters. They handle service calls to the
    "huawei_solar" integration and manage errors appropriately.
"""

import logging

import voluptuous as vol
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

_LOGGER = logging.getLogger(__name__)


async def async_set_grid_export_power_pct(self, device_id, power_percentage) -> None:
    """Set the maximum grid export power percentage and handle errors.

    Raises:
        ServiceNotFound: When the huawei_solar service is not registered in HA.
        HomeAssistantError: On any other HA-level write failure.
    """

    # Raise explicitly so callers (write-and-verify) can record the failure.
    if not self.hass.services.has_service(
        "huawei_solar", "set_maximum_feed_grid_power_percent"
    ):
        raise ServiceNotFound("huawei_solar", "set_maximum_feed_grid_power_percent")

    try:
        # Send the service call to set the maximum grid export power percentage.
        # blocking=True propagates service exceptions back to the caller so that
        # write-and-verify can record the failure and retry.
        await self.hass.services.async_call(
            "huawei_solar",
            "set_maximum_feed_grid_power_percent",
            {
                "device_id": device_id,
                "power_percentage": power_percentage,
            },
            blocking=True,
        )

        # Log success message
        _LOGGER.debug(
            "Updated export power pct to %s for device_id %s",
            power_percentage,
            device_id,
        )

    except vol.Invalid as err:
        # Handle validation errors (e.g., invalid device_id or power_percentage)
        _LOGGER.exception(
            "Invalid input for set_maximum_feed_grid_power_percent "
            "(device_id=%s, power_percentage=%s)",
            device_id,
            power_percentage,
        )
        raise HomeAssistantError(f"Invalid input data: {err}") from err

    except (ServiceNotFound, ServiceValidationError, HomeAssistantError):
        # Service missing or HA rejected the call
        _LOGGER.exception(
            "HA error during set_maximum_feed_grid_power_percent "
            "(device_id=%s, power_percentage=%s)",
            device_id,
            power_percentage,
        )
        raise


async def async_set_tou_periods(self, batteries_id, tou_modes) -> None:
    """Set the TOU modes for the specified batteries.

    Raises:
        ServiceNotFound: When the huawei_solar service is not registered in HA.
        HomeAssistantError: On any other HA-level write failure.
    """

    # Convert the list of TOU modes into the required format
    periods = "\n".join(tou_modes)  # Join TOU modes with newline

    # Raise explicitly so callers (write-and-verify) can record the failure.
    if not self.hass.services.has_service("huawei_solar", "set_tou_periods"):
        raise ServiceNotFound("huawei_solar", "set_tou_periods")

    try:
        # Send the service call to set the TOU periods.
        # blocking=True propagates service exceptions back to the caller so that
        # write-and-verify can record the failure and retry.
        await self.hass.services.async_call(
            "huawei_solar",
            "set_tou_periods",
            {
                "device_id": batteries_id,
                "periods": periods,
            },
            blocking=True,
        )

        # Log success message
        _LOGGER.debug(
            "Set TOU periods for device_id %s with tou_modes %s",
            batteries_id,
            tou_modes,
        )

    except vol.Invalid as err:
        # Handle validation errors (e.g., invalid batteries_id or TOU modes)
        _LOGGER.exception(
            "Invalid input for set_tou_periods (device_id=%s)",
            batteries_id,
        )
        raise HomeAssistantError(f"Invalid input data: {err}") from err

    except (ServiceNotFound, ServiceValidationError, HomeAssistantError):
        # Service missing or HA rejected the call
        _LOGGER.exception(
            "HA error during set_tou_periods (device_id=%s)",
            batteries_id,
        )
        raise


async def async_set_forcible_discharge(
    self, device_id: str, target_soc: int, power: int
) -> None:
    """Set forcible discharge for the battery at specified power and target SOC.

    Args:
        device_id (str): The device ID of the battery (e.g. sensor.luna2000_xxx).
        target_soc (int): The target SOC level to discharge to (0-100).
        power (int): The maximum discharge power in watts.
    """

    # Validate input parameters
    if not isinstance(target_soc, int) or not (0 <= target_soc <= 100):
        raise ValueError(
            f"target_soc must be an integer between 0 and 100, got {target_soc}"
        )

    if not isinstance(power, int) or power < 0:
        raise ValueError(f"power must be a non-negative integer, got {power}")

    # Raise explicitly so callers (write-and-verify) can record the failure.
    if not self.hass.services.has_service("huawei_solar", "set_forcible_discharge"):
        raise ServiceNotFound("huawei_solar", "set_forcible_discharge")

    try:
        # Send the service call to set forcible discharge.
        # blocking=True propagates service exceptions back to the caller so that
        # write-and-verify can record the failure and retry.
        await self.hass.services.async_call(
            "huawei_solar",
            "set_forcible_discharge",
            {
                "device_id": device_id,
                "target_soc": target_soc,
                "power": power,
            },
            blocking=True,
        )

        # Log success message
        _LOGGER.debug(
            "Set forcible discharge for device_id %s to %s%% SOC at %sW",
            device_id,
            target_soc,
            power,
        )

    except vol.Invalid as err:
        # Handle validation errors (e.g., wrong device_id, out-of-range target_soc)
        _LOGGER.exception(
            "Invalid input for set_forcible_discharge "
            "(device_id=%s, target_soc=%s, power=%s)",
            device_id,
            target_soc,
            power,
        )
        raise HomeAssistantError(f"Invalid input data: {err}") from err

    except (ServiceNotFound, ServiceValidationError, HomeAssistantError):
        # Service missing or HA rejected the call — propagate so callers can enter safe mode
        _LOGGER.exception(
            "HA error during set_forcible_discharge "
            "(device_id=%s, target_soc=%s, power=%s)",
            device_id,
            target_soc,
            power,
        )
        raise

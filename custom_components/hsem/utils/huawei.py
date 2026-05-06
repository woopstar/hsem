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
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)


async def async_set_grid_export_power_pct(self, device_id, power_percentage) -> None:
    """Set the maximum grid export power percentage and handle errors."""

    # Check if the service exists
    if not self.hass.services.has_service(
        "huawei_solar", "set_maximum_feed_grid_power_percent"
    ):
        _LOGGER.error(
            "Service huawei_solar.set_maximum_feed_grid_power_percent not found"
        )

    try:
        # Send the service call to set the maximum grid export power percentage
        await self.hass.services.async_call(
            "huawei_solar",  # Integration providing the service
            "set_maximum_feed_grid_power_percent",  # The action to set grid export power
            {
                "device_id": device_id,  # Device ID of the inverter
                "power_percentage": power_percentage,  # The power percentage to set
            },
            blocking=False,  # Non-blocking call to avoid performance issues
        )

        # Log success message
        _LOGGER.debug(
            f"Updated export power pct to: {power_percentage} for device id: {device_id}"
        )

    except vol.MultipleInvalid as err:
        # Handle validation errors (e.g., invalid device_id)
        _LOGGER.error(
            f"Invalid input data: {err}. Please check the device ID or power percentage."
        )
        raise HomeAssistantError(f"Invalid input data: {err}")

    except HomeAssistantError as err:
        # Handle general Home Assistant errors (e.g., service not found)
        _LOGGER.error(f"Home Assistant error while setting grid export power: {err}")
        raise

    except Exception as err:
        # Handle any other unexpected errors
        _LOGGER.error(f"An unexpected error occurred: {err}")
        raise HomeAssistantError(f"Unexpected error: {err}")


async def async_set_tou_periods(self, batteries_id, tou_modes) -> None:
    """Set the TOU modes for the specified batteries."""

    # Convert the list of TOU modes into the required format
    periods = "\n".join(tou_modes)  # Join TOU modes with newline

    # Check if the service exists
    if not self.hass.services.has_service("huawei_solar", "set_tou_periods"):
        _LOGGER.error("Service huawei_solar.set_tou_periods not found")
        return  # Exit early if service is not found

    try:
        # Send the service call to set the TOU periods
        await self.hass.services.async_call(
            "huawei_solar",
            "set_tou_periods",
            {
                "device_id": batteries_id,  # Device ID of the inverter
                "periods": periods,  # TOU modes formatted as a string
            },
            blocking=False,  # Non-blocking call to avoid performance issues
        )

        # Log success message
        _LOGGER.debug(
            f"Set TOU periods for device id: {batteries_id} with tou modes: {tou_modes}"
        )

    except vol.MultipleInvalid as err:
        # Handle validation errors (e.g., invalid batteries_id)
        _LOGGER.error(
            f"Invalid input data: {err}. Please check the device ID or TOU modes."
        )
        raise HomeAssistantError(f"Invalid input data: {err}")

    except HomeAssistantError as err:
        # Handle general Home Assistant errors (e.g., service not found)
        _LOGGER.error(f"Home Assistant error while setting TOU periods: {err}")
        raise

    except Exception as err:
        # Handle any other unexpected errors
        _LOGGER.error(f"An unexpected error occurred: {err}")
        raise HomeAssistantError(f"Unexpected error: {err}")


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

    # Check if the service exists
    if not self.hass.services.has_service("huawei_solar", "set_forcible_discharge"):
        _LOGGER.error("Service huawei_solar.set_forcible_discharge not found")
        return

    try:
        # Send the service call to set forcible discharge
        await self.hass.services.async_call(
            "huawei_solar",
            "set_forcible_discharge",
            {
                "device_id": device_id,  # Device ID of the battery
                "target_soc": target_soc,  # Target SOC in percentage (0-100)
                "power": power,  # Maximum discharge power in watts
            },
            blocking=False,  # Non-blocking call to avoid performance issues
        )

        # Log success message
        _LOGGER.debug(
            f"Set forcible discharge for device {device_id} to {target_soc}% SOC at {power}W"
        )

    except vol.MultipleInvalid as err:
        # Handle validation errors
        _LOGGER.error(
            f"Invalid input data for forcible discharge: {err}. Check device_id, target_soc, or power."
        )
        raise HomeAssistantError(f"Invalid input data: {err}")

    except HomeAssistantError as err:
        # Handle general Home Assistant errors
        _LOGGER.error(f"Home Assistant error while setting forcible discharge: {err}")
        raise

    except Exception as err:
        # Handle any other unexpected errors
        _LOGGER.error(f"An unexpected error occurred during forcible discharge: {err}")
        raise HomeAssistantError(f"Unexpected error: {err}")

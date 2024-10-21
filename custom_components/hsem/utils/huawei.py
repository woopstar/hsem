"""
This module provides utility functions for interacting with Huawei solar inverters
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
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)


async def async_set_grid_export_power_pct(self, device_id, power_percentage):
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
        _LOGGER.warning(
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


async def async_set_tou_periods(self, batteries_id, tou_modes):
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
            blocking=True,  # Non-blocking call to avoid performance issues
        )

        # Log success message
        _LOGGER.warning(
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

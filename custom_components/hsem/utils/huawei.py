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
            blocking=False,  # Non-blocking call to avoid performance issues
        )

        # Log success message
        _LOGGER.debug(
            f"Set TOU periods for device id: {batteries_id} with periods: {periods}"
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

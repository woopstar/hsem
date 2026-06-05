"""Utility functions for interacting with Huawei solar inverters via Home Assistant services.

Includes functions to set the maximum grid export power percentage and
to configure Time-of-Use (TOU) periods for batteries.
"""

from typing import Any

import voluptuous as vol

from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER


async def async_set_grid_export_power_pct(
    self: Any, device_id: str, power_percentage: int
) -> None:
    """Set the maximum grid export power percentage for a Huawei inverter.

    Args:
        self: The calling coordinator or component instance.
        device_id: The device ID of the inverter.
        power_percentage: The export power limit as a percentage (0-100).

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

    except ServiceNotFound, ServiceValidationError, HomeAssistantError:
        # Service missing or HA rejected the call
        _LOGGER.exception(
            "HA error during set_maximum_feed_grid_power_percent "
            "(device_id=%s, power_percentage=%s)",
            device_id,
            power_percentage,
        )
        raise


async def async_set_grid_export_power_watt(
    self: Any, device_id: str, power_watt: int
) -> None:
    """Set the maximum grid export power in watts and handle errors.

    Uses the ``huawei_solar.set_maximum_feed_grid_power`` service to set an
    absolute power limit in watts (instead of a percentage).  This is used
    as a soft floor (e.g. 100 W) when the system wants to block exports but
    the inverter does not handle a 0 % percentage limit well.

    Raises:
        ServiceNotFound: When the huawei_solar service is not registered in HA.
        HomeAssistantError: On any other HA-level write failure.
    """

    # Raise explicitly so callers (write-and-verify) can record the failure.
    if not self.hass.services.has_service(
        "huawei_solar", "set_maximum_feed_grid_power"
    ):
        raise ServiceNotFound("huawei_solar", "set_maximum_feed_grid_power")

    try:
        # Send the service call to set the maximum grid export power in watts.
        # blocking=True propagates service exceptions back to the caller so that
        # write-and-verify can record the failure and retry.
        await self.hass.services.async_call(
            "huawei_solar",
            "set_maximum_feed_grid_power",
            {
                "device_id": device_id,
                "power": power_watt,
            },
            blocking=True,
        )

        # Log success message
        _LOGGER.debug(
            "Updated export power to %s W for device_id %s",
            power_watt,
            device_id,
        )

    except vol.Invalid as err:
        # Handle validation errors (e.g., invalid device_id or power_watt)
        _LOGGER.exception(
            "Invalid input for set_maximum_feed_grid_power "
            "(device_id=%s, power_watt=%s)",
            device_id,
            power_watt,
        )
        raise HomeAssistantError(f"Invalid input data: {err}") from err

    except ServiceNotFound, ServiceValidationError, HomeAssistantError:
        # Service missing or HA rejected the call
        _LOGGER.exception(
            "HA error during set_maximum_feed_grid_power (device_id=%s, power_watt=%s)",
            device_id,
            power_watt,
        )
        raise


async def async_set_tou_periods(
    self: Any, batteries_id: str, tou_modes: list[str]
) -> None:
    """Set the Time-of-Use periods for the specified batteries.

    Args:
        self: The calling coordinator or component instance.
        batteries_id: The device ID of the batteries.
        tou_modes: A list of TOU mode strings to apply.

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

    except ServiceNotFound, ServiceValidationError, HomeAssistantError:
        # Service missing or HA rejected the call
        _LOGGER.exception(
            "HA error during set_tou_periods (device_id=%s)",
            batteries_id,
        )
        raise


async def async_set_forcible_discharge(
    self: Any, device_id: str, target_soc: int, power: int
) -> None:
    """Set forcible discharge for the battery at specified power and target SOC.

    Args:
        self: The calling coordinator or component instance.
        device_id: The device ID of the battery (e.g. ``sensor.luna2000_xxx``).
        target_soc: The target SOC level to discharge to (0-100).
        power: The maximum discharge power in watts.

    Raises:
        ValueError: If target_soc or power are out of valid range.
        ServiceNotFound: When the huawei_solar service is not registered in HA.
        HomeAssistantError: On any other HA-level write failure.
    """

    # Validate input parameters
    if not isinstance(target_soc, int) or not (0 <= target_soc <= 100):
        raise ValueError(
            f"target_soc must be an integer between 0 and 100, got {target_soc}"
        )

    if not isinstance(power, int) or power < 0:
        raise ValueError(f"power must be a non-negative integer, got {power}")

    # Raise explicitly so callers (write-and-verify) can record the failure.
    if not self.hass.services.has_service("huawei_solar", "forcible_discharge_soc"):
        raise ServiceNotFound("huawei_solar", "forcible_discharge_soc")

    try:
        # Send the service call to set forcible discharge.
        # blocking=True propagates service exceptions back to the caller so that
        # write-and-verify can record the failure and retry.
        await self.hass.services.async_call(
            "huawei_solar",
            "forcible_discharge_soc",
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
            "Invalid input for forcible_discharge_soc "
            "(device_id=%s, target_soc=%s, power=%s)",
            device_id,
            target_soc,
            power,
        )
        raise HomeAssistantError(f"Invalid input data: {err}") from err

    except ServiceNotFound, ServiceValidationError, HomeAssistantError:
        # Service missing or HA rejected the call — propagate so callers can enter safe mode
        _LOGGER.exception(
            "HA error during forcible_discharge_soc "
            "(device_id=%s, target_soc=%s, power=%s)",
            device_id,
            target_soc,
            power,
        )
        raise


async def async_stop_forcible_discharge(self: Any, device_id: str) -> None:
    """Stop any active forcible charge or discharge on the battery.

    Args:
        self: The calling coordinator or component instance.
        device_id: The device ID of the battery.

    Raises:
        ServiceNotFound: When the huawei_solar service is not registered in HA.
        HomeAssistantError: On any other HA-level write failure.
    """
    if not self.hass.services.has_service("huawei_solar", "stop_forcible_charge"):
        raise ServiceNotFound("huawei_solar", "stop_forcible_charge")

    try:
        await self.hass.services.async_call(
            "huawei_solar",
            "stop_forcible_charge",
            {"device_id": device_id},
            blocking=True,
        )
        _LOGGER.debug("Stopped forcible charge/discharge for device_id %s", device_id)
    except ServiceNotFound, ServiceValidationError, HomeAssistantError:
        _LOGGER.exception(
            "HA error during stop_forcible_charge (device_id=%s)", device_id
        )
        raise

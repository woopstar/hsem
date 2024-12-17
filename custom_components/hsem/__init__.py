"""
This module initializes the custom component for Home Assistant.
"""

import logging
from importlib.metadata import PackageNotFoundError, version

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from custom_components.hsem.const import DOMAIN, MIN_HUAWEI_SOLAR_VERSION

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.TIME]


async def check_huawei_solar_version(hass: HomeAssistant) -> bool:
    """Check the version of the Huawei Solar integration asynchronously."""

    def _get_version():
        try:
            return version("huawei_solar")
        except PackageNotFoundError:
            return None

    installed_version = await hass.async_add_executor_job(_get_version)

    if installed_version is None:
        _LOGGER.error("Huawei Solar integration is not installed.")
        return False

    if installed_version < MIN_HUAWEI_SOLAR_VERSION:
        _LOGGER.error(
            f"Huawei Solar version {installed_version} is installed, "
            f"but version {MIN_HUAWEI_SOLAR_VERSION} or higher is required."
        )
        return False

    return True


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the HSEM integration."""
    if not await check_huawei_solar_version(hass):
        _LOGGER.error(
            "Failed to set up HSEM due to missing or incompatible Huawei Solar version."
        )
        return False

    _LOGGER.info("HSEM integration successfully initialized.")
    hass.data.setdefault(DOMAIN, {})

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await check_huawei_solar_version(hass):
        return False

    hass.data[DOMAIN][entry.entry_id] = {}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info("HSEM integration successfully set up.")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info(f"Unloading HSEM integration for {entry.entry_id}")

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok

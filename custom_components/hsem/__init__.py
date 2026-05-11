"""This module initializes the custom component for Home Assistant."""

import inspect
import logging
from importlib.metadata import PackageNotFoundError, version

from packaging.version import InvalidVersion, Version

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from custom_components.hsem.const import DOMAIN, MIN_HUAWEI_SOLAR_VERSION

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.TIME, Platform.SELECT]


def _parse_version(version_str: str) -> Version | None:
    """Parse a version string into a packaging Version object.

    Args:
        version_str: The version string to parse (e.g. "1.10.0", "1.5.0a1").

    Returns:
        A ``packaging.version.Version`` instance, or ``None`` if the string is
        not a valid PEP 440 version.
    """
    try:
        return Version(version_str)
    except InvalidVersion:
        _LOGGER.warning("Invalid version string encountered: '%s'", version_str)
        return None


async def check_huawei_solar_version(hass: HomeAssistant) -> bool:
    """Check the version of the Huawei Solar integration asynchronously."""

    def _get_version():
        try:
            return version("huawei_solar")
        except PackageNotFoundError:
            return None

    installed_version_str = await hass.async_add_executor_job(_get_version)

    if installed_version_str is None:
        _LOGGER.error("Huawei Solar integration is not installed.")
        return False

    installed_version = _parse_version(installed_version_str)
    required_version = _parse_version(MIN_HUAWEI_SOLAR_VERSION)

    if installed_version is None:
        _LOGGER.error(
            "Could not parse installed Huawei Solar version: '%s'.",
            installed_version_str,
        )
        return False

    if required_version is None:
        _LOGGER.error(
            "Could not parse required Huawei Solar version constant: '%s'.",
            MIN_HUAWEI_SOLAR_VERSION,
        )
        return False

    if installed_version < required_version:
        _LOGGER.error(
            "Huawei Solar version %s is installed, "
            "but version %s or higher is required.",
            installed_version_str,
            MIN_HUAWEI_SOLAR_VERSION,
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

    # Add update listner for options
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    _LOGGER.info("HSEM integration successfully set up.")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info(f"Unloading HSEM integration for {entry.entry_id}")

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.debug("Options update triggered for HSEM: %s", entry.entry_id)

    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not isinstance(domain_data, dict):
        return

    for obj in domain_data.values():
        method = getattr(obj, "async_options_updated", None)
        if not callable(method):
            continue

        result = method(entry)  # may be None, sync, or coroutine
        if inspect.isawaitable(result):
            await result

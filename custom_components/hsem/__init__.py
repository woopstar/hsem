"""This module initializes the custom component for Home Assistant."""

from __future__ import annotations

import inspect
import logging
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from packaging.version import InvalidVersion, Version

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from custom_components.hsem.const import DOMAIN, MIN_HUAWEI_SOLAR_VERSION
from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.services import (
    async_register_services,
    async_unregister_services,
)
from custom_components.hsem.utils.logger import (
    async_close_hsem_logger,
    async_init_hsem_logger,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]


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
        _LOGGER.error("Huawei Solar integration is not installed")
        return False

    installed_version = _parse_version(installed_version_str)
    required_version = _parse_version(MIN_HUAWEI_SOLAR_VERSION)

    if installed_version is None:
        _LOGGER.error(
            "Could not parse installed Huawei Solar version: '%s'",
            installed_version_str,
        )
        return False

    if required_version is None:
        _LOGGER.error(
            "Could not parse required Huawei Solar version constant: '%s'",
            MIN_HUAWEI_SOLAR_VERSION,
        )
        return False

    if installed_version < required_version:
        _LOGGER.error(
            "Huawei Solar version %s is installed, "
            "but version %s or higher is required",
            installed_version_str,
            MIN_HUAWEI_SOLAR_VERSION,
        )
        return False

    return True


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Set up the HSEM integration.

    The ``_config`` parameter is required by the Home Assistant component-setup
    protocol (passed when HA loads YAML configuration) but is not used because
    HSEM is a config-entry-only integration.  The leading underscore signals
    this is intentionally unused.
    """
    if not await check_huawei_solar_version(hass):
        _LOGGER.error(
            "Failed to set up HSEM due to missing or incompatible Huawei Solar version"
        )
        return False

    _LOGGER.debug("HSEM integration successfully initialized")
    hass.data.setdefault(DOMAIN, {})

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the HSEM integration from a config entry.

    Creates the shared :class:`HSEMDataUpdateCoordinator`, runs the first
    update cycle, forwards platform setups, registers services, and adds
    an options update listener.
    """
    if not await check_huawei_solar_version(hass):
        return False

    # Initialise the HSEM dedicated log file (hsem.log in the config dir).
    await async_init_hsem_logger(hass)

    # Create the shared DataUpdateCoordinator and run the first update cycle.
    coordinator = HSEMDataUpdateCoordinator(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator}
    await coordinator.async_setup()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register HSEM services (force_recalculation, set_temporary_override, etc.).
    await async_register_services(hass)

    # Add update listener for options.
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    _LOGGER.debug("HSEM integration successfully set up")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading HSEM integration for %s", entry.entry_id)

    # Tear down the coordinator's timers before unloading platforms.
    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator: HSEMDataUpdateCoordinator | None = domain_data.get("coordinator")
    if coordinator is not None:
        await coordinator.async_teardown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    # Unregister HSEM services when the last entry is removed.
    remaining = hass.data.get(DOMAIN, {})
    if not remaining:
        await async_unregister_services(hass)

    # Close the HSEM dedicated log file handler.
    await async_close_hsem_logger()

    return unload_ok


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.debug("Options update triggered for HSEM: %s", entry.entry_id)

    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not isinstance(domain_data, dict):
        return

    # Notify the coordinator so it re-reads config and re-runs the pipeline.
    coordinator: HSEMDataUpdateCoordinator | None = domain_data.get("coordinator")
    if coordinator is not None:
        await coordinator.async_options_updated()
        return

    # Fallback: notify any legacy objects that expose async_options_updated.
    for obj in domain_data.values():
        method = getattr(obj, "async_options_updated", None)
        if not callable(method):
            continue

        result = method(entry)  # May be None, sync, or coroutine.
        if inspect.isawaitable(result):
            await result

"""This module initializes the custom component for Home Assistant."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from packaging.version import InvalidVersion, Version

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)

from custom_components.hsem.const import DOMAIN, MIN_HUAWEI_SOLAR_VERSION
from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.services import async_register_services
from custom_components.hsem.utils.logger import (
    async_close_hsem_logger,
    async_init_hsem_logger,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class HSEMRuntimeData:
    """Runtime data stored on the config entry."""

    coordinator: HSEMDataUpdateCoordinator


type HSEMConfigEntry = ConfigEntry[HSEMRuntimeData]

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


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries to the current version.

    Home Assistant 2025.x+ calls this function on the component module
    (__init__.py) instead of the ConfigFlow handler.  We delegate to the
    existing migration logic in :class:`HSEMConfigFlow`.
    """
    from custom_components.hsem.config_flow import HSEMConfigFlow

    flow = HSEMConfigFlow()
    result = await flow.async_migrate_entry(hass, entry)

    # Ensure the entry's minor_version matches the handler as well.
    target_minor = getattr(flow, "MINOR_VERSION", 1)
    if entry.minor_version != target_minor:
        hass.config_entries.async_update_entry(entry, minor_version=target_minor)

    return result


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

    # Register services in async_setup so they are available even when no
    # config entry is loaded (Bronze rule: action-setup).
    await async_register_services(hass)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: HSEMConfigEntry) -> bool:
    """Set up the HSEM integration from a config entry.

    Creates the shared :class:`HSEMDataUpdateCoordinator`, runs the first
    update cycle, forwards platform setups, and adds an options update
    listener.  Services are registered once in ``async_setup``.
    """
    if not await check_huawei_solar_version(hass):
        raise ConfigEntryError(
            "Huawei Solar integration is not installed or version is too old"
        )

    # Initialise the HSEM dedicated log file (hsem.log in the config dir).
    await async_init_hsem_logger(hass)

    # Create the shared DataUpdateCoordinator and run the first update cycle.
    coordinator = HSEMDataUpdateCoordinator(hass, entry)

    try:
        await coordinator.async_setup()
    except ConfigEntryNotReady:
        raise
    except ConfigEntryAuthFailed:
        raise
    except ConfigEntryError:
        raise
    except (TimeoutError, ConnectionError, OSError) as exc:
        raise ConfigEntryNotReady(
            f"HSEM could not connect during initial setup: {exc}"
        ) from exc
    except Exception as exc:
        raise ConfigEntryError(f"Unexpected error during HSEM setup: {exc}") from exc

    entry.runtime_data = HSEMRuntimeData(coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Add update listener for options.
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    _LOGGER.debug("HSEM integration successfully set up")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HSEMConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading HSEM integration for %s", entry.entry_id)

    # Tear down the coordinator's timers before unloading platforms.
    coordinator: HSEMDataUpdateCoordinator | None = (
        entry.runtime_data.coordinator if entry.runtime_data else None
    )
    if coordinator is not None:
        await coordinator.async_teardown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry.runtime_data = None  # type: ignore[assignment]  # HA convention: clear on unload

    # Close the HSEM dedicated log file handler.
    await async_close_hsem_logger()

    return unload_ok


async def async_update_options(hass: HomeAssistant, entry: HSEMConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.debug("Options update triggered for HSEM: %s", entry.entry_id)

    if entry.runtime_data is None:
        return

    # Notify the coordinator so it re-reads config and re-runs the pipeline.
    await entry.runtime_data.coordinator.async_options_updated()

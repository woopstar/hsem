"""
This module initializes the custom component for Home Assistant.

Functions:
    async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        Set up the custom component from a config entry.

    async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        Unload the custom component from a config entry.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform

PLATFORMS = [Platform.SENSOR]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

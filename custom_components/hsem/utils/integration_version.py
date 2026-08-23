"""Async Home Assistant manifest version authority for HSEM."""

from __future__ import annotations

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.loader import IntegrationNotFound, async_get_integration

from custom_components.hsem.const import DOMAIN


async def async_get_hsem_integration_version(hass: HomeAssistant) -> str:
    """Return HSEM's loaded manifest version without event-loop file I/O."""
    try:
        integration = await async_get_integration(hass, DOMAIN)
        version = integration.version
        if version is None:
            return STATE_UNKNOWN
        normalized = str(version).strip()
    except IntegrationNotFound, AttributeError, TypeError, ValueError:
        return STATE_UNKNOWN
    return normalized or STATE_UNKNOWN

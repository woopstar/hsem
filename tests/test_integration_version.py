"""Tests for async manifest-derived HSEM version lookup."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.loader import IntegrationNotFound

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.utils import integration_version as version_module


@pytest.mark.asyncio
async def test_version_comes_from_home_assistant_integration_manifest() -> None:
    hass = MagicMock(spec=HomeAssistant)
    loader = AsyncMock(return_value=SimpleNamespace(version="7.3.1"))

    with patch.object(version_module, "async_get_integration", loader):
        version = await version_module.async_get_hsem_integration_version(hass)

    assert version == "7.3.1"
    loader.assert_awaited_once_with(hass, DOMAIN)


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_version", [None, "", "   "])
async def test_missing_manifest_version_fails_safe(
    manifest_version: str | None,
) -> None:
    hass = MagicMock(spec=HomeAssistant)
    loader = AsyncMock(return_value=SimpleNamespace(version=manifest_version))

    with patch.object(version_module, "async_get_integration", loader):
        version = await version_module.async_get_hsem_integration_version(hass)

    assert version == STATE_UNKNOWN


@pytest.mark.asyncio
async def test_missing_integration_fails_safe() -> None:
    hass = MagicMock(spec=HomeAssistant)
    loader = AsyncMock(side_effect=IntegrationNotFound(DOMAIN))

    with patch.object(version_module, "async_get_integration", loader):
        version = await version_module.async_get_hsem_integration_version(hass)

    assert version == STATE_UNKNOWN

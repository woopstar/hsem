"""Tests for HSEM's config-entry unload lifecycle.

Covers the real bug from issue #891: ``async_unload_entry`` tore down the
coordinator and platforms but never called ``async_unregister_services``,
leaving HSEM's services registered in ``hass.services`` after the
integration was unloaded or reloaded.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hsem import HSEMRuntimeData, async_unload_entry
from custom_components.hsem.const import DOMAIN
from custom_components.hsem.services import SERVICE_HANDLER_MAP, async_register_services


class _FakeServiceRegistry:
    """Minimal stateful stand-in for HomeAssistant's real service registry.

    Tracks registered (domain, service) pairs so the test can assert on the
    actual post-unload state instead of just call-args on a mock.
    """

    def __init__(self) -> None:
        self._services: dict[str, set[str]] = {}

    def has_service(self, domain: str, service: str) -> bool:
        return service in self._services.get(domain, set())

    def async_register(
        self,
        domain: str,
        service: str,
        service_func: Any,
        schema: Any = None,
        supports_response: Any = None,
    ) -> None:
        assert callable(service_func)
        self._services.setdefault(domain, set()).add(service)

    def async_remove(self, domain: str, service: str) -> None:
        self._services.get(domain, set()).discard(service)


@pytest.fixture
def mock_hass() -> MagicMock:
    """Return a mocked HomeAssistant with a stateful fake service registry."""
    hass = MagicMock()
    hass.services = _FakeServiceRegistry()
    hass.config_entries = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    return hass


@pytest.mark.asyncio
async def test_async_unload_entry_removes_hsem_services(
    mock_hass: MagicMock,
) -> None:
    """async_unload_entry must unregister every HSEM service (issue #891)."""
    await async_register_services(mock_hass)
    assert all(
        mock_hass.services.has_service(DOMAIN, name) for name in SERVICE_HANDLER_MAP
    )

    mock_coordinator = MagicMock()
    mock_coordinator.async_teardown = AsyncMock()

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.runtime_data = HSEMRuntimeData(coordinator=mock_coordinator)

    result = await async_unload_entry(mock_hass, entry)

    assert result is True
    mock_coordinator.async_teardown.assert_awaited_once()
    assert not any(
        mock_hass.services.has_service(DOMAIN, name) for name in SERVICE_HANDLER_MAP
    )


@pytest.mark.asyncio
async def test_async_unload_entry_skips_service_removal_when_platforms_fail(
    mock_hass: MagicMock,
) -> None:
    """Services are still unregistered even if platform unload fails.

    ``async_unregister_services`` runs unconditionally after the platform
    unload attempt, matching the real HA teardown ordering used elsewhere
    in ``async_unload_entry``.
    """
    mock_hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
    await async_register_services(mock_hass)

    mock_coordinator = MagicMock()
    mock_coordinator.async_teardown = AsyncMock()

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.runtime_data = HSEMRuntimeData(coordinator=mock_coordinator)

    result = await async_unload_entry(mock_hass, entry)

    assert result is False
    assert not any(
        mock_hass.services.has_service(DOMAIN, name) for name in SERVICE_HANDLER_MAP
    )

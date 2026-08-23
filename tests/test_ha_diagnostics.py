"""Tests for the Home Assistant HSEM diagnostics hook."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.hsem import diagnostics as diagnostics_module


@pytest.mark.asyncio
async def test_diagnostics_uses_async_manifest_version() -> None:
    hass = MagicMock(spec=HomeAssistant)
    coordinator = MagicMock()
    coordinator._last_planner_input = MagicMock()
    coordinator._last_planner_output = MagicMock()
    coordinator.data = SimpleNamespace(apply_summary=None)
    entry = MagicMock()
    entry.entry_id = "entry-id"
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    version_lookup = AsyncMock(return_value="7.3.1")

    with (
        patch.object(
            diagnostics_module,
            "async_get_hsem_integration_version",
            version_lookup,
        ),
        patch.object(
            diagnostics_module,
            "build_diagnostics_dump",
            return_value={},
        ) as build_dump,
    ):
        await diagnostics_module.async_get_config_entry_diagnostics(
            hass,
            entry,
        )

    version_lookup.assert_awaited_once_with(hass)
    assert build_dump.call_args.kwargs["integration_version"] == "7.3.1"


@pytest.mark.asyncio
async def test_diagnostics_reports_missing_coordinator() -> None:
    hass = MagicMock(spec=HomeAssistant)
    entry = MagicMock()
    entry.entry_id = "entry-id"
    entry.runtime_data = None

    result = await diagnostics_module.async_get_config_entry_diagnostics(hass, entry)

    assert result == {"error": "coordinator_not_found", "entry_id": "entry-id"}


@pytest.mark.asyncio
async def test_diagnostics_reports_missing_planner_cycle() -> None:
    hass = MagicMock(spec=HomeAssistant)
    coordinator = MagicMock()
    coordinator._last_planner_input = None
    coordinator._last_planner_output = None
    coordinator.data = None
    entry = MagicMock()
    entry.entry_id = "entry-id"
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    result = await diagnostics_module.async_get_config_entry_diagnostics(hass, entry)

    assert result == {"error": "no_planner_cycle_completed", "entry_id": "entry-id"}

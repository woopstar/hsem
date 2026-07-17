"""Regression tests for the v1->v2 entity ``unique_id`` migration.

v6.0.0 (#523) prefixed every entity ``unique_id`` with the config entry id but
shipped no entity-registry migration, so existing v5 entities were orphaned and
re-created with a ``_2`` suffix -- losing their ``entity_id`` and long-term
statistics. ``async_migrate_entry`` must hand a remap callback to
``entity_registry.async_migrate_entries`` that renames the ids in place.

These tests use plain mocks (matching the rest of the suite) and exercise the
remap callback directly, so they need no running Home Assistant.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.config_flow import HSEMConfigFlow

ENTRY_ID = "01JHBRS16N1VQM58YSEB88AC90"


def _make_entry() -> MagicMock:
    entry = MagicMock()
    entry.version = 1
    entry.entry_id = ENTRY_ID
    entry.data = {}
    return entry


@pytest.mark.asyncio
async def test_migrate_v1_to_v2_registers_unique_id_remap() -> None:
    """v1->v2 migrates config data AND rewrites entity unique_ids in place."""
    hass = MagicMock()
    entry = _make_entry()

    with patch(
        "custom_components.hsem.config_flow.er.async_migrate_entries",
        new=AsyncMock(),
    ) as migrate_entries:
        result = await HSEMConfigFlow().async_migrate_entry(hass, entry)

    assert result is True

    # Config data was migrated to version 2.
    hass.config_entries.async_update_entry.assert_called_once()
    assert hass.config_entries.async_update_entry.call_args.kwargs["version"] == 2

    # The entity-registry remap was invoked for this entry.
    migrate_entries.assert_awaited_once()
    assert migrate_entries.await_args is not None
    hass_arg, entry_id_arg, update_func = migrate_entries.await_args.args
    assert hass_arg is hass
    assert entry_id_arg == ENTRY_ID

    # Old (unprefixed) id -> prefixed in place (entity_id/history preserved).
    old = SimpleNamespace(unique_id="hsem_workingmode_sensor")
    assert update_func(old) == {"new_unique_id": f"hsem_{ENTRY_ID}_workingmode_sensor"}


@pytest.mark.asyncio
async def test_remap_callback_is_idempotent_and_scoped() -> None:
    """Already-prefixed and foreign unique_ids are left untouched."""
    hass = MagicMock()
    entry = _make_entry()

    with patch(
        "custom_components.hsem.config_flow.er.async_migrate_entries",
        new=AsyncMock(),
    ) as migrate_entries:
        await HSEMConfigFlow().async_migrate_entry(hass, entry)

    assert migrate_entries.await_args is not None
    update_func = migrate_entries.await_args.args[2]

    already = SimpleNamespace(unique_id=f"hsem_{ENTRY_ID}_workingmode_sensor")
    assert update_func(already) is None  # no double prefix

    foreign = SimpleNamespace(unique_id="other_integration_sensor")
    assert update_func(foreign) is None  # only hsem_ ids are touched

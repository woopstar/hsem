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

from custom_components.hsem.config_flow import (
    _CHARGE_RATE_BUCKETS,
    _V3_DEPRECATED_KEYS,
    _V4_DEPRECATED_KEYS,
    HSEMConfigFlow,
)

ENTRY_ID = "01JHBRS16N1VQM58YSEB88AC90"


def _make_entry(
    *,
    version: int = 1,
    data: dict | None = None,
    options: dict | None = None,
) -> MagicMock:
    entry = MagicMock()
    entry.version = version
    entry.entry_id = ENTRY_ID
    entry.data = data or {}
    entry.options = options or {}
    return entry


@pytest.mark.asyncio
async def test_migrate_v1_to_v2_registers_unique_id_remap() -> None:
    """v1->v2 migrates config data AND rewrites entity unique_ids in place."""
    hass = MagicMock()
    entry = _make_entry()

    with (
        patch(
            "custom_components.hsem.config_flow.er.async_migrate_entries",
            new=AsyncMock(),
        ) as migrate_entries,
        patch(
            "custom_components.hsem.config_flow.er.async_get", return_value=MagicMock()
        ),
        patch(
            "custom_components.hsem.config_flow.er.async_entries_for_config_entry",
            return_value=[],
        ),
    ):
        result = await HSEMConfigFlow().async_migrate_entry(hass, entry)

    assert result is True

    # Config data was migrated all the way through to the current version.
    hass.config_entries.async_update_entry.assert_called_once()
    assert hass.config_entries.async_update_entry.call_args.kwargs["version"] == 4

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

    with (
        patch(
            "custom_components.hsem.config_flow.er.async_migrate_entries",
            new=AsyncMock(),
        ) as migrate_entries,
        patch(
            "custom_components.hsem.config_flow.er.async_get", return_value=MagicMock()
        ),
        patch(
            "custom_components.hsem.config_flow.er.async_entries_for_config_entry",
            return_value=[],
        ),
    ):
        await HSEMConfigFlow().async_migrate_entry(hass, entry)

    assert migrate_entries.await_args is not None
    update_func = migrate_entries.await_args.args[2]

    already = SimpleNamespace(unique_id=f"hsem_{ENTRY_ID}_workingmode_sensor")
    assert update_func(already) is None  # no double prefix

    foreign = SimpleNamespace(unique_id="other_integration_sensor")
    assert update_func(foreign) is None  # only hsem_ ids are touched


@pytest.mark.asyncio
async def test_migrate_v2_to_v3_removes_charge_rate_state_and_entities() -> None:
    """v3 retires only charge-rate values and the seven registry entities."""
    preserved_data = {"hsem_ocpp_enabled": True, "hsem_ev_target_soc": 82}
    preserved_options = {
        "hsem_batteries_wait_mode_behavior": "strict",
        "hsem_ev_smart_charging": True,
    }
    entry = _make_entry(
        version=2,
        data={**preserved_data, **dict.fromkeys(_V3_DEPRECATED_KEYS, "old")},
        options={**preserved_options, **dict.fromkeys(_V3_DEPRECATED_KEYS, "old")},
    )
    hass = MagicMock()
    registry = MagicMock()
    retired = [
        SimpleNamespace(
            domain="number",
            platform="hsem",
            unique_id=f"hsem_{ENTRY_ID}_charge_rate_{bucket}",
            entity_id=f"number.user_named_charge_rate_{index}",
        )
        for index, bucket in enumerate(_CHARGE_RATE_BUCKETS)
    ]
    unrelated = SimpleNamespace(
        domain="number",
        platform="hsem",
        unique_id=f"hsem_{ENTRY_ID}_ev_target_soc",
        entity_id="number.hsem_ev_target_soc",
    )

    with (
        patch("custom_components.hsem.config_flow.er.async_get", return_value=registry),
        patch(
            "custom_components.hsem.config_flow.er.async_entries_for_config_entry",
            return_value=[*retired, unrelated],
        ),
        patch(
            "custom_components.hsem.config_flow.er.async_migrate_entries",
            new=AsyncMock(),
        ) as migrate_entries,
    ):
        result = await HSEMConfigFlow().async_migrate_entry(hass, entry)

    assert result is True
    migrate_entries.assert_not_awaited()
    update = hass.config_entries.async_update_entry.call_args.kwargs
    # The migration chain doesn't stop at v3 — an entry starting at v2 runs
    # straight through the v3->v4 step too (empty registry list here, so it
    # contributes no further removals, but it does bump the final version).
    assert update["version"] == 4
    assert update["data"] == preserved_data
    assert update["options"] == preserved_options
    assert len(_V3_DEPRECATED_KEYS) == 8
    assert {call.args[0] for call in registry.async_remove.call_args_list} == {
        row.entity_id for row in retired
    }
    assert unrelated.entity_id not in {
        call.args[0] for call in registry.async_remove.call_args_list
    }


@pytest.mark.asyncio
async def test_migrate_v3_to_v4_removes_battery_schedule_state_and_entities() -> None:
    """v4 retires only the fixed-battery-schedule values and their nine entities."""
    preserved_data = {"hsem_ocpp_enabled": True, "hsem_ev_target_soc": 82}
    preserved_options = {
        "hsem_batteries_wait_mode_behavior": "strict",
        "hsem_ev_smart_charging": True,
    }
    entry = _make_entry(
        version=3,
        data={**preserved_data, **dict.fromkeys(_V4_DEPRECATED_KEYS, "old")},
        options={**preserved_options, **dict.fromkeys(_V4_DEPRECATED_KEYS, "old")},
    )
    hass = MagicMock()
    registry = MagicMock()
    retired_switches = [
        SimpleNamespace(
            domain="switch",
            platform="hsem",
            unique_id=(
                f"hsem_{ENTRY_ID}_hsem_batteries_enable_batteries_schedule_{n}_switch"
            ),
            entity_id=f"switch.batteries_schedule_{n}",
        )
        for n in (1, 2, 3)
    ]
    retired_times = [
        SimpleNamespace(
            domain="time",
            platform="hsem",
            unique_id=(
                f"hsem_{ENTRY_ID}_hsem_batteries_enable_batteries_schedule_{n}"
                f"_{edge}_time"
            ),
            entity_id=f"time.batteries_schedule_{n}_{edge}",
        )
        for n in (1, 2, 3)
        for edge in ("start", "end")
    ]
    retired = retired_switches + retired_times
    unrelated = SimpleNamespace(
        domain="switch",
        platform="hsem",
        unique_id=f"hsem_{ENTRY_ID}_dynamic_discharge_floor_switch",
        entity_id="switch.hsem_dynamic_discharge_floor",
    )

    with (
        patch("custom_components.hsem.config_flow.er.async_get", return_value=registry),
        patch(
            "custom_components.hsem.config_flow.er.async_entries_for_config_entry",
            return_value=[*retired, unrelated],
        ),
        patch(
            "custom_components.hsem.config_flow.er.async_migrate_entries",
            new=AsyncMock(),
        ) as migrate_entries,
    ):
        result = await HSEMConfigFlow().async_migrate_entry(hass, entry)

    assert result is True
    migrate_entries.assert_not_awaited()
    update = hass.config_entries.async_update_entry.call_args.kwargs
    assert update["version"] == 4
    assert update["data"] == preserved_data
    assert update["options"] == preserved_options
    assert len(_V4_DEPRECATED_KEYS) == 9
    assert {call.args[0] for call in registry.async_remove.call_args_list} == {
        row.entity_id for row in retired
    }
    assert unrelated.entity_id not in {
        call.args[0] for call in registry.async_remove.call_args_list
    }


@pytest.mark.asyncio
async def test_migrate_v3_to_v4_is_noop_for_already_migrated_entry() -> None:
    """A second migration attempt on an already-v4 entry touches nothing."""
    hass = MagicMock()
    entry = _make_entry(version=4)

    with (
        patch(
            "custom_components.hsem.config_flow.er.async_migrate_entries",
            new=AsyncMock(),
        ) as migrate_entries,
        patch(
            "custom_components.hsem.config_flow.er.async_get", return_value=MagicMock()
        ),
        patch(
            "custom_components.hsem.config_flow.er.async_entries_for_config_entry",
            return_value=[],
        ),
    ):
        result = await HSEMConfigFlow().async_migrate_entry(hass, entry)

    assert result is True
    hass.config_entries.async_update_entry.assert_not_called()
    migrate_entries.assert_not_awaited()


def test_number_platform_exposes_only_functional_controls() -> None:
    """The seven retired temperature buckets must not be registered."""
    from custom_components.hsem.number import NUMBER_DESCRIPTIONS

    keys = {description.key for description in NUMBER_DESCRIPTIONS}
    assert len(keys) == 4
    assert all("charge_rate" not in key for key in keys)

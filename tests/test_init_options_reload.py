"""Tests for the HSEM options update listener (issue #1139).

Since issue #859 the switch/number/time/sensor platforms only create EV and
OCPP entities when their feature flag is on at platform setup. The options
update listener used to refresh the coordinator in place on every change, so
turning one of those flags on or off never added or removed entities until a
manual reload.

The same listener also fires every time an HSEM switch/number/time/selector
persists its state into ``config_entry.options``. A blanket reload is
therefore wrong; these tests pin the rule that only an entity-gating flag
change schedules a reload.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem import (
    ENTITY_GATING_CONFIG_KEYS,
    HSEMRuntimeData,
    _entity_gating_snapshot,
    async_setup_entry,
    async_update_options,
)


def _make_entry(
    options: dict[str, Any] | None = None, data: dict[str, Any] | None = None
) -> MagicMock:
    """Return a config-entry stand-in with real options/data dicts."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = dict(options or {})
    entry.data = dict(data or {})
    entry.runtime_data = None
    return entry


def _attach_runtime(entry: MagicMock) -> MagicMock:
    """Attach runtime data with a snapshot of the entry's current gating."""
    coordinator = MagicMock()
    coordinator.async_options_updated = AsyncMock()
    entry.runtime_data = HSEMRuntimeData(
        coordinator=coordinator,
        entity_gating=_entity_gating_snapshot(entry),
    )
    return coordinator


@pytest.fixture
def mock_hass() -> MagicMock:
    """Return a mocked HomeAssistant with a recordable reload scheduler."""
    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_schedule_reload = MagicMock()
    return hass


def test_gating_keys_cover_every_platform_gate() -> None:
    """The snapshot must track exactly the four flags the platforms gate on."""
    assert set(ENTITY_GATING_CONFIG_KEYS) == {
        "hsem_ev_planned_load_enabled",
        "hsem_ev_second_planned_load_enabled",
        "hsem_ocpp_enabled",
        "hsem_ocpp_second_enabled",
    }


def test_snapshot_reads_options_over_data_with_defaults() -> None:
    """The snapshot resolves options, then data, then defaults (all False)."""
    entry = _make_entry(
        options={"hsem_ocpp_enabled": True},
        data={"hsem_ev_planned_load_enabled": True, "hsem_ocpp_enabled": False},
    )
    assert _entity_gating_snapshot(entry) == {
        "hsem_ev_planned_load_enabled": True,
        "hsem_ev_second_planned_load_enabled": False,
        "hsem_ocpp_enabled": True,
        "hsem_ocpp_second_enabled": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ENTITY_GATING_CONFIG_KEYS)
@pytest.mark.parametrize("initial", [False, True])
async def test_gating_flag_change_schedules_reload(
    mock_hass: MagicMock, key: str, initial: bool
) -> None:
    """Turning any gating flag on or off reloads instead of refreshing in place."""
    entry = _make_entry(options={key: initial})
    coordinator = _attach_runtime(entry)

    entry.options[key] = not initial
    await async_update_options(mock_hass, entry)

    mock_hass.config_entries.async_schedule_reload.assert_called_once_with("test_entry")
    coordinator.async_options_updated.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeat_update_before_reload_does_not_schedule_twice(
    mock_hass: MagicMock,
) -> None:
    """A second options write before the reload runs must not reload again."""
    entry = _make_entry()
    coordinator = _attach_runtime(entry)

    entry.options["hsem_ev_planned_load_enabled"] = True
    await async_update_options(mock_hass, entry)
    await async_update_options(mock_hass, entry)

    mock_hass.config_entries.async_schedule_reload.assert_called_once_with("test_entry")
    coordinator.async_options_updated.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    [
        # Entity-driven options writes (switch, number, time, selector).
        ("hsem_ev_smart_charging", False),
        ("hsem_ev_force_charge_now", True),
        ("hsem_ev_target_soc", 60),
        ("hsem_ev_deadline_time", "06:30:00"),
        ("hsem_solcast_pv_forecast_forecast_likelihood", "pv_estimate10"),
        ("hsem_batteries_charge_efficiency", 95),
        ("hsem_read_only", True),
    ],
)
async def test_entity_options_write_refreshes_in_place(
    mock_hass: MagicMock, key: str, value: Any
) -> None:
    """HSEM's own entities saving state must never reload the integration."""
    entry = _make_entry(
        options={
            "hsem_ev_planned_load_enabled": True,
            "hsem_ocpp_enabled": True,
        }
    )
    coordinator = _attach_runtime(entry)

    entry.options[key] = value
    await async_update_options(mock_hass, entry)

    mock_hass.config_entries.async_schedule_reload.assert_not_called()
    coordinator.async_options_updated.assert_awaited_once()


@pytest.mark.asyncio
async def test_unrelated_option_change_refreshes_in_place(
    mock_hass: MagicMock,
) -> None:
    """Non-gating options keep the in-place coordinator refresh."""
    entry = _make_entry(options={"hsem_ev_planned_load_charger_power_kw": 7.4})
    coordinator = _attach_runtime(entry)

    entry.options["hsem_ev_planned_load_charger_power_kw"] = 11.0
    entry.options["hsem_ocpp_port"] = 9100
    await async_update_options(mock_hass, entry)

    mock_hass.config_entries.async_schedule_reload.assert_not_called()
    coordinator.async_options_updated.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_default_is_not_a_change(mock_hass: MagicMock) -> None:
    """Writing a gating flag's default value explicitly must not reload."""
    entry = _make_entry()
    coordinator = _attach_runtime(entry)

    for key in ENTITY_GATING_CONFIG_KEYS:
        entry.options[key] = False
    await async_update_options(mock_hass, entry)

    mock_hass.config_entries.async_schedule_reload.assert_not_called()
    coordinator.async_options_updated.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_without_runtime_data_is_noop(mock_hass: MagicMock) -> None:
    """An update arriving after unload must neither reload nor refresh."""
    entry = _make_entry(options={"hsem_ocpp_enabled": True})

    await async_update_options(mock_hass, entry)

    mock_hass.config_entries.async_schedule_reload.assert_not_called()


@pytest.mark.asyncio
async def test_setup_entry_snapshots_gating_flags() -> None:
    """async_setup_entry stores the gating values the platforms are set up with."""
    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    coordinator = MagicMock()
    coordinator.async_setup = AsyncMock()
    coordinator.async_run_first_refresh = AsyncMock()

    entry = _make_entry(
        options={"hsem_ev_second_planned_load_enabled": True},
        data={"hsem_ocpp_enabled": True},
    )

    with (
        patch(
            "custom_components.hsem.check_huawei_solar_version",
            new=AsyncMock(return_value=True),
        ),
        patch("custom_components.hsem.async_init_hsem_logger", new=AsyncMock()),
        patch(
            "custom_components.hsem.HSEMDataUpdateCoordinator",
            return_value=coordinator,
        ),
    ):
        assert await async_setup_entry(hass, entry) is True

    assert entry.runtime_data.entity_gating == {
        "hsem_ev_planned_load_enabled": False,
        "hsem_ev_second_planned_load_enabled": True,
        "hsem_ocpp_enabled": True,
        "hsem_ocpp_second_enabled": False,
    }

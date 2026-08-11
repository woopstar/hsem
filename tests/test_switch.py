"""Tests for the HSEM switch entity.

Verifies that toggling a switch updates the HA state instantly and then
persists the new value to the config entry.  This ordering matters because
the config-entry update triggers a background planner run, and the UI must
not wait for that run to complete before reflecting the toggle.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.hsem.custom_switches.description import (
    HSEMSwitchEntityDescription,
)
from custom_components.hsem.custom_switches.switch import HSEMSwitch
from custom_components.hsem.utils.sensornames.controls import (
    get_read_only_switch_key,
)


def _mock_config_entry(**option_overrides: object) -> MagicMock:
    """Return a minimal mock ConfigEntry."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {
        get_read_only_switch_key(): False,
        **option_overrides,
    }
    return entry


def _mock_hass() -> MagicMock:
    """Return a minimal Home Assistant mock."""
    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


def _make_switch(
    key: str = get_read_only_switch_key(),
    default: bool = False,
) -> HSEMSwitch:
    """Build an HSEMSwitch with mocked hass/config entry."""
    hass = _mock_hass()
    config_entry = _mock_config_entry(**{key: default})
    description = HSEMSwitchEntityDescription(
        key=key,
        name="Read Only",
        description="Disable automatic working-mode changes.",
    )
    return HSEMSwitch(hass, config_entry, description)


class TestHSEMSwitchTurnOn:
    """Tests for async_turn_on."""

    @pytest.mark.asyncio
    async def test_turn_on_sets_is_on(self) -> None:
        """Turning on must set _attr_is_on to True."""
        switch = _make_switch(default=False)
        switch.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]
        await switch.async_turn_on()
        assert switch.is_on is True

    @pytest.mark.asyncio
    async def test_turn_on_writes_state(self) -> None:
        """async_write_ha_state must be called so HA reflects the toggle."""
        switch = _make_switch(default=False)
        switch.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]
        await switch.async_turn_on()
        switch.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_on_persists_to_config_entry(self) -> None:
        """The new on value must be written to the config entry options."""
        switch = _make_switch(default=False)
        switch.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]
        await switch.async_turn_on()
        switch.hass.config_entries.async_update_entry.assert_called_once()  # type: ignore[attr-defined]
        call_kwargs = switch.hass.config_entries.async_update_entry.call_args[1]  # type: ignore[attr-defined]
        assert call_kwargs["options"][get_read_only_switch_key()] is True

    @pytest.mark.asyncio
    async def test_turn_on_writes_state_before_persisting(self) -> None:
        """UI feedback must happen before the config-entry update is triggered."""
        switch = _make_switch(default=False)
        calls: list[str] = []

        def _track_write_state() -> None:
            calls.append("write_state")

        def _track_update_entry(*args: Any, **kwargs: Any) -> None:
            calls.append("update_entry")

        switch.async_write_ha_state = _track_write_state  # type: ignore[method-assign,misc,assignment]
        switch.hass.config_entries.async_update_entry = _track_update_entry  # type: ignore[method-assign,misc,assignment]

        await switch.async_turn_on()

        assert calls == ["write_state", "update_entry"]


class TestHSEMSwitchTurnOff:
    """Tests for async_turn_off."""

    @pytest.mark.asyncio
    async def test_turn_off_sets_is_on(self) -> None:
        """Turning off must set _attr_is_on to False."""
        switch = _make_switch(default=True)
        switch.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]
        await switch.async_turn_off()
        assert switch.is_on is False

    @pytest.mark.asyncio
    async def test_turn_off_writes_state(self) -> None:
        """async_write_ha_state must be called so HA reflects the toggle."""
        switch = _make_switch(default=True)
        switch.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]
        await switch.async_turn_off()
        switch.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_off_persists_to_config_entry(self) -> None:
        """The new off value must be written to the config entry options."""
        switch = _make_switch(default=True)
        switch.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]
        await switch.async_turn_off()
        switch.hass.config_entries.async_update_entry.assert_called_once()  # type: ignore[attr-defined]
        call_kwargs = switch.hass.config_entries.async_update_entry.call_args[1]  # type: ignore[attr-defined]
        assert call_kwargs["options"][get_read_only_switch_key()] is False

    @pytest.mark.asyncio
    async def test_turn_off_writes_state_before_persisting(self) -> None:
        """UI feedback must happen before the config-entry update is triggered."""
        switch = _make_switch(default=True)
        calls: list[str] = []

        def _track_write_state() -> None:
            calls.append("write_state")

        def _track_update_entry(*args: Any, **kwargs: Any) -> None:
            calls.append("update_entry")

        switch.async_write_ha_state = _track_write_state  # type: ignore[method-assign,misc,assignment]
        switch.hass.config_entries.async_update_entry = _track_update_entry  # type: ignore[method-assign,misc,assignment]

        await switch.async_turn_off()

        assert calls == ["write_state", "update_entry"]


class TestHSEMSwitchConfigUpdateListener:
    """Tests for the config-entry update listener on the switch."""

    @pytest.mark.asyncio
    async def test_config_update_listener_updates_state(self) -> None:
        """When the config entry changes externally, the switch state updates."""
        switch = _make_switch(default=False)
        switch.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]

        updated_entry = _mock_config_entry(**{get_read_only_switch_key(): True})
        await switch._async_handle_config_update(switch.hass, updated_entry)

        assert switch.is_on is True
        switch.async_write_ha_state.assert_called_once()

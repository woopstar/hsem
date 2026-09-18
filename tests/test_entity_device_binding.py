"""Tests for ``HSEMEntity``'s device attachment on add-to-hass.

An HSEM entity that names a ``source_device_id`` (e.g. a Huawei-derived
sensor) is re-parented onto that device in the entity registry, so it appears
under the hardware it reflects instead of under the HSEM controller device.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.helpers.entity import Entity

from custom_components.hsem.devices import HSEMDevice
from custom_components.hsem.entity import HSEMEntity

_MODULE = "custom_components.hsem.entity"
_ENTITY_ID = "sensor.hsem_test_entity"
_SOURCE_DEVICE_ID = "huawei_device_id"


def _entity(source_device_id: str | None = None) -> HSEMEntity:
    """Return an HSEM entity, optionally naming a source device."""
    config_entry = MagicMock()
    config_entry.entry_id = "test_entry"
    config_entry.options = {}
    config_entry.data = {}
    entity = HSEMEntity(config_entry)
    entity.hass = MagicMock()
    entity.entity_id = _ENTITY_ID
    if source_device_id is not None:
        entity.source_device_id = source_device_id  # type: ignore[attr-defined]  # opt-in binding
    return entity


def _registries(*, entity_entry: Any, device_entry: Any) -> tuple[MagicMock, MagicMock]:
    """Return mock entity and device registries returning the given entries."""
    entity_reg = MagicMock()
    entity_reg.async_get.return_value = entity_entry
    device_reg = MagicMock()
    device_reg.async_get.return_value = device_entry
    return entity_reg, device_reg


class TestDeviceBinding:
    """Re-parenting happens only when it is both requested and needed."""

    @pytest.mark.asyncio
    async def test_entity_naming_another_device_is_rebound(self) -> None:
        """A source device different from the current one triggers an update."""
        entity = _entity(_SOURCE_DEVICE_ID)
        entity_reg, device_reg = _registries(
            entity_entry=MagicMock(device_id="hsem_device_id"),
            device_entry=MagicMock(id=_SOURCE_DEVICE_ID),
        )

        with (
            patch(f"{_MODULE}.er.async_get", return_value=entity_reg),
            patch(f"{_MODULE}.dr.async_get", return_value=device_reg),
            patch.object(Entity, "async_added_to_hass", AsyncMock()) as base,
        ):
            await entity.async_added_to_hass()

        entity_reg.async_update_entity.assert_called_once_with(
            _ENTITY_ID, device_id=_SOURCE_DEVICE_ID
        )
        base.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_entity_without_a_source_device_is_left_alone(self) -> None:
        """Most HSEM entities stay on the HSEM device they were created on."""
        entity = _entity()
        entity_reg, device_reg = _registries(
            entity_entry=MagicMock(device_id="hsem_device_id"), device_entry=None
        )

        with (
            patch(f"{_MODULE}.er.async_get", return_value=entity_reg),
            patch(f"{_MODULE}.dr.async_get", return_value=device_reg),
            patch.object(Entity, "async_added_to_hass", AsyncMock()) as base,
        ):
            await entity.async_added_to_hass()

        entity_reg.async_update_entity.assert_not_called()
        base.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unregistered_entity_is_left_alone(self) -> None:
        """An entity not yet in the registry cannot be re-parented."""
        entity = _entity(_SOURCE_DEVICE_ID)
        entity_reg, device_reg = _registries(entity_entry=None, device_entry=None)

        with (
            patch(f"{_MODULE}.er.async_get", return_value=entity_reg),
            patch(f"{_MODULE}.dr.async_get", return_value=device_reg),
            patch.object(Entity, "async_added_to_hass", AsyncMock()) as base,
        ):
            await entity.async_added_to_hass()

        entity_reg.async_update_entity.assert_not_called()
        base.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_removal_delegates_to_home_assistant(self) -> None:
        """Removal has no HSEM-specific work beyond the base class."""
        entity = _entity()

        with patch.object(Entity, "async_will_remove_from_hass", AsyncMock()) as base:
            await entity.async_will_remove_from_hass()

        base.assert_awaited_once()


class TestDeviceInfo:
    """Each entity reports the HSEM device it belongs to."""

    def test_device_info_follows_the_declared_device(self) -> None:
        """The default controller device differs from a per-EV device."""
        controller = _entity()
        ev = _entity()
        ev._hsem_device = HSEMDevice.EV_PRIMARY

        assert controller._hsem_device is HSEMDevice.CONTROLLER
        assert controller.device_info != ev.device_info

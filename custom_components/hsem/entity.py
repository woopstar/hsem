from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.hsem.const import DOMAIN, NAME

if TYPE_CHECKING:
    from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class HSEMEntity(Entity):
    """Base class for all HSEM entities.

    Provides shared device information and entity registry attachment logic
    used by select, switch, and time platform entities.
    """

    _attr_icon = "mdi:flash"
    _attr_has_entity_name = True

    def __init__(self, config_entry) -> None:
        """Initialize the HSEM entity."""
        super().__init__()
        self._config = config_entry

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._config.entry_id)},
            name=NAME,
            manufacturer=DOMAIN.upper(),
            model="Custom Integration",
        )

    async def async_will_remove_from_hass(self) -> None:
        """Entity being removed from hass."""
        await super().async_will_remove_from_hass()

    async def async_added_to_hass(self) -> None:
        """Attach the entity to same device as the source entity."""

        entity_reg = er.async_get(self.hass)
        entity_entry = entity_reg.async_get(self.entity_id)
        if entity_entry is None or not hasattr(self, "source_device_id"):
            return await super().async_added_to_hass()

        device_id: str = getattr(self, "source_device_id")  # noqa: B009
        device_reg = dr.async_get(self.hass)
        device_entry = device_reg.async_get(device_id)
        if (
            not device_entry or device_entry.id == entity_entry.device_id
        ):  # pragma: no cover
            return await super().async_added_to_hass()
        _LOGGER.debug("Binding %s to device %s", self.entity_id, device_id)
        entity_reg.async_update_entity(self.entity_id, device_id=device_id)

        await super().async_added_to_hass()


class HSEMCoordinatorEntity(CoordinatorEntity["HSEMDataUpdateCoordinator"]):
    """Typed :class:`~homeassistant.helpers.update_coordinator.CoordinatorEntity`
    base for all HSEM coordinator-backed sensors.

    Pre-parametrises ``CoordinatorEntity`` with :class:`HSEMDataUpdateCoordinator`
    so that Pyright correctly resolves the ``coordinator`` type at every
    ``super().__init__`` call site and in every ``self.coordinator`` access.

    Inheriting sensors should call
    ``HSEMCoordinatorEntity.__init__(self, coordinator)`` (or ``super().__init__``)
    instead of the raw ``CoordinatorEntity.__init__``.
    """

    def __init__(self, coordinator: HSEMDataUpdateCoordinator) -> None:
        """Initialise with a typed coordinator.

        Args:
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        super().__init__(coordinator)

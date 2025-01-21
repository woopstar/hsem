import logging
from typing import Any

import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.entity_registry as er
from homeassistant.helpers.entity import Entity

from custom_components.hsem.const import DOMAIN, NAME

_LOGGER = logging.getLogger(__name__)


class HSEMEntity(Entity):
    """
    HSEMEntity is a base class for HSEM (Device) entities that extends RestoreEntity.
    """

    _attr_icon = "mdi:flash"
    _attr_has_entity_name = True

    def __init__(self, config_entry) -> None:
        """Initialize the HSEM"""
        super().__init__()
        self._config = config_entry

    @property
    def device_info(self) -> dict[str, Any]:
        """Return the device information"""
        return {
            "identifiers": {(DOMAIN, self._config.entry_id)},
            "name": NAME,
            "manufacturer": DOMAIN.upper(),
            "model": "Custom Integration",
        }

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

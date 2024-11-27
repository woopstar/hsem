import logging

import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.entity_registry as er
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.const import DOMAIN, NAME

_LOGGER = logging.getLogger(__name__)


class HSEMEntity(RestoreEntity):
    """
    HSEMEntity is a base class for HSEM (Device) entities that extends RestoreEntity.

    Attributes:
        _attr_icon (str): The icon attribute for the entity.
        _attr_has_entity_name (bool): Indicates if the entity has a name.

    Methods:
        __init__(config_entry):
            Initializes the HSEM entity with the provided configuration entry.

      ª  set_entity_id(platform_str, key):
            Sets the entity ID using the platform string and key.

        device_info:
            Returns the device information as a dictionary. If the configuration entry is missing, logs a warning and returns None.
    """

    # Define the attributes of the entity
    _attr_icon = "mdi:flash"
    _attr_has_entity_name = True

    def __init__(self, config_entry):
        """Initialize the HSEM"""
        super().__init__()
        self.config_entry = config_entry

    def set_entity_id(self, platform_str, key):
        """Set the entity id"""
        entity_id = f"{platform_str}.{DOMAIN}_{key}"
        _LOGGER.debug("entity_id = %s", entity_id)
        self.entity_id = entity_id

    @property
    def device_info(self):
        """Return the device information"""
        if not self.config_entry:
            _LOGGER.debug("Config entry is missing for this entity.")
            return None

        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get("device_name", NAME),
            "manufacturer": NAME,
        }

    @property
    def should_poll(self):
        """Return False because entity pushes its state to HA"""
        return False

    async def async_will_remove_from_hass(self):
        """Entity being removed from hass."""
        await super().async_will_remove_from_hass()

    async def async_added_to_hass(self) -> None:
        """Attach the entity to same device as the source entity."""
        await super().async_added_to_hass()

        entity_reg = er.async_get(self.hass)
        entity_entry = entity_reg.async_get(self.entity_id)
        if entity_entry is None or not hasattr(self, "source_device_id"):
            return

        device_id: str = getattr(self, "source_device_id")  # noqa: B009
        device_reg = dr.async_get(self.hass)
        device_entry = device_reg.async_get(device_id)
        if (
            not device_entry or device_entry.id == entity_entry.device_id
        ):  # pragma: no cover
            return
        _LOGGER.debug("Binding %s to device %s", self.entity_id, device_id)
        entity_reg.async_update_entity(self.entity_id, device_id=device_id)

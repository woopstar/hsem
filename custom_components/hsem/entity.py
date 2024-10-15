import logging

from homeassistant.helpers.entity import Entity

from .const import DOMAIN, ICON, NAME

_LOGGER = logging.getLogger(__name__)


class HSEMEntity(Entity):
    """Base class for HSEM (Device)"""

    # Define the attributes of the entity
    _attr_icon = ICON
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
            _LOGGER.warning("Config entry is missing for this entity.")
            return None

        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get("device_name", NAME),
            "manufacturer": NAME,
        }

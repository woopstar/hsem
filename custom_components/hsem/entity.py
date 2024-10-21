import logging

from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, ICON, NAME

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

        set_entity_id(platform_str, key):
            Sets the entity ID using the platform string and key.

        device_info:
            Returns the device information as a dictionary. If the configuration entry is missing, logs a warning and returns None.
    """

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

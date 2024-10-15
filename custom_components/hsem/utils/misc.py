import logging
from homeassistant.helpers import entity_registry as er
from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

def get_config_value(config_entry, key, default_value=None):
    """Get the configuration value from options or fall back to the initial data."""
    return config_entry.options.get(key, config_entry.data.get(key, default_value))

async def async_resolve_entity_id_from_unique_id(self, unique_entity_id, domain="sensor"):
    """
    Resolve the entity_id from the unique_id using the entity registry.

    :param unique_entity_id: Unique ID of the entity to resolve.
    :param domain: The domain of the entity (e.g., 'sensor').
    :return: The resolved entity_id or None if not found.
    """
    # Get the entity registry
    registry = er.async_get(self.hass)

    # Fetch the entity_id from the unique_id
    entry = registry.async_get_entity_id(domain, DOMAIN, unique_entity_id)

    # Log the resolved entity_id for debugging purposes
    if entry:
        _LOGGER.debug(
            f"Resolved entity_id for unique_id {unique_entity_id}: {entry}"
        )
        return entry
    else:
        _LOGGER.warning(
            f"Entity with unique_id {unique_entity_id} not found in registry."
        )
        return None

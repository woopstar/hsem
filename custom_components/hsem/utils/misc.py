import hashlib
import logging

from homeassistant.helpers import entity_registry as er

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def generate_md5_hash(input_sensor):
    """Generate an MD5 hash based on the input sensor's name."""
    return hashlib.md5(input_sensor.encode("utf-8")).hexdigest()


def get_config_value(config_entry, key, default_value=None):
    """Get the configuration value from options or fall back to the initial data."""
    return config_entry.options.get(key, config_entry.data.get(key, default_value))


def convert_to_float(state):
    """Resolve the input sensor state and cast it to a float."""

    if state is None:
        return 0.0

    try:
        return float(state)
    except ValueError:
        return 0.0  # Return None for unexpected states


def convert_to_boolean(state):
    """Resolve the input sensor state and cast it to a boolean."""

    if state is None:
        return False

    state_map = {
        "on": True,
        "true": True,
        "1": True,
        "off": False,
        "false": False,
        "0": False,
        "charging": True,
        "not_charging": False,
        "notcharging": False,
        "unknown": False,
        "available": True,
        "unavailable": False,
        "ready": True,
        "notready": False,
        "not_ready": False,
        "unready": False,
        "disconnected": False,
        "connected": True,
        "locked": False,
        "unlocked": True,
        "paused": False,
        "continue": True,
    }

    # Convert the state to lowercase for case-insensitive comparison
    state_value_lower = state.lower()

    # Check if the state is in the mapping and return the corresponding boolean
    if state_value_lower in state_map:
        return state_map[state_value_lower]
    else:
        return None  # Return None for unexpected states


async def async_resolve_entity_id_from_unique_id(
    self, unique_entity_id, domain="sensor"
):
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
        _LOGGER.debug(f"Resolved entity_id for unique_id {unique_entity_id}: {entry}")
        return entry
    else:
        _LOGGER.warning(
            f"Entity with unique_id {unique_entity_id} not found in registry."
        )
        return None

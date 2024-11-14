"""
This module provides utility functions for the Home Assistant custom integration.

Functions:
    generate_md5_hash(input_sensor):
        Generate an MD5 hash based on the input sensor's name.

    get_config_value(config_entry, key, default_value=None):
        Get the configuration value from options or fall back to the initial data.

    convert_to_float(state):
        Resolve the input sensor state and cast it to a float.

    convert_to_boolean(state):
        Resolve the input sensor state and cast it to a boolean.

    async_resolve_entity_id_from_unique_id(self, unique_entity_id, domain="sensor"):
"""

import hashlib
import logging

from homeassistant.helpers import entity_registry as er

from custom_components.hsem.const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def generate_md5_hash(input_sensor):
    """Generate an SHA-256 hash based on the input sensor's name."""
    return hashlib.sha256(input_sensor.encode("utf-8")).hexdigest()


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
        return 0.0


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
        return None


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
        _LOGGER.debug(
            f"Entity with unique_id {unique_entity_id} not found in registry."
        )
        return None


async def async_set_select_option(self, entity_id, option):
    """Set the selected option for an entity."""

    # Check if entity_id exists
    entity = self.hass.states.get(entity_id)

    if entity is None:
        _LOGGER.error(f"Entity with id {entity_id} not found.")
        return  # Exit the method if entity_id does not exist

    try:
        # Make the service call to set the option
        await self.hass.services.async_call(
            "select",
            "select_option",
            {
                "entity_id": entity_id,
                "option": option,
            },
            blocking=True,
        )
        _LOGGER.debug(f"Set option '{option}' for entity_id '{entity_id}'")
    except Exception as err:
        _LOGGER.error(
            f"Failed to set option '{option}' for entity_id '{entity_id}': {err}"
        )
        raise


def ha_get_entity_state_and_convert(
    self, entity_id, output_type=None, float_precision=2
):
    """Get the state of an entity."""

    state = self.hass.states.get(entity_id)
    if state:
        if output_type is None:
            return state

        if output_type.lower() == "float":
            return round(convert_to_float(state.state), float_precision)
        if output_type.lower() == "boolean":
            return convert_to_boolean(state.state)
        if output_type.lower() == "string":
            return str(state.state)
        else:
            return None
    else:
        _LOGGER.debug(f"Sensor {entity_id} not found.")
        return None

async def async_remove_entity_from_ha(self, entity_unique_id):
    """
    Remove an existing entity in Home Assistant based on its unique ID.

    :param entity_unique_id: The unique ID of the entity to be removed.
    """
    # Check if the entity exists
    entity_exists = await async_resolve_entity_id_from_unique_id(self, entity_unique_id)
    if not entity_exists:
        return False

    # Get the entity registry
    registry = er.async_get(self.hass)

    # Fetch the entity ID for the unique ID
    existing_entry = registry.async_get_entity_id("sensor", DOMAIN, entity_unique_id)

    # Remove the entity if it exists in the registry
    if existing_entry:
        _LOGGER.warning(f"Removing existing entity with unique ID '{entity_unique_id}' before re-adding.")
        registry.async_remove(existing_entry)
        return True
    else:
        return False

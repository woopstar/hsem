"""
This module provides utility functions for the Home Assistant custom integration.
"""

import asyncio
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time
from logging.handlers import RotatingFileHandler

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from sqlalchemy import null

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Create a separate logger for async_logger
HSEM_LOGGER = logging.getLogger("hsem_logger")
LOG_FILE_PATH = "/config/hsem.log"
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_FILE_BACKUP_COUNT = 1  # Keep 1 backup files

# Configure the rotating file handler
file_handler = RotatingFileHandler(
    LOG_FILE_PATH,
    maxBytes=LOG_FILE_MAX_BYTES,
    backupCount=LOG_FILE_BACKUP_COUNT,
)

# Set the log format and level
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)

# Attach the file handler to the logger
HSEM_LOGGER.addHandler(file_handler)
HSEM_LOGGER.setLevel(logging.DEBUG)

# Prevent the logger from propagating to the root logger
HSEM_LOGGER.propagate = False

LOG_EXECUTOR = ThreadPoolExecutor(max_workers=1)

_entity_id_from_unique_id_cache = {}


class EntityNotFoundError(HomeAssistantError):
    """Exception raised when an entity is not found."""


def generate_hash(input_sensor) -> str:
    """Generate an SHA-256 hash based on the input sensor's name."""
    return hashlib.sha256(input_sensor.encode("utf-8")).hexdigest()


def get_config_value(config_entry, key) -> str | None:
    """Get the configuration value from options or fall back to the initial data."""
    if key not in DEFAULT_CONFIG_VALUES:
        raise KeyError(f"Key '{key}' not found in DEFAULT_VALUES")

    if config_entry is None and key in DEFAULT_CONFIG_VALUES:
        return DEFAULT_CONFIG_VALUES[key]

    if config_entry is None:
        return None

    data = config_entry.options.get(
        key, config_entry.data.get(key, DEFAULT_CONFIG_VALUES[key])
    )

    if data is null or data is None:
        return DEFAULT_CONFIG_VALUES[key]

    return data


def convert_to_time(time_value) -> time | None:
    """
    Convert a time value (str or datetime.time) to a datetime.time object.
    """
    if isinstance(time_value, time):
        return time_value

    if isinstance(time_value, str):
        return datetime.strptime(time_value, "%H:%M:%S").time()

    return None


def convert_to_float(state) -> float:
    """Resolve the input sensor state and cast it to a float."""

    if state is None:
        return 0.0

    try:
        return float(state)
    except ValueError:
        return 0.0


def convert_to_int(state) -> int:
    """Resolve the input sensor state and cast it to a float."""

    if state is None:
        return 0

    try:
        return int(state)
    except ValueError:
        return 0


def convert_to_boolean(state) -> bool:
    """Resolve the input sensor state and cast it to a boolean."""

    if state is None:
        return False

    if isinstance(state, bool):
        return state

    if isinstance(state, int):
        return state != 0

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
    if isinstance(state, str):
        state_value_lower = state.lower()

        # Check if the state is in the mapping and return the corresponding boolean
        if state_value_lower in state_map:
            return state_map[state_value_lower]
        else:
            return False

    return False

async def async_resolve_entity_id_from_unique_id(
    self, unique_entity_id, domain="sensor"
) -> str | None:
    """
    Resolve the entity_id from the unique_id using the entity registry.

    :param unique_entity_id: Unique ID of the entity to resolve.
    :param domain: The domain of the entity (e.g., 'sensor').
    :return: The resolved entity_id or None if not found.
    """

    global _entity_id_from_unique_id_cache

    cache_key = (DOMAIN, domain, unique_entity_id)

    # Check cache first
    entity_id = _entity_id_from_unique_id_cache.get(cache_key)
    if entity_id:
        if self.hass.states.get(entity_id) is not None:
            return entity_id
        del _entity_id_from_unique_id_cache[cache_key]

    # Get the entity registry
    registry = er.async_get(self.hass)

    # Fetch the entity_id from the unique_id
    entry = registry.async_get_entity_id(domain, DOMAIN, unique_entity_id)

    # Log the resolved entity_id for debugging purposes
    if entry:
        # Store in cache
        _entity_id_from_unique_id_cache[cache_key] = entry

        _LOGGER.debug(f"Resolved entity_id for unique_id {unique_entity_id}: {entry}")
        return entry
    else:
        _LOGGER.debug(
            f"Entity with unique_id {unique_entity_id} not found in registry."
        )
        return None


async def async_set_number_value(self, entity_id, value) -> None:
    """
    Set the value for a number entity.

    Parameters:
    - entity_id (str): The entity_id of the number entity.
    - value (float|int): The value to set.
    """
    entity = self.hass.states.get(entity_id)

    if entity is None:
        _LOGGER.error(f"Entity with id {entity_id} not found.")
        return

    try:
        await self.hass.services.async_call(
            "number",
            "set_value",
            {
                "entity_id": entity_id,
                "value": value,
            },
            blocking=True,
        )
        _LOGGER.debug(f"Set value '{value}' for number entity_id '{entity_id}'")
    except Exception as err:
        _LOGGER.error(
            f"Failed to set value '{value}' for number entity_id '{entity_id}': {err}"
        )
        raise


async def async_set_select_option(self, entity_id, option) -> None:
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
) -> float | int | bool | str | None:
    """Get the state of an entity."""

    if entity_id is None:
        return None

    if not self.hass.states.get(entity_id):
        raise EntityNotFoundError(f"Entity '{entity_id}' not found in Home Assistant.")

    state = self.hass.states.get(entity_id)

    try:

        if output_type is None:
            if state.state == "unknown":
                raise EntityNotFoundError(f"Entity '{entity_id}' state unknown.")

            return state

        if output_type.lower() == "float":
            if state.state == "unknown":
                raise EntityNotFoundError(f"Entity '{entity_id}' state unknown.")
            return round(convert_to_float(state.state), float_precision)

        if output_type.lower() == "int":
            if state.state == "unknown":
                raise EntityNotFoundError(f"Entity '{entity_id}' state unknown.")
            return convert_to_int(state.state)

        if output_type.lower() == "boolean":
            if state.state == "unknown":
                raise EntityNotFoundError(f"Entity '{entity_id}' state unknown.")

            return convert_to_boolean(state.state)

        if output_type.lower() == "string":
            return str(state.state)

        _LOGGER.error(
            f"Unknown output type '{output_type}' for entity '{entity_id}'. Returning None."
        )
        return None

    except Exception as e:
        raise HomeAssistantError(
            f"Error converting state of entity '{entity_id}' to type '{output_type}': {e}"
        )


async def async_remove_entity_from_ha(self, entity_unique_id) -> bool:
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
        _LOGGER.debug(
            f"Removing existing entity with unique ID '{entity_unique_id}' before re-adding."
        )
        registry.async_remove(existing_entry)
        return True
    else:
        return False


async def async_entity_exists(hass, entity_id) -> bool:
    """Check if an entity exists in Home Assistant."""
    return hass.states.get(entity_id) is not None


async def async_device_exists(hass, device_id) -> bool:
    """Check if a device exists in Home Assistant."""
    device_registry = dr.async_get(hass)
    return device_registry.async_get(device_id) is not None


async def async_logger(self, msg, level="debug") -> None:
    """
    Log a message to a dedicated file-based logger.

    :param msg: The message to log.
    :param level: The log level ('debug', 'info', 'warning', 'error', 'critical').
    """
    if self._hsem_verbose_logging:
        log_method = getattr(HSEM_LOGGER, level.lower(), HSEM_LOGGER.debug)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(LOG_EXECUTOR, log_method, msg)

def get_max_discharge_power(usable_capacity: int) -> int:
    """
    Return max discharge power in WATT based on Huawei battery usable capacity.
    Supports both old (S0: 5/10/15 kWh) and new (S1: 7/14/21 kWh) series.
    """
    mapping = {
        # Old batteries (S0)
        5: 2500,
        10: 5000,
        15: 5000,
        # New batteries (S1)
        7: 3500,
        14: 7000,
        21: 10500,
    }
    return mapping.get(usable_capacity, 2500)

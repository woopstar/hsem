"""Home Assistant integration helpers — entity resolution, service calls,
state queries, and device/entity existence checks.

These utilities wrap the HA entity registry, state machine, and service
calls behind safe async interfaces that handle caching, error reporting,
and type conversion consistently across the HSEM integration.
"""

from typing import Any, cast

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.utils.conversion import (
    convert_to_boolean,
    convert_to_float,
    convert_to_int,
)
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

_entity_id_from_unique_id_cache: dict[tuple[str, str, str], str] = {}


class EntityNotFoundError(HomeAssistantError):
    """Exception raised when an entity is not found."""


async def async_resolve_entity_id_from_unique_id(  # NOSONAR
    self: Any, unique_entity_id: str, domain: str = "sensor"
) -> str | None:
    """Resolve an entity_id from a unique_id using the entity registry.

    Results are cached per (domain, unique_id) pair to avoid repeated
    registry lookups.

    Args:
        self: The calling coordinator or component instance.
        unique_entity_id: The unique ID of the entity to resolve.
        domain: The Home Assistant domain (e.g. ``"sensor"``).

    Returns:
        The resolved entity_id, or None if not found.
    """

    global _entity_id_from_unique_id_cache

    cache_key = (DOMAIN, domain, unique_entity_id)

    if self.hass is None:
        return None

    # Check cache first
    entity_id = _entity_id_from_unique_id_cache.get(cache_key)
    if entity_id:
        if self.hass.states.get(entity_id) is not None:
            return cast(str, entity_id)
        del _entity_id_from_unique_id_cache[cache_key]

    # Get the entity registry
    registry = er.async_get(self.hass)

    # Fetch the entity_id from the unique_id
    entry = registry.async_get_entity_id(domain, DOMAIN, unique_entity_id)

    # Log the resolved entity_id for debugging purposes
    if entry:
        # Store in cache
        _entity_id_from_unique_id_cache[cache_key] = entry

        _LOGGER.debug(
            "Resolved entity_id for unique_id %s: %s", unique_entity_id, entry
        )
        return cast(str, entry)
    else:
        _LOGGER.debug(
            "Entity with unique_id %s not found in registry", unique_entity_id
        )
        return None


async def async_set_number_value(self: Any, entity_id: str, value: float | int) -> None:
    """Set the value of a Home Assistant number entity.

    Args:
        self: The calling coordinator or component instance.
        entity_id: The entity_id of the number entity.
        value: The numeric value to set.

    Raises:
        ServiceNotFound: If the ``number.set_value`` service is not registered.
        HomeAssistantError: On any other HA-level write failure.
    """
    entity = self.hass.states.get(entity_id)

    if entity is None:
        _LOGGER.error("Entity with id %s not found", entity_id)
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
        _LOGGER.debug("Set value '%s' for number entity_id '%s'", value, entity_id)
    except ServiceNotFound, ServiceValidationError, HomeAssistantError:
        _LOGGER.exception(
            "Failed to set value '%s' for number entity_id '%s' (operation=set_value)",
            value,
            entity_id,
        )
        raise


async def async_set_select_option(self: Any, entity_id: str, option: str) -> None:
    """Set the selected option of a Home Assistant select entity.

    Args:
        self: The calling coordinator or component instance.
        entity_id: The entity_id of the select entity.
        option: The option value to select.

    Raises:
        ServiceNotFound: If the ``select.select_option`` service is not registered.
        HomeAssistantError: On any other HA-level write failure.
    """

    # Check if entity_id exists
    entity = self.hass.states.get(entity_id)

    if entity is None:
        _LOGGER.error("Entity with id %s not found", entity_id)
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
        _LOGGER.debug("Set option '%s' for entity_id '%s'", option, entity_id)
    except ServiceNotFound, ServiceValidationError, HomeAssistantError:
        _LOGGER.exception(
            "Failed to set option '%s' for entity_id '%s' (operation=select_option)",
            option,
            entity_id,
        )
        raise


def ha_get_entity_state_and_convert(
    self: Any,
    entity_id: str | None,
    output_type: str | None = None,
    float_precision: int = 2,
) -> float | int | bool | str | None:
    """Get an entity's state and convert it to the requested type.

    Args:
        self: The calling coordinator or component instance.
        entity_id: The entity_id to query, or None.
        output_type: The desired output type (``"float"``, ``"int"``,
            ``"boolean"``, ``"string"``, or None for the raw state object).
        float_precision: Number of decimal places when output_type is ``"float"``.

    Returns:
        The converted state value, or None if the entity is unavailable.

    Raises:
        EntityNotFoundError: If the entity is not found or state is unknown.
        HomeAssistantError: If conversion fails.
    """

    if entity_id is None:
        return None

    if not self.hass.states.get(entity_id):
        raise EntityNotFoundError(f"Entity '{entity_id}' not found in Home Assistant.")

    state = self.hass.states.get(entity_id)

    try:
        if output_type is None:
            if state.state == STATE_UNKNOWN:
                raise EntityNotFoundError(f"Entity '{entity_id}' state unknown.")

            return cast("float | int | bool | str | None", state)

        if output_type.lower() == "float":
            if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                return None
            value = convert_to_float(state.state)
            if value is None:
                return None
            return cast(float, round(value, float_precision))

        if output_type.lower() == "int":
            if state.state == STATE_UNKNOWN:
                raise EntityNotFoundError(f"Entity '{entity_id}' state unknown.")
            return convert_to_int(state.state)

        if output_type.lower() == "boolean":
            if state.state == STATE_UNKNOWN:
                raise EntityNotFoundError(f"Entity '{entity_id}' state unknown.")

            return convert_to_boolean(state.state)

        if output_type.lower() == "string":
            return str(state.state)

        _LOGGER.error(
            f"Unknown output type '{output_type}' for entity '{entity_id}'. Returning None."
        )
        return None

    except (ValueError, TypeError, AttributeError) as e:
        raise HomeAssistantError(
            f"Error converting state of entity '{entity_id}' to type '{output_type}': {e}"
        )


async def async_remove_entity_from_ha(self: Any, entity_unique_id: str) -> bool:
    """Remove an entity from Home Assistant by its unique ID.

    Args:
        self: The calling coordinator or component instance.
        entity_unique_id: The unique ID of the entity to remove.

    Returns:
        True if the entity was found and removed, False otherwise.
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


async def async_entity_exists(hass: Any, entity_id: str) -> bool:  # NOSONAR
    """Check whether an entity exists in Home Assistant.

    Args:
        hass: The Home Assistant core instance.
        entity_id: The entity_id to check.

    Returns:
        True if the entity exists, False otherwise.
    """
    return hass.states.get(entity_id) is not None


async def async_device_exists(hass: Any, device_id: str) -> bool:  # NOSONAR
    """Check whether a device exists in Home Assistant.

    Args:
        hass: The Home Assistant core instance.
        device_id: The device ID to check.

    Returns:
        True if the device exists, False otherwise.
    """
    device_registry = dr.async_get(hass)
    return device_registry.async_get(device_id) is not None

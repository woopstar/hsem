"""This module provides utility functions for the Home Assistant custom integration."""

import hashlib
import logging
from datetime import datetime, time, timedelta

from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from sqlalchemy import null

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES, DOMAIN

# Re-export async_logger from its dedicated module so that existing callers
# importing it from utils.misc continue to work without changes.
from custom_components.hsem.utils.logger import (  # noqa: F401
    HSEM_LOGGER,
    LOG_EXECUTOR,
    async_logger,
)

_LOGGER = logging.getLogger(__name__)

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


def convert_to_time(time_value) -> time:
    """Convert a time value (str or datetime.time) to a datetime.time object."""
    if isinstance(time_value, time):
        return time_value

    if isinstance(time_value, str):
        return datetime.strptime(time_value, "%H:%M:%S").time()

    return time()


def is_time_in_window(current: time, start: time, end: time) -> bool:
    """Check whether *current* falls within the [start, end) window.

    Handles windows that cross midnight (e.g. 23:00–02:00) correctly.

    Args:
        current: The time to test.
        start: Start of the window (inclusive).
        end: End of the window (exclusive).

    Returns:
        True if *current* is within the window, False otherwise.
    """
    if start <= end:
        # Same-day window (e.g. 07:00–09:00)
        return start <= current < end
    # Cross-midnight window (e.g. 23:00–02:00)
    return current >= start or current < end


def next_window_start_dt(now: datetime, window_start: time) -> datetime:
    """Return the next upcoming datetime when a discharge/charge window begins.

    Anchors ``window_start`` to today's date and advances by one day when that
    moment has already passed, so the returned datetime is always strictly in
    the future relative to ``now``.

    This enables cross-date-boundary charge planning: a 07:00 discharge
    window configured for the next calendar day is correctly resolved when
    it is currently, say, 22:00 on the previous day.

    Args:
        now: Current timezone-aware datetime.
        window_start: Wall-clock start time of the discharge/charge window.

    Returns:
        Timezone-aware datetime of the next occurrence of *window_start*.
    """
    candidate = datetime.combine(now.date(), window_start).replace(tzinfo=now.tzinfo)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def interval_ends_before_window_start(
    interval_end: datetime,
    window_start: time,
    now: datetime,
) -> bool:
    """Return True when an *interval* ends strictly before a schedule *window* begins.

    Resolves ``window_start`` to a timezone-aware :class:`datetime` on the
    correct calendar date so that cross-midnight windows (e.g. a window that
    starts at ``23:00`` today and ends at ``02:00`` tomorrow) are handled
    without false positives.

    Args:
        interval_end: Timezone-aware end of the recommendation interval.
        window_start: Wall-clock start time of the charge/discharge window.
        now: Current timezone-aware datetime (used to anchor the date).

    Returns:
        True if the interval ends before the window starts.
    """
    return interval_end <= next_window_start_dt(now, window_start)


def convert_to_float(state) -> float | None:
    """Resolve the input sensor state and cast it to a float.

    Returns ``None`` for values that cannot be meaningfully interpreted as a
    number: ``None``, the HA sentinel strings ``"unknown"`` / ``"unavailable"``,
    empty strings, and anything that raises a conversion error.  A real numeric
    ``0`` (or ``"0"``) is preserved as ``0.0``.

    This distinction lets callers differentiate between *missing data* and
    *real zero consumption*, which is critical for safe hardware decisions.

    Args:
        state: Raw sensor state value (string, int, float, or None).

    Returns:
        Parsed float value, or ``None`` when the state is absent or invalid.
    """
    if state is None:
        return None

    if isinstance(state, str):
        stripped = state.strip()
        if stripped == "" or stripped.lower() in ("unknown", "unavailable"):
            return None
        try:
            return float(stripped)
        except (ValueError, TypeError):
            return None

    try:
        return float(state)
    except (ValueError, TypeError):
        return None


def convert_to_int(state) -> int:
    """Resolve the input sensor state and cast it to a float."""

    if state is None:
        return 0

    try:
        return int(state)
    except ValueError:
        return 0


def convert_months_to_int(months: list) -> list[int]:
    """Convert month values to integers.

    Args:
        months: List of month values (can be strings or integers)

    Returns:
        List of integer month values (1-12)

    Raises:
        ValueError: If any month is not a valid integer or outside range 1-12
    """
    result = []
    for month in months:
        try:
            month_int = int(float(month))
            if month_int < 1 or month_int > 12:
                raise ValueError(f"Month must be between 1 and 12, got {month_int}")
            result.append(month_int)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid month value: {month}. Error: {e}") from e
    return result


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
        "in_progress": True,
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
    """Resolve the entity_id from the unique_id using the entity registry.

    :param unique_entity_id: Unique ID of the entity to resolve.
    :param domain: The domain of the entity (e.g., 'sensor').
    :return: The resolved entity_id or None if not found.
    """

    global _entity_id_from_unique_id_cache

    cache_key = (DOMAIN, domain, unique_entity_id)

    if self.hass is None:
        return None

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
    """Set the value for a number entity.

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
        _LOGGER.debug("Set value '%s' for number entity_id '%s'", value, entity_id)
    except (ServiceNotFound, ServiceValidationError, HomeAssistantError):
        _LOGGER.exception(
            "Failed to set value '%s' for number entity_id '%s' (operation=set_value)",
            value,
            entity_id,
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
        _LOGGER.debug("Set option '%s' for entity_id '%s'", option, entity_id)
    except (ServiceNotFound, ServiceValidationError, HomeAssistantError):
        _LOGGER.exception(
            "Failed to set option '%s' for entity_id '%s' (operation=select_option)",
            option,
            entity_id,
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
            if state.state in ("unknown", "unavailable"):
                raise EntityNotFoundError(
                    f"Entity '{entity_id}' state is '{state.state}'."
                )
            value = convert_to_float(state.state)
            if value is None:
                raise EntityNotFoundError(
                    f"Entity '{entity_id}' state '{state.state}' cannot be converted to float."
                )
            return round(value, float_precision)

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

    except (ValueError, TypeError, AttributeError) as e:
        raise HomeAssistantError(
            f"Error converting state of entity '{entity_id}' to type '{output_type}': {e}"
        )


async def async_remove_entity_from_ha(self, entity_unique_id) -> bool:
    """Remove an existing entity in Home Assistant based on its unique ID.

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


def get_max_discharge_power(usable_capacity: int) -> int:
    """Return max discharge power in WATT based on Huawei batteries max rated capacity.
    Supports both old (S0: 5/10/15 kWh) and new (S1: 7/14/21 kWh) series.
    """
    mapping = {
        # Old batteries (S0)
        5000: 2500,
        10000: 5000,
        15000: 5000,
        # New batteries (S1)
        7000: 3500,
        14000: 7000,
        21000: 10500,
    }
    return mapping.get(usable_capacity, 2500)


def calculate_recommended_threshold(
    purchase_price: float,
    expected_cycles: int,
    usable_capacity: float,
    conversion_loss: float,
    import_price: float = 0.0,
) -> float:
    """Calculate the recommended price threshold based on battery depreciation and losses.

    Formula: (Purchase Price * Capacity Loss) / (Cycles * Capacity) + (Avg Import Price * Loss %)
    """
    if purchase_price <= 0 or expected_cycles <= 0 or usable_capacity <= 0:
        return 0.0

    # Capacity loss is typically 30% over lifetime for LiFePO4
    capacity_loss = 0.30

    # 1. Depreciation cost per kWh
    depreciation = (purchase_price * capacity_loss) / (
        expected_cycles * usable_capacity
    )

    # 2. Conversion loss cost (approx 10% of current import price)
    conversion_loss_cost = import_price * (conversion_loss / 100)

    return round(depreciation + conversion_loss_cost, 3)

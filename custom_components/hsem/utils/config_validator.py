"""Centralized config validation utilities for the HSEM integration.

All pure (non-HA) validation logic lives here so that flow validators,
coordinator setup, and tests share a single source of truth.

Design principles
-----------------
- Every public function returns a ``dict[str, str]`` of ``{field: error_key}``,
  following the Home Assistant config-flow convention.  An empty dict means
  the input is valid.
- Functions that also need HA state (e.g. checking whether an entity exists)
  are async and accept a ``hass`` parameter — they are clearly named
  ``async_validate_*``.
- Pure sync functions (format / range checks) are plain ``def``.
- No function duplicates logic already in ``utils/misc.py``.  Entity
  existence checks reuse ``async_entity_exists`` / ``async_device_exists``
  from there.
"""

import re
from datetime import datetime as _datetime

from custom_components.hsem.utils.misc import (
    async_device_exists,
    async_entity_exists,
    convert_months_to_int,
)

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# Home Assistant entity IDs must match <domain>.<object_id> where both parts
# contain only lowercase letters, digits, and underscores.
_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

# HA time strings from the "time" selector always arrive as "HH:MM:SS".
_TIME_FORMAT = "%H:%M:%S"


# ---------------------------------------------------------------------------
# Entity ID validation (format only — no HA lookup)
# ---------------------------------------------------------------------------


def validate_entity_id_format(entity_id: str) -> bool:
    """Return True when *entity_id* looks like a valid HA entity ID.

    Checks the ``<domain>.<object_id>`` pattern with lowercase alphanumerics
    and underscores only.  Does **not** verify that the entity actually exists
    in HA — use :func:`async_validate_entity_ids` for that.

    Args:
        entity_id: The entity ID string to validate.

    Returns:
        ``True`` when the format is valid, ``False`` otherwise.
    """
    if not isinstance(entity_id, str) or not entity_id.strip():
        return False
    return bool(_ENTITY_ID_RE.match(entity_id.strip()))


def validate_entity_id_fields(
    user_input: dict,
    required_fields: list[str],
    optional_fields: list[str] | None = None,
) -> dict[str, str]:
    """Validate entity ID format for required and optional config fields.

    Returns a ``{field: error_key}`` dict.  The possible error keys are:

    * ``"required"`` — the required field is absent or empty.
    * ``"invalid_entity_id"`` — the value is present but malformed.

    Args:
        user_input: Dict of field name → value from the config/options form.
        required_fields: Fields that must be present and have a valid format.
        optional_fields: Fields that are checked only when present.

    Returns:
        Dict mapping field names to translation error keys.
    """
    errors: dict[str, str] = {}
    optional = optional_fields or []

    for field in required_fields:
        value = user_input.get(field)
        if not value:
            errors[field] = "required"
        elif not validate_entity_id_format(str(value)):
            errors[field] = "invalid_entity_id"

    for field in optional:
        value = user_input.get(field)
        if value and not validate_entity_id_format(str(value)):
            errors[field] = "invalid_entity_id"

    return errors


# ---------------------------------------------------------------------------
# Entity / device existence checks (requires HA)
# ---------------------------------------------------------------------------


async def async_validate_entity_ids(
    hass,
    user_input: dict,
    required_fields: list[str],
    optional_fields: list[str] | None = None,
) -> dict[str, str]:
    """Check that entity IDs in *user_input* exist in the HA state machine.

    Runs format validation first; only entities with valid format are looked
    up in HA.  This avoids unnecessary registry queries for obviously bad
    values.

    Args:
        hass: Home Assistant instance.
        user_input: Dict of field name → value from the config/options form.
        required_fields: Fields that must exist and resolve in HA.
        optional_fields: Fields checked only when they contain a value.

    Returns:
        Dict mapping field names to translation error keys.
    """
    # Format check first (fast, synchronous)
    errors = validate_entity_id_fields(user_input, required_fields, optional_fields)

    optional = optional_fields or []
    all_lookup_fields = [*required_fields, *optional]

    for field in all_lookup_fields:
        if field in errors:
            # Already flagged by format check — skip HA lookup
            continue
        value = user_input.get(field)
        if not value:
            continue
        if not await async_entity_exists(hass, str(value)):
            errors[field] = "entity_not_found"

    return errors


async def async_validate_device_ids(
    hass,
    user_input: dict,
    required_fields: list[str],
    optional_fields: list[str] | None = None,
) -> dict[str, str]:
    """Check that device IDs in *user_input* exist in the HA device registry.

    Args:
        hass: Home Assistant instance.
        user_input: Dict of field name → value from the config/options form.
        required_fields: Fields that must resolve to a known HA device.
        optional_fields: Fields checked only when they contain a value.

    Returns:
        Dict mapping field names to translation error keys.
    """
    errors: dict[str, str] = {}
    optional = optional_fields or []

    for field in required_fields:
        value = user_input.get(field)
        if not value:
            errors[field] = "required"
        elif not await async_device_exists(hass, str(value)):
            errors[field] = "device_not_found"

    for field in optional:
        value = user_input.get(field)
        if value and not await async_device_exists(hass, str(value)):
            errors[field] = "device_not_found"

    return errors


# ---------------------------------------------------------------------------
# Month validation
# ---------------------------------------------------------------------------


def validate_months(
    user_input: dict,
    winter_field: str = "hsem_months_winter",
) -> dict[str, str]:
    """Validate month configuration.

    Ensures:

    * The winter field is present.
    * All supplied month values are valid integers in [1, 12].
    * At least one month is assigned to each season (winter and summer).

    Args:
        user_input: Dict containing the month config fields.
        winter_field: Key for the winter-months list in *user_input*.

    Returns:
        Dict mapping field names to translation error keys.
    """
    errors: dict[str, str] = {}

    winter_raw = user_input.get(winter_field)
    if not winter_raw:
        errors[winter_field] = "required"
        return errors

    try:
        winter_months = convert_months_to_int(winter_raw)
    except ValueError:
        errors[winter_field] = "invalid_month_value"
        return errors

    if not winter_months:
        errors[winter_field] = "months_winter_empty"
        return errors

    all_months = set(range(1, 13))
    summer_months = all_months - set(winter_months)
    if not summer_months:
        errors[winter_field] = "months_summer_empty"

    return errors


# ---------------------------------------------------------------------------
# Time window validation
# ---------------------------------------------------------------------------


def _parse_time_str(value: str) -> _datetime | None:
    """Parse a ``HH:MM:SS`` string into a :class:`datetime` or return ``None``."""
    try:
        return _datetime.strptime(value, _TIME_FORMAT)
    except (ValueError, TypeError):
        return None


def validate_time_window(
    user_input: dict,
    enabled_field: str,
    start_field: str,
    end_field: str,
) -> dict[str, str]:
    """Validate a single battery-schedule time window.

    Rules:

    * When the schedule is disabled (``enabled_field`` is ``False``), the time
      values are not checked.
    * Start and end must parse as ``HH:MM:SS``.
    * Start and end must not be identical (zero-length window).
    * Cross-midnight windows (start > end) are explicitly **allowed**.

    Args:
        user_input: Dict from the config/options form.
        enabled_field: Field name whose boolean value gates further checks.
        start_field: Field name for the window start time string.
        end_field: Field name for the window end time string.

    Returns:
        Dict mapping field names to translation error keys.
    """
    errors: dict[str, str] = {}

    enabled = user_input.get(enabled_field, False)
    if not enabled:
        return errors

    start_raw = user_input.get(start_field)
    end_raw = user_input.get(end_field)

    start_dt = _parse_time_str(start_raw) if start_raw else None
    end_dt = _parse_time_str(end_raw) if end_raw else None

    if start_dt is None:
        errors[start_field] = "invalid_time_format"
        return errors
    if end_dt is None:
        errors[end_field] = "invalid_time_format"
        return errors

    if start_dt == end_dt:
        errors["base"] = "start_time_equals_end_time"

    return errors


# ---------------------------------------------------------------------------
# Power and energy limit validation
# ---------------------------------------------------------------------------


def validate_power_limits(
    user_input: dict,
    field: str,
    min_watts: float = 0.0,
    max_watts: float = 100_000.0,
) -> dict[str, str]:
    """Validate that a power value (in watts) falls within acceptable bounds.

    Args:
        user_input: Dict from the config/options form.
        field: The field name whose value is being validated.
        min_watts: Inclusive lower bound (default 0 W).
        max_watts: Inclusive upper bound (default 100 kW).

    Returns:
        Dict mapping field names to translation error keys.
    """
    errors: dict[str, str] = {}
    value = user_input.get(field)
    if value is None:
        return errors  # Optional field — absence is not an error here

    try:
        w = float(value)
    except (ValueError, TypeError):
        errors[field] = "invalid_power_value"
        return errors

    if w < min_watts or w > max_watts:
        errors[field] = "power_out_of_range"

    return errors


def validate_energy_limits(
    user_input: dict,
    field: str,
    min_kwh: float = 0.0,
    max_kwh: float = 1_000.0,
) -> dict[str, str]:
    """Validate that an energy value (in kWh) falls within acceptable bounds.

    Args:
        user_input: Dict from the config/options form.
        field: The field name whose value is being validated.
        min_kwh: Inclusive lower bound (default 0 kWh).
        max_kwh: Inclusive upper bound (default 1 000 kWh).

    Returns:
        Dict mapping field names to translation error keys.
    """
    errors: dict[str, str] = {}
    value = user_input.get(field)
    if value is None:
        return errors

    try:
        kwh = float(value)
    except (ValueError, TypeError):
        errors[field] = "invalid_energy_value"
        return errors

    if kwh < min_kwh or kwh > max_kwh:
        errors[field] = "energy_out_of_range"

    return errors


# ---------------------------------------------------------------------------
# Price / cost validation
# ---------------------------------------------------------------------------


def validate_price(
    user_input: dict,
    field: str,
    min_price: float = -100.0,
    max_price: float = 100.0,
    allow_negative: bool = True,
) -> dict[str, str]:
    """Validate a price or cost value.

    Args:
        user_input: Dict from the config/options form.
        field: The field name whose value is being validated.
        min_price: Inclusive lower bound.
        max_price: Inclusive upper bound.
        allow_negative: When ``False`` the value must be ≥ 0.

    Returns:
        Dict mapping field names to translation error keys.
    """
    errors: dict[str, str] = {}
    value = user_input.get(field)
    if value is None:
        return errors

    try:
        price = float(value)
    except (ValueError, TypeError):
        errors[field] = "invalid_price_value"
        return errors

    effective_min = 0.0 if not allow_negative else min_price
    if price < effective_min or price > max_price:
        errors[field] = "price_out_of_range"

    return errors


# ---------------------------------------------------------------------------
# Weighted values validation
# ---------------------------------------------------------------------------


def validate_consumption_weights(user_input: dict) -> dict[str, str]:
    """Validate that the four house-consumption energy weights sum to 100.

    Args:
        user_input: Dict from the config/options form.

    Returns:
        Dict with ``{"base": "hsem_house_consumption_energy_weight_total"}``
        when the weights do not sum to 100, otherwise empty.
    """
    errors: dict[str, str] = {}
    fields = [
        "hsem_house_consumption_energy_weight_1d",
        "hsem_house_consumption_energy_weight_3d",
        "hsem_house_consumption_energy_weight_7d",
        "hsem_house_consumption_energy_weight_14d",
    ]

    try:
        total = sum(int(user_input.get(f, 0)) for f in fields)
    except (ValueError, TypeError):
        errors["base"] = "hsem_house_consumption_energy_weight_total"
        return errors

    if total != 100:
        errors["base"] = "hsem_house_consumption_energy_weight_total"

    return errors


# ---------------------------------------------------------------------------
# Composite validator helpers
# ---------------------------------------------------------------------------


def merge_errors(*error_dicts: dict[str, str]) -> dict[str, str]:
    """Merge multiple error dicts, keeping the first error per field.

    This lets validators be composed without later checks overwriting earlier
    ones for the same field.

    Args:
        *error_dicts: Any number of ``{field: error_key}`` dicts.

    Returns:
        Merged dict with at most one error per field.
    """
    merged: dict[str, str] = {}
    for d in error_dicts:
        for field, key in d.items():
            if field not in merged:
                merged[field] = key
    return merged

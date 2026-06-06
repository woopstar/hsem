"""Type-conversion utilities for Home Assistant sensor states.

Includes helpers for converting raw sensor values into float, int,
month-list, boolean, and time types, with proper handling of
HA sentinel values (``unknown`` / ``unavailable``).
"""

from datetime import datetime, time
from typing import Any

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN


def convert_to_time(time_value: str | time) -> time:
    """Convert a string or ``datetime.time`` to a ``datetime.time`` object.

    Args:
        time_value: A time string in ``HH:MM:SS`` format or a ``datetime.time``.

    Returns:
        The converted ``datetime.time`` object.
    """
    if isinstance(time_value, time):
        return time_value

    if isinstance(time_value, str):
        return datetime.strptime(time_value, "%H:%M:%S").time()

    return time()


def convert_to_float(state: Any) -> float | None:
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
        if stripped == "" or stripped.lower() in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            return float(stripped)
        except ValueError, TypeError:
            return None

    try:
        return float(state)
    except ValueError, TypeError:
        return None


def convert_to_int(state: Any) -> int | None:
    """Cast *state* to an integer, distinguishing real zero from invalid input.

    Returns:
        ``int`` when *state* is a valid numeric value (including ``0``).
        ``None`` when *state* is ``None``, a HA sentinel (``"unknown"``,
        ``"unavailable"``), an empty string, or any non-numeric text.
        This mirrors the behaviour of ``convert_to_float`` and ensures that
        defective config values or missing sensor readings are visible to the
        caller rather than silently replaced with ``0``.
    """
    if state is None:
        return None

    if isinstance(state, str):
        stripped = state.strip().lower()
        if stripped in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
            return None
        try:
            return int(float(stripped))
        except ValueError, TypeError:
            return None

    try:
        return int(state)
    except ValueError, TypeError:
        return None


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


def convert_to_boolean(state: Any) -> bool:
    """Resolve an input sensor state and cast it to a boolean.

    Handles booleans, integers, and a wide range of string values
    (``"on"``, ``"off"``, ``"charging"``, ``"connected"``, etc.).

    Args:
        state: The raw sensor state value.

    Returns:
        The resolved boolean value.  Returns False for unrecognised inputs.
    """

    if state is None:
        return False

    if isinstance(state, bool):
        return state

    if isinstance(state, int):
        return state != 0

    state_map = {
        STATE_ON: True,
        "true": True,
        "1": True,
        STATE_OFF: False,
        "false": False,
        "0": False,
        "charging": True,
        "not_charging": False,
        "notcharging": False,
        STATE_UNKNOWN: False,
        "available": True,
        STATE_UNAVAILABLE: False,
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

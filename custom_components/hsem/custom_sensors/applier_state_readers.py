"""Entity-state readers used by the applier.

Extracted from ``applier.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: no behaviour change.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.hsem.utils.huawei import (
    extract_tou_periods,
)

# ---------------------------------------------------------------------------
# Read-back helpers (pure — no side effects)
# ---------------------------------------------------------------------------


def _read_number_state(
    sensor: Any, entity_id: str | None
) -> float | None:  # NOSONAR -- HA internal type; circular import risk
    """Read a number entity state from HA and return it as float, or None.

    Args:
        sensor: HSEM sensor instance with a ``hass`` attribute.
        entity_id: HA entity ID to read.

    Returns:
        Current numeric state, or ``None`` when the entity is unavailable.
    """
    if not entity_id:
        return None
    state = sensor.hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
        return None
    try:
        return float(state.state)
    except ValueError, TypeError:
        return None


def _read_tou_periods(
    sensor: Any, entity_id: str | None
) -> list[str] | None:  # NOSONAR -- HA internal type; circular import risk
    """Read the live TOU schedule from HA and return it as a period list.

    The schedule lives in the entity's ``Period 1``…``Period 10`` attributes.
    The entity *state* is only the number of configured periods, so it can
    never be used to verify a written schedule.  This reads HA directly rather
    than :class:`LiveState`, whose snapshot is captured before the write and
    would therefore never reflect it.

    Args:
        sensor: HSEM sensor instance with a ``hass`` attribute.
        entity_id: HA entity ID to read.

    Returns:
        Current period strings, or ``None`` when the entity is unavailable.
    """
    if not entity_id:
        return None
    state = sensor.hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
        return None
    return extract_tou_periods(state.attributes)


def _read_select_state(
    sensor: Any, entity_id: str | None
) -> str | None:  # NOSONAR -- HA internal type; circular import risk
    """Read a select entity state from HA and return it as a string, or None.

    Args:
        sensor: HSEM sensor instance with a ``hass`` attribute.
        entity_id: HA entity ID to read.

    Returns:
        Current option string, or ``None`` when unavailable.
    """
    if not entity_id:
        return None
    state = sensor.hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
        return None
    return str(state.state)


def _parse_power_control_pct(state: str | None) -> int | None:
    """Parse the inverter active power control state string into a numeric value.

    Handles both percentage (``"Limited to 80%"`` → 80) and watt-based
    (``"Limited to 100W"`` → 100) formats.  ``"Unlimited"`` returns 100.

    Args:
        state: Raw string from the inverter entity (e.g. ``"Unlimited"``,
               ``"Limited to 80%"``, or ``"Limited to 100W"``).

    Returns:
        Integer value (percentage or watts), or ``None`` if the string
        cannot be parsed.
    """
    if not isinstance(state, str):
        return None
    normalized = state.strip().lower()
    # Accept any locale-independent representation of "unlimited" / no cap.
    if normalized in (
        "unlimited",
        "ikke begrænset",
        "onbeperkt",
        "unbegrenzt",
        "illimitato",
        "sin límite",
        "không giới hạn",
    ):
        return 100
    # Extract the numeric value regardless of surrounding translated text or
    # unit suffix (% or W).  This handles patterns like:
    #   "Limited to 80%"   →  80
    #   "Limited to 100W"  →  100
    #   "Begrenzt auf 80 %"  →  80
    #   "Beperkt tot 80%"  →  80
    match = re.search(r"(-?\d+(?:\.\d+)?)", normalized)
    if match:
        try:
            return int(round(float(match.group(1))))
        except ValueError, TypeError:
            pass
    return None


def _is_watt_limit(state: str | None) -> bool:
    """Check if the power control state represents a watt-based limit.

    Args:
        state: Raw string from the inverter entity (e.g. ``"Limited to 100W"``
               or ``"Limited to 80%"``).

    Returns:
        ``True`` if the state is a watt-based limit, ``False`` otherwise
        (percentage-based or unlimited).
    """
    if not isinstance(state, str):
        return False
    normalized = state.strip().lower()
    # Unlimited / percentage-based states never contain a watt indicator
    if normalized in (
        "unlimited",
        "ikke begrænset",
        "onbeperkt",
        "unbegrenzt",
        "illimitato",
        "sin límite",
        "không giới hạn",
    ):
        return False
    # Look for a number immediately followed (with optional whitespace) by "w"
    # Single quantifier avoids polynomial backtracking from stacked greedy quantifiers
    return bool(re.search(r"\d[\d\s]*w", normalized))

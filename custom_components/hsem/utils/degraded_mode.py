"""Degraded-mode classification for HSEM.

HSEM operates in one of three health states:

``OK``
    All required entities are present and readable.  Hardware writes are
    permitted.

``Degraded``
    Non-critical data is missing (e.g. price feed unavailable).  Read-only
    calculations continue on best-effort values.  Hardware writes are still
    permitted because the battery SoC and capacity data are intact — the
    planner can still make a safe decision.

``Error``
    One or more *critical* entities are missing or unreadable (battery SoC,
    rated capacity, max charge/discharge power, or the house consumption
    power sensor).  Hardware writes are **blocked** to avoid acting on
    incomplete state.

Design notes
------------
- The labels below intentionally match the ``status`` strings that
  ``working_mode_sensor.extra_state_attributes`` has historically exposed
  (``"ok"``, ``"degraded"``, ``"error"``).  This keeps dashboards
  and automations compatible with the old string attribute while the new
  sensor provides a proper HA state.
- Criticality is determined by :func:`classify_degraded_mode`; callers must
  not hard-code the set of critical fields because it may grow over time.
"""

from __future__ import annotations

from enum import Enum


class DegradedMode(Enum):
    """Health-state classification for one HSEM update cycle."""

    OK = "ok"
    """All required entities are readable.  Normal operation."""

    Degraded = "degraded"
    """Non-critical data missing.  Read-only calculations continue; writes allowed."""

    Error = "error"
    """Critical data missing.  Hardware writes are blocked."""


# ---------------------------------------------------------------------------
# Labels that map an entity description keyword to "critical"
#
# The state_collector records labels like:
#   "Error reading <label> (entity_id=...)"  or  "Missing entity: <label>"
#
# We treat an entity as critical when its label contains one of these
# substrings (case-insensitive).  Non-critical absences produce Degraded.
# ---------------------------------------------------------------------------
_CRITICAL_KEYWORDS: frozenset[str] = frozenset(
    {
        # Battery hardware — must know these to avoid damaging the pack
        "batteries_state_of_capacity",
        "batteries_maximum_charging_power",
        "batteries_maximum_discharging_power",
        "batteries_rated_capacity",
        # House load — needed to compute net consumption correctly
        "house_consumption_power",
    }
)


def classify_degraded_mode(
    missing_entities: bool,
    missing_entities_list: list[str],
) -> DegradedMode:
    """Return the appropriate :class:`DegradedMode` for the current cycle.

    Args:
        missing_entities: True when ``LiveState.missing_entities`` is set.
        missing_entities_list: Human-readable labels of missing/errored
            entities, as recorded by ``LiveState.add_missing_entity``.

    Returns:
        :attr:`DegradedMode.OK` when no entities are missing.
        :attr:`DegradedMode.Error` when any *critical* entity is missing.
        :attr:`DegradedMode.Degraded` when only non-critical entities are missing.
    """
    if not missing_entities:
        return DegradedMode.OK

    for label in missing_entities_list:
        label_lower = label.lower()
        if any(kw in label_lower for kw in _CRITICAL_KEYWORDS):
            return DegradedMode.Error

    return DegradedMode.Degraded


def hardware_writes_allowed(mode: DegradedMode) -> bool:
    """Return True when hardware writes are safe to execute.

    Only :attr:`DegradedMode.OK` and :attr:`DegradedMode.Degraded` permit
    writes.  :attr:`DegradedMode.Error` blocks all inverter / battery writes
    because critical sensor data is absent.

    Args:
        mode: The current :class:`DegradedMode`.

    Returns:
        True if writes are allowed, False if they must be blocked.
    """
    return mode is not DegradedMode.Error

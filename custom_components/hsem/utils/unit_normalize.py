"""Generic sensor unit-normalization utility for user-configured entities.

HSEM lets users point many config-flow fields at arbitrary ``sensor``-domain
entities with no ``device_class`` restriction (see ``flows/energy_and_ml.py``,
``flows/power.py``). Home Assistant only auto-converts a sensor's displayed
value to the configured unit system when the entity has a proper
``device_class``; template sensors and many integrations do not, so the raw
``state.state`` value can be in whatever unit the source reports, without
HSEM ever checking.

This module provides a single, reusable normalizer that delegates to Home
Assistant's own :mod:`homeassistant.util.unit_conversion` converters — the
same mechanism HA itself uses for device-class auto-conversion — rather than
hand-rolling per-unit multipliers (issue #945).

Related, narrower fixes for the same class of problem:

- Issue #592 fixed this for EV charger power with a one-off,
  charging-state-aware normalizer (:func:`normalize_ev_power_w` in
  :mod:`custom_components.hsem.utils.conversion`). That function's
  plausibility checks (implausibly-high / suspiciously-low readings *while
  charging*) are tied to EV charging state, which this generic utility has
  no concept of, so it intentionally stays separate rather than being
  migrated onto :func:`normalize_to_unit`.
- PR #944 fixed this for wind speed and forecast temperature by reading
  each entity's declared unit and converting via ``TemperatureConverter`` /
  ``SpeedConverter`` directly in the feature-specific reader.
  :func:`normalize_to_unit` generalises that pattern for any HSEM field
  with a known canonical unit.
"""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import (
    BaseUnitConverter,
    EnergyConverter,
    PowerConverter,
    SpeedConverter,
    TemperatureConverter,
)

from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

# Every converter class HSEM normalizes through. Extend this tuple (never
# hand-roll a new per-unit multiplier) when a new canonical unit family is
# required, e.g. EnergyConverter for kWh-expecting meter fields.
_CONVERTER_CLASSES: tuple[type[BaseUnitConverter], ...] = (
    TemperatureConverter,
    PowerConverter,
    EnergyConverter,
    SpeedConverter,
)

# Maps every unit string each converter recognises to that converter class,
# so looking up the canonical unit alone is enough to find the right one.
_CONVERTER_BY_UNIT: dict[str, type[BaseUnitConverter]] = {
    unit: converter
    for converter in _CONVERTER_CLASSES
    for unit in converter.VALID_UNITS
    if unit is not None
}


def normalize_to_unit(
    value: float | None,
    source_unit: str | None,
    canonical_unit: str,
    *,
    entity_id: str = "",
    label: str = "",
) -> float | None:
    """Normalize *value* from *source_unit* to HSEM's *canonical_unit*.

    Delegates to Home Assistant's own ``unit_conversion`` converters
    (:class:`TemperatureConverter`, :class:`PowerConverter`,
    :class:`EnergyConverter`, :class:`SpeedConverter`) rather than
    hand-rolled per-unit multipliers.

    Args:
        value: The raw numeric reading, or ``None`` when unavailable.
        source_unit: The entity's declared unit (``unit_of_measurement`` or
            ``native_unit_of_measurement``), or ``None``/empty when the
            entity does not declare one (common for template sensors).
        canonical_unit: HSEM's expected unit for this field (e.g.
            ``UnitOfTemperature.CELSIUS``, ``UnitOfPower.WATT``).
        entity_id: Entity the reading came from (for log messages).
        label: Human-readable label used in log messages.

    Returns:
        *value* converted to *canonical_unit*. Returns *value* unchanged
        when no unit is declared, the units already match, or no known
        conversion exists between them (a misconfiguration is logged, not
        raised). ``None`` when *value* is ``None``.
    """
    if value is None:
        return None

    if not source_unit:
        _LOGGER.debug(
            "Unit normalize: %s (%s) has no declared unit; assuming it "
            "already reports in %s.",
            entity_id or "<unknown entity>",
            label or "value",
            canonical_unit,
        )
        return value

    if source_unit == canonical_unit:
        return value

    converter = _CONVERTER_BY_UNIT.get(canonical_unit)
    if converter is None or source_unit not in converter.VALID_UNITS:
        _LOGGER.info(
            "Unit normalize: %s (%s) reports unit '%s'; HSEM has no known "
            "conversion from it to '%s'. Using the raw value unchanged — "
            "verify the sensor's unit is correct.",
            entity_id or "<unknown entity>",
            label or "value",
            source_unit,
            canonical_unit,
        )
        return value

    try:
        converted = converter.convert(value, source_unit, canonical_unit)
    except HomeAssistantError:
        _LOGGER.warning(
            "Unit normalize: failed to convert %s (%s) from '%s' to '%s'; "
            "using the raw value unchanged.",
            entity_id or "<unknown entity>",
            label or "value",
            source_unit,
            canonical_unit,
        )
        return value

    if abs(converted - value) > 1e-9:
        _LOGGER.debug(
            "Unit normalize: converted %s (%s) from %.4f %s to %.4f %s.",
            entity_id or "<unknown entity>",
            label or "value",
            value,
            source_unit,
            converted,
            canonical_unit,
        )

    return converted

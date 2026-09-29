"""Live per-phase grid inputs for the main-fuse check (issue #1119, 6.3.x).

The phase-aware grid-charge limiter (issue #831) compares each phase with the
main fuse in Watts (``fuse_amps × 230 V``). A fuse trips on current, so each
phase field may be a power sensor (W, kW, ...) or a current sensor (A, mA).
Before issue #1119 every phase field went through the Watt normalisation, and
a current reading passed through it unchanged: 16 A was read as 16 W, and the
fuse guard was effectively off.

Each reading is returned in Watts:

- **Power** is converted to W.
- **Current** becomes ``|I| × 230 V``. A current reading carries no direction,
  so it counts as import, the conservative case for the fuse. Against the
  limiter's ``fuse_amps × 230 V`` limit this is the same comparison in amps.
- **A missing or unrecognised unit** gives ``None``. The limiter's
  ``phase_powers_valid`` check then fails closed (0 W grid charge).

This is the 6.3.x subset of the ``main`` fix: it has no voltage sensors, and
there is no switchable-EV phase hold on this line.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from time import monotonic
from typing import Any

from homeassistant.const import UnitOfElectricCurrent, UnitOfPower
from homeassistant.util.unit_conversion import ElectricCurrentConverter, PowerConverter

from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.units import GRID_PHASE_VOLTAGE

#: The unusable-unit WARNING repeats at most this often per entity and unit.
PHASE_UNIT_WARNING_INTERVAL_S = 3600.0


def phase_reading_to_power_w(value: float | None, unit: str | None) -> float | None:
    """Return one phase reading in Watts, or ``None`` when it is unusable.

    Args:
        value: The raw numeric reading.
        unit: The entity's ``unit_of_measurement``.

    Returns:
        Signed Watts for a power reading, ``|I| × 230 V`` for a current
        reading, and ``None`` for a missing value or a missing or unknown unit.
    """
    if value is None or not math.isfinite(value) or not unit:
        return None
    if unit in PowerConverter.VALID_UNITS:
        return PowerConverter.convert(value, unit, UnitOfPower.WATT)
    if unit in ElectricCurrentConverter.VALID_UNITS:
        amps = ElectricCurrentConverter.convert(
            value, unit, UnitOfElectricCurrent.AMPERE
        )
        return abs(amps) * GRID_PHASE_VOLTAGE
    return None


def read_grid_phase_power_w(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    cfg: SensorConfig,
    reader: Callable[..., Any],
) -> tuple[float | None, float | None, float | None]:
    """Read the three live phase inputs, each in Watts.

    Args:
        sensor: The collecting component; must expose ``.hass``. It also
            holds the unusable-unit warning throttle.
        cfg: Current sensor configuration.
        reader: Read closure ``(entity_id, "float", label=...) -> Any`` that
            returns ``None`` for an unconfigured or unreadable entity.

    Returns:
        ``(phase_a, phase_b, phase_c)`` in Watts; see
        :func:`phase_reading_to_power_w`.
    """
    return (
        _read_phase_power_w(
            sensor,
            cfg.huawei_solar_power_meter_phase_a_active_power,
            reader,
            "power_meter_phase_a_active_power",
        ),
        _read_phase_power_w(
            sensor,
            cfg.huawei_solar_power_meter_phase_b_active_power,
            reader,
            "power_meter_phase_b_active_power",
        ),
        _read_phase_power_w(
            sensor,
            cfg.huawei_solar_power_meter_phase_c_active_power,
            reader,
            "power_meter_phase_c_active_power",
        ),
    )


def _read_phase_power_w(
    sensor: Any,
    entity_id: str | None,
    reader: Callable[..., Any],
    label: str,
) -> float | None:
    """Return one phase in Watts, or ``None`` (warning on an unusable unit)."""
    value = convert_to_float(reader(entity_id, "float", label=label))
    if value is None or entity_id is None:
        return None
    hass = getattr(sensor, "hass", None)
    state = hass.states.get(entity_id) if hass is not None else None
    unit = state.attributes.get("unit_of_measurement") if state is not None else None
    power_w = phase_reading_to_power_w(value, unit)
    if power_w is None:
        _warn_unusable_unit(sensor, entity_id, label, unit)
    return power_w


def _warn_unusable_unit(
    sensor: Any, entity_id: str, label: str, unit: str | None
) -> None:
    """Log a rate-limited WARNING for a phase reading HSEM cannot interpret."""
    warned_at = getattr(sensor, "_phase_unit_warned_at", None)
    if not isinstance(warned_at, dict):
        warned_at = {}
        sensor._phase_unit_warned_at = warned_at
    key = (entity_id, unit or "")
    now = monotonic()
    last = warned_at.get(key)
    if last is not None and now - last < PHASE_UNIT_WARNING_INTERVAL_S:
        return
    warned_at[key] = now
    _LOGGER.warning(
        "Live phase input %s (%s) reports unit '%s', which is neither power "
        "(W, kW) nor current (A), so it cannot be checked against the main "
        "fuse. The phase is treated as unavailable: phase-aware grid charging "
        "stays blocked until the sensor is fixed",
        entity_id,
        label,
        unit or "none",
    )

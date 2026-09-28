"""Live per-phase grid inputs for the main-fuse safety checks (issue #1119).

Two live checks compare each phase with the main fuse: the Huawei grid-charge
limiter (issue #831) and the switchable-EV one-phase hold (issue #1083). A
fuse trips on current, so each phase field may be a power sensor (W, kW, ...)
or a current sensor (A, mA). The reading keeps its unit family, and
:func:`~custom_components.hsem.utils.phase_power.phase_fuse_headroom_a`
compares it with the fuse in amps.

A reading with a missing or unrecognised unit becomes ``None``, so both checks
fail closed. Passing it through as Watts, as before issue #1119, read 16 A as
16 W and silently disabled the guard.
"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import Any

from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
)

from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.ha_helpers import entity_unit, read_normalized_float
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.phase_power import (
    PhaseReading,
    PhaseReadings,
    PhaseVoltages,
)
from custom_components.hsem.utils.unit_normalize import normalize_to_unit_family

#: The unusable-unit WARNING repeats at most this often per entity and unit.
PHASE_UNIT_WARNING_INTERVAL_S = 3600.0

_PHASE_UNITS = (UnitOfPower.WATT, UnitOfElectricCurrent.AMPERE)


def read_grid_phase_inputs(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    cfg: SensorConfig,
    reader: Callable[..., Any],
) -> tuple[PhaseReadings, PhaseVoltages]:
    """Read the three live phase readings and their optional voltages.

    Args:
        sensor: The working-mode sensor, used for ``hass`` access and to hold
            the unusable-unit warning throttle.
        cfg: Current sensor configuration.
        reader: Read closure ``(entity_id, "float", label=...) -> Any`` that
            returns ``None`` for an unconfigured or unreadable entity.

    Returns:
        ``(readings, voltages)``, each ordered ``(phase_a, phase_b, phase_c)``.
    """
    readings: PhaseReadings = (
        _read_phase_reading(
            sensor,
            cfg.huawei_solar_power_meter_phase_a_active_power,
            reader,
            "power_meter_phase_a_active_power",
        ),
        _read_phase_reading(
            sensor,
            cfg.huawei_solar_power_meter_phase_b_active_power,
            reader,
            "power_meter_phase_b_active_power",
        ),
        _read_phase_reading(
            sensor,
            cfg.huawei_solar_power_meter_phase_c_active_power,
            reader,
            "power_meter_phase_c_active_power",
        ),
    )
    voltages: PhaseVoltages = (
        read_normalized_float(
            sensor,
            cfg.huawei_solar_power_meter_phase_a_voltage,
            reader,
            UnitOfElectricPotential.VOLT,
            label="power_meter_phase_a_voltage",
        ),
        read_normalized_float(
            sensor,
            cfg.huawei_solar_power_meter_phase_b_voltage,
            reader,
            UnitOfElectricPotential.VOLT,
            label="power_meter_phase_b_voltage",
        ),
        read_normalized_float(
            sensor,
            cfg.huawei_solar_power_meter_phase_c_voltage,
            reader,
            UnitOfElectricPotential.VOLT,
            label="power_meter_phase_c_voltage",
        ),
    )
    return readings, voltages


def _read_phase_reading(
    sensor: Any,
    entity_id: str | None,
    reader: Callable[..., Any],
    label: str,
) -> PhaseReading | None:
    """Return one phase as a power or current reading, or ``None``."""
    value = convert_to_float(reader(entity_id, "float", label=label))
    if value is None or entity_id is None:
        return None
    unit = entity_unit(sensor, entity_id)
    normalized = normalize_to_unit_family(value, unit, _PHASE_UNITS)
    if normalized is None:
        _warn_unusable_unit(sensor, entity_id, label, unit)
        return None
    return PhaseReading(value=normalized[0], unit=normalized[1])


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
        "and the switchable-EV one-phase hold stay blocked until the sensor "
        "is fixed",
        entity_id,
        label,
        unit or "none",
    )

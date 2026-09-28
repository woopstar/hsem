"""Shared builders for live per-phase grid readings (issue #1119)."""

from __future__ import annotations

from homeassistant.const import UnitOfElectricCurrent, UnitOfPower

from custom_components.hsem.utils.phase_power import PhaseReading, PhaseReadings


def _readings(
    unit: str, a: float | None, b: float | None, c: float | None
) -> PhaseReadings:
    """Return three readings in *unit*; ``None`` stays an unavailable phase."""
    return (
        None if a is None else PhaseReading(a, unit),
        None if b is None else PhaseReading(b, unit),
        None if c is None else PhaseReading(c, unit),
    )


def watts(a: float | None, b: float | None, c: float | None) -> PhaseReadings:
    """Return signed per-phase power readings in Watts."""
    return _readings(UnitOfPower.WATT, a, b, c)


def amps(a: float | None, b: float | None, c: float | None) -> PhaseReadings:
    """Return per-phase current readings in amps."""
    return _readings(UnitOfElectricCurrent.AMPERE, a, b, c)

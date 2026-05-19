"""Explicit unit-conversion helpers for HSEM (issue #290).

This module provides named conversion functions that make W/kW, Wh/kWh,
and price-per-unit transformations explicit and auditable.  Every function
is a pure one-liner — the value is in the *name*, not the arithmetic.

All functions accept ``int`` or ``float`` and return ``float``.

Usage
-----
>>> from custom_components.hsem.utils.units import (
...     watt_to_kilowatt, kilowatt_to_watt,
...     watthours_to_kilowatthours, kilowatthours_to_watthours,
...     power_to_energy_kwh, energy_to_power_kw,
... )
>>>
>>> watt_to_kilowatt(5000.0)          # 5000 W → 5.0 kW
5.0
>>> kilowatt_to_watt(5.0)              # 5.0 kW → 5000 W
5000.0
>>> watthours_to_kilowatthours(10000.0)  # 10000 Wh → 10.0 kWh
10.0
>>> power_to_energy_kwh(power_kw=5.0, duration_h=2.0)  # 5 kW × 2 h → 10 kWh
10.0
>>> energy_to_power_kw(energy_kwh=10.0, duration_h=2.0)  # 10 kWh ÷ 2 h → 5 kW
5.0
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Power conversions (W ↔ kW)
# ---------------------------------------------------------------------------


def watt_to_kilowatt(power_w: float) -> float:
    """Convert Watts to kiloWatts.

    Args:
        power_w: Power in Watts (W).

    Returns:
        Power in kiloWatts (kW).
    """
    return power_w / 1000.0


def kilowatt_to_watt(power_kw: float) -> float:
    """Convert kiloWatts to Watts.

    Args:
        power_kw: Power in kiloWatts (kW).

    Returns:
        Power in Watts (W).
    """
    return power_kw * 1000.0


# ---------------------------------------------------------------------------
# Energy conversions (Wh ↔ kWh)
# ---------------------------------------------------------------------------


def watthours_to_kilowatthours(energy_wh: float) -> float:
    """Convert Watt-hours to kiloWatt-hours.

    Args:
        energy_wh: Energy in Watt-hours (Wh).

    Returns:
        Energy in kiloWatt-hours (kWh).
    """
    return energy_wh / 1000.0


def kilowatthours_to_watthours(energy_kwh: float) -> float:
    """Convert kiloWatt-hours to Watt-hours.

    Args:
        energy_kwh: Energy in kiloWatt-hours (kWh).

    Returns:
        Energy in Watt-hours (Wh).
    """
    return energy_kwh * 1000.0


# ---------------------------------------------------------------------------
# Duration-aware conversions (power ⇄ energy)
# ---------------------------------------------------------------------------


def power_to_energy_kwh(power_kw: float, duration_h: float) -> float:
    """Convert power over a duration to energy.

    ``energy_kwh = power_kw × duration_h``

    Args:
        power_kw: Average power in kiloWatts (kW).
        duration_h: Duration in hours (h).

    Returns:
        Energy in kiloWatt-hours (kWh).
    """
    return power_kw * duration_h


def energy_to_power_kw(energy_kwh: float, duration_h: float) -> float:
    """Convert energy over a duration to average power.

    ``power_kw = energy_kwh ÷ duration_h``

    Args:
        energy_kwh: Energy in kiloWatt-hours (kWh).
        duration_h: Duration in hours (h).

    Returns:
        Average power in kiloWatts (kW).
    """
    return energy_kwh / duration_h


# ---------------------------------------------------------------------------
# Price / cost helpers
# ---------------------------------------------------------------------------


def energy_cost(energy_kwh: float, price_per_kwh: float) -> float:
    """Compute the monetary cost of a given amount of energy.

    ``cost = energy_kwh × price_per_kwh``

    Args:
        energy_kwh: Energy in kiloWatt-hours (kWh).
        price_per_kwh: Price per kiloWatt-hour (local currency/kWh).

    Returns:
        Monetary cost in local currency.
    """
    return energy_kwh * price_per_kwh


def implied_price_per_kwh(total_cost: float, energy_kwh: float) -> float:
    """Compute the implied average price from a total cost and energy.

    ``price_per_kwh = total_cost ÷ energy_kwh``

    Args:
        total_cost: Total monetary cost (local currency).
        energy_kwh: Energy in kiloWatt-hours (kWh).

    Returns:
        Implied average price per kWh.  Returns ``0.0`` if *energy_kwh* is
        zero or negative (to avoid division-by-zero or nonsensical results).
    """
    if energy_kwh <= 0.0:
        return 0.0
    return total_cost / energy_kwh

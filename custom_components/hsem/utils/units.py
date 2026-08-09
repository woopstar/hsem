"""Explicit unit-conversion helpers for HSEM (issue #290).

This module provides named conversion functions that make W/kW, Wh/kWh,
price-per-unit transformations, duration conversions, and battery economics
explicit and auditable.  Every function is a pure one-liner — the value is
in the *name*, not the arithmetic.

All functions accept ``int`` or ``float`` and return ``float``.

Usage
-----
>>> from custom_components.hsem.utils.units import (
...     watt_to_kilowatt, kilowatt_to_watt,
...     watthours_to_kilowatthours, kilowatthours_to_watthours,
...     power_to_energy_kwh, energy_to_power_kw,
...     timedelta_to_hours, slot_duration_hours, hours_ahead,
...     roundtrip_loss_pct, usable_kwh_from_rated,
...     max_energy_per_slot_kwh, fuse_max_energy_per_slot_kwh,
...     ev_dc_to_ac_kwh, ev_ac_to_dc_kwh,
... )
>>>
>>> watt_to_kilowatt(5000.0)          # 5000 W → 5.0 kW
5.0
>>> timedelta_to_hours(timedelta(minutes=90))  # 90 min → 1.5 h
1.5
>>> roundtrip_loss_pct(97.0, 97.0)   # (1-0.97*0.97)*100
5.91
"""

from __future__ import annotations

from datetime import datetime, timedelta

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
# Duration conversions
# ---------------------------------------------------------------------------


def timedelta_to_hours(td: timedelta) -> float:
    """Convert a ``timedelta`` to hours.

    Canonical replacement for ``td.total_seconds() / 3600.0``.

    Args:
        td: Time duration.

    Returns:
        Duration in hours (h).
    """
    return td.total_seconds() / 3600.0


def slot_duration_hours(slot_start: datetime, slot_end: datetime) -> float:
    """Return the duration of a time slot in hours.

    Canonical replacement for ``(end - start).total_seconds() / 3600.0``.

    Args:
        slot_start: Slot start time.
        slot_end: Slot end time.

    Returns:
        Slot duration in hours (h).
    """
    return timedelta_to_hours(slot_end - slot_start)


def hours_ahead(now: datetime, future_time: datetime) -> float:
    """Return the hours from *now* to *future_time* (≥ 0).

    Canonical replacement for
    ``max((future_time - now).total_seconds() / 3600.0, 0.0)``.

    Args:
        now: Current time (timezone-aware).
        future_time: Target time (same timezone as *now*).

    Returns:
        Non-negative hours ahead.
    """
    return max(timedelta_to_hours(future_time - now), 0.0)


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
    if duration_h <= 0:
        return 0.0

    return energy_kwh / duration_h


# ---------------------------------------------------------------------------
# Battery / efficiency helpers
# ---------------------------------------------------------------------------


def roundtrip_loss_pct(
    charge_efficiency_pct: float,
    discharge_efficiency_pct: float,
) -> float:
    """Return the roundtrip energy loss as a percentage (0–100).

    ``loss_pct = (1 − (charge_eff × discharge_eff)) × 100``

    Replacement for the ``(1.0 - cd * dd) * 100.0`` pattern.

    Args:
        charge_efficiency_pct: Charge-side efficiency (0–100).
        discharge_efficiency_pct: Discharge-side efficiency (0–100).

    Returns:
        Roundtrip loss in percentage points (e.g. 5.91 for 97 % each way).
    """
    return (
        1.0 - (charge_efficiency_pct / 100.0) * (discharge_efficiency_pct / 100.0)
    ) * 100.0


def usable_kwh_from_rated(
    rated_capacity_kwh: float,
    min_soc_pct: float,
    max_soc_pct: float,
) -> float:
    """Return usable battery capacity in kWh given a DoD window.

    ``usable = rated × (max_soc_pct − min_soc_pct) / 100``

    Replacement for the ``rated * (max-min) / 100`` pattern.

    Args:
        rated_capacity_kwh: Nameplate battery capacity (kWh).
        min_soc_pct: Minimum allowed SoC as a percentage (0–100).
        max_soc_pct: Maximum allowed SoC as a percentage (0–100).

    Returns:
        Usable capacity in kWh.
    """
    if rated_capacity_kwh <= 0 or min_soc_pct >= max_soc_pct:
        return 0.0
    return rated_capacity_kwh * (max_soc_pct - min_soc_pct) / 100.0


def max_energy_per_slot_kwh(
    power_w: float,
    interval_minutes: int,
    efficiency_fraction: float = 1.0,
) -> float:
    """Return the maximum energy (kWh) deliverable in one planner slot.

    ``energy = (power_w / 1000 × efficiency) / (60 / interval_minutes)``

    Replacement for the
    ``(power_w / 1000 * eff) / (60 / interval_minutes)`` pattern used for
    max charge/discharge per slot.

    Args:
        power_w: Maximum power in Watts (W).
        interval_minutes: Planner interval length (minutes).
        efficiency_fraction: Efficiency as a fraction (0–1).  Default 1.0
            for no efficiency adjustment.

    Returns:
        Maximum energy per slot in kWh.
    """
    if power_w <= 0 or interval_minutes <= 0:
        return 0.0
    slot_hours = interval_minutes / 60.0
    return watt_to_kilowatt(power_w) * efficiency_fraction * slot_hours


# ---------------------------------------------------------------------------
# Grid fuse limit helpers
# ---------------------------------------------------------------------------

# Nominal per-phase grid voltage (V).  Used by the main-fuse import limit.
GRID_PHASE_VOLTAGE = 230.0


def fuse_max_energy_per_slot_kwh(
    amps: float,
    phases: int,
    slot_hours: float,
) -> float:
    """Return the maximum grid-import energy (kWh) a main fuse allows per slot.

    ``energy = amps × 230 V × phases / 1000 (kW) × slot_hours (h)``

    Single source of truth for the main-fuse import cap.  Used by both the
    MILP grid-import constraint and the post-hoc EV/battery throttle so the
    optimiser and the safety clamp can never disagree about the limit.

    Args:
        amps: Main fuse/breaker rating in amps (A).
        phases: Electrical phase count (1 or 3).
        slot_hours: Slot duration in hours (h).

    Returns:
        Maximum import energy per slot in kWh.  ``0.0`` when the fuse is
        disabled (``amps <= 0``).
    """
    if amps <= 0 or phases <= 0 or slot_hours <= 0:
        return 0.0
    return amps * GRID_PHASE_VOLTAGE * float(phases) / 1000.0 * slot_hours


def export_max_energy_per_slot_kwh(
    power_kw: float,
    slot_hours: float,
) -> float:
    """Return the maximum grid-export energy (kWh) an export cap allows per slot.

    ``energy = power_kw (kW) × slot_hours (h)``

    Single source of truth for the DNO/inverter grid-export power cap
    (issue #726).  Used by the MILP grid-export bound so the optimiser
    never plans export above the physically enforceable site limit.

    Args:
        power_kw: Maximum grid export power in kW (0 = disabled).
        slot_hours: Slot duration in hours (h).

    Returns:
        Maximum export energy per slot in kWh.  ``0.0`` when the cap is
        disabled (``power_kw <= 0``).
    """
    if power_kw <= 0 or slot_hours <= 0:
        return 0.0
    return power_kw * slot_hours


# ---------------------------------------------------------------------------
# EV charger DC ↔ AC conversion helpers
# ---------------------------------------------------------------------------


def ev_dc_to_ac_kwh(dc_kwh: float, charger_efficiency: float) -> float:
    """Convert EV-battery-side (DC) energy to the AC load the house must supply.

    ``ac_kwh = dc_kwh ÷ charger_efficiency``

    Args:
        dc_kwh: Energy delivered to the EV battery (kWh, DC side).
        charger_efficiency: Charger efficiency as a fraction (0–1).

    Returns:
        AC energy drawn from the house/grid/PV (kWh).  Returns ``0.0`` when
        *charger_efficiency* is not positive (avoids division by zero).
    """
    if charger_efficiency <= 0:
        return 0.0
    return dc_kwh / charger_efficiency


def ev_ac_to_dc_kwh(ac_kwh: float, charger_efficiency: float) -> float:
    """Convert EV AC load to the energy delivered to the EV battery (DC).

    ``dc_kwh = ac_kwh × charger_efficiency``

    Args:
        ac_kwh: AC energy drawn from the house/grid/PV (kWh).
        charger_efficiency: Charger efficiency as a fraction (0–1).

    Returns:
        Energy delivered to the EV battery (kWh, DC side).
    """
    return ac_kwh * charger_efficiency


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

"""Absolute-SoC bounds that anchor the planner's battery model (issue #1094).

The planner measures battery energy as kWh *above an origin*: the effective
discharge floor.  Every published SoC percentage is converted back from that
origin (``floor_pct + kwh_above_floor / rated_kwh * 100``), so the origin must
be a SoC the battery can actually be at.  :func:`resolve_soc_bounds_pct` is the
single place that decides it, shared by the engine's model capacity and the
candidate generator's forecast-export reserve.
"""

from __future__ import annotations

import math


def finite_or(value: float | None, fallback: float) -> float:
    """Return *value* as a finite float, or *fallback* when it is not one.

    ``None``, unparsable values, NaN and ±inf all yield *fallback*; a genuine
    ``0.0`` is preserved.
    """
    try:
        converted = float(value) if value is not None else fallback
    except TypeError, ValueError:
        return fallback
    return converted if math.isfinite(converted) else fallback


def resolve_soc_bounds_pct(
    hardware_floor_pct: float | None,
    maximum_soc_pct: float | None,
    dynamic_floor_pct: float | None = None,
    current_soc_pct: float | None = None,
) -> tuple[float, float, float]:
    """Return finite ``(hardware_floor, effective_floor, maximum)`` SoC in percent.

    The dynamic floor shares the same absolute-SoC frame as Huawei's hardware
    floor and the configured maximum SoC.  All three are normalised so a stale
    or oversized bridge estimate cannot create a floor above the battery
    ceiling (issue #807).

    The dynamic floor is additionally capped at the live SoC (issue #1094).
    It is a *bridge reserve* — do not discharge below it — not a statement of
    where the battery is.  A battery already below the reserve cannot
    discharge at all, which an origin at the live SoC expresses exactly; an
    origin at the unreached reserve would instead report the battery at the
    reserve SoC and shrink its charge headroom to ``maximum − reserve``.  The
    hardware floor is never lowered by this cap.

    Args:
        hardware_floor_pct: Huawei end-of-discharge SoC (0-100).
        maximum_soc_pct: Configured charging cut-off SoC (0-100).
        dynamic_floor_pct: Bridge-reserve SoC from the dynamic discharge
            floor, or ``None`` when the feature is disabled.
        current_soc_pct: Live battery SoC (0-100), or ``None`` when unknown.
            A missing or non-finite value leaves the dynamic floor uncapped.

    Returns:
        ``(hardware_floor, effective_floor, maximum)`` with
        ``hardware_floor <= effective_floor <= maximum`` always holding.
    """
    hardware_floor = min(max(finite_or(hardware_floor_pct, 0.0), 0.0), 100.0)
    maximum_soc = min(
        max(finite_or(maximum_soc_pct, 100.0), hardware_floor),
        100.0,
    )
    dynamic_floor = finite_or(dynamic_floor_pct, hardware_floor)
    dynamic_floor = min(dynamic_floor, finite_or(current_soc_pct, dynamic_floor))
    effective_floor = min(max(dynamic_floor, hardware_floor), maximum_soc)
    return hardware_floor, effective_floor, maximum_soc


def wait_mode_reserve_above_hardware_floor(
    plan_reserve_kwh: float | None,
    rated_capacity_kwh: float | None,
    hardware_floor_pct: float,
    effective_floor_pct: float,
) -> float | None:
    """Return the wait-mode reserve measured from the hardware floor (issue #1200).

    The planner measures battery energy above the *effective* discharge floor,
    so the wait-mode self-consumption reserve (issue #914) is a number of kWh
    above that origin.  The applier compares it with the live capacity above
    the **hardware** floor.  With the dynamic discharge floor active the two
    origins differ, and the energy between them, which is exactly what the
    floor sets aside, counted as surplus the house may use.

    Adding that energy to the reserve puts both on the hardware-floor origin.
    The #954 time decay of the plan's reserve is not applied to it: the
    floor's reserve is needed now.

    Args:
        plan_reserve_kwh: Reserve from ``calculate_required_battery_for_plan``
            in kWh above the effective floor, or ``None`` when it cannot be
            derived.
        rated_capacity_kwh: Battery rated capacity (kWh).
        hardware_floor_pct: Normalized hardware end-of-discharge SoC.
        effective_floor_pct: Normalized effective floor from
            :func:`resolve_soc_bounds_pct`, already capped at the live SoC.

    Returns:
        The reserve in kWh above the hardware floor, or ``None`` when
        *plan_reserve_kwh* is ``None`` (the applier then keeps strict Wait).
    """
    if plan_reserve_kwh is None:
        return None
    floor_kwh = (
        max(finite_or(rated_capacity_kwh, 0.0), 0.0)
        * max(effective_floor_pct - hardware_floor_pct, 0.0)
        / 100.0
    )
    return round(plan_reserve_kwh + floor_kwh, 3)

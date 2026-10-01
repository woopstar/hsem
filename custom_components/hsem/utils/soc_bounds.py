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
    discharge at all, but it must not be reported at the reserve SoC or lose
    charge headroom.  The hardware floor is never lowered by this cap.

    The effective floor is the floor in force now.  The battery model's origin
    is the hardware floor (issue #1188); the dynamic floor reaches the plan as
    a per-slot bound, see ``planner/discharge_reserve.py``.

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


def reserve_floor_pct(
    reserve_kwh: float,
    usable_kwh: float,
    configured_min_soc_pct: float,
    max_soc_pct: float,
    *,
    capped: bool = True,
) -> float:
    """Return the SoC that holds *reserve_kwh* above the configured minimum.

    The reserve sits **on top of** the configured minimum SoC (issue #1221):
    *usable_kwh* is the capacity between the minimum and the maximum, so the
    reserve's share of it is a share of that span, not of the whole battery.
    Read as an absolute SoC instead, the share counted the energy below the
    minimum as reserve, and a 1.7 kWh bridge was held as 1.56 kWh.

    Args:
        reserve_kwh: Energy to hold, safety margin included (kWh).
        usable_kwh: Capacity between the minimum and the maximum SoC (kWh).
        configured_min_soc_pct: Configured minimum SoC (0-100).
        max_soc_pct: Configured maximum SoC (0-100).
        capped: ``False`` returns the SoC the reserve asks for even when the
            battery cannot hold it (issue #1222): the per-slot profile needs
            the whole reserve to place a shortfall.

    Returns:
        The floor in SoC percent: the configured minimum when there is no
        reserve or no usable capacity, and, unless *capped* is ``False``,
        never above *max_soc_pct* (a full battery holds all there is).
    """
    span_pct = max_soc_pct - configured_min_soc_pct
    if usable_kwh <= 1e-9 or span_pct <= 1e-9 or reserve_kwh <= 0.0:
        return configured_min_soc_pct
    share = reserve_kwh / usable_kwh
    return configured_min_soc_pct + (min(share, 1.0) if capped else share) * span_pct

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

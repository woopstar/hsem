"""House-battery target SoC by a daily deadline (issue #1109).

An opt-in user preference: reach ``battery_target_soc_pct`` by the next
occurrence of ``battery_target_soc_time``, funded **only** by PV the normal
plan would otherwise export.  This module resolves the next occurrence into a
:class:`BatteryTargetSpec` (target slot, target energy in model kWh, and the
shortfall price ``P``) and scores a plan's shortfall against it.

The MILP side (the two-stage solve that pins grid import to the normal plan)
lives in :mod:`custom_components.hsem.planner.milp._battery_target`; the
selector adds :func:`battery_target_penalty` to every candidate's ``score``
(never to ``total_cost``) through ``CostWeights.battery_target``.

Pure Python, no Home Assistant imports.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.milp._objective import ev_deadline_penalty_per_kwh
from custom_components.hsem.utils.datetime_utils import (
    future_slot_indices,
    utc_key,
)
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import clamp_efficiency
from custom_components.hsem.utils.soc_bounds import finite_or, resolve_soc_bounds_pct

#: Margin (currency per kWh) that keeps ``P`` strictly above the best export
#: value it must outbid and strictly below the smallest EV deadline penalty.
PENALTY_EPSILON = 1e-3

#: Stage 1 counts as meeting the target within this many kWh.
TARGET_TOLERANCE_KWH = 1e-6

#: ``estimated_battery_capacity_kwh`` is rounded to 3 decimals, so the score
#: ignores a shortfall below that resolution.
SCORE_TOLERANCE_KWH = 1e-3


@dataclass(frozen=True)
class BatteryTargetSpec:
    """The next target occurrence, resolved against one planner run.

    Attributes:
        target_time: Next occurrence of the configured daily target time.
        slot_end: End of slot ``T``, the last future slot ending at or before
            *target_time*.  The target is measured at the end of this slot.
        target_pct: Configured target, in absolute SoC percent.
        target_kwh: Target in model kWh above the MILP SoC origin, clamped to
            ``[0, usable_kwh]``.
        penalty_per_kwh: Undiscounted shortfall price ``P`` per DC kWh.
    """

    target_time: datetime
    slot_end: datetime
    target_pct: float
    target_kwh: float
    penalty_per_kwh: float


def _parse_target_time(raw: str) -> time | None:
    """Return the configured ``HH:MM[:SS]`` time, or ``None`` when invalid."""
    try:
        return time.fromisoformat(str(raw).strip())
    except TypeError, ValueError:
        return None


def next_target_slot_index(
    slots: Sequence[PlannedSlot],
    now: datetime,
    target: time,
) -> tuple[datetime, int] | None:
    """Return ``(occurrence, slot index)`` of the next enforceable occurrence.

    The occurrence is today's *target* in ``now``'s time zone, rolled to the
    next day when it falls before the end of the current (first future) slot.
    The slot index is that of the last future slot ending at or before the
    occurrence.  ``None`` when the horizon ends before the occurrence.
    """
    future_idx = future_slot_indices((s.end for s in slots), now)
    if not future_idx:
        return None
    tz = now.tzinfo
    local_date = now.astimezone(tz).date()
    occurrence = datetime.combine(local_date, target, tzinfo=tz)
    if utc_key(occurrence) < utc_key(slots[future_idx[0]].end):
        occurrence = datetime.combine(local_date + timedelta(days=1), target, tzinfo=tz)
    occurrence_key = utc_key(occurrence)
    if utc_key(slots[future_idx[-1]].end) < occurrence_key:
        return None
    eligible = [i for i in future_idx if utc_key(slots[i].end) <= occurrence_key]
    if not eligible:
        return None
    return occurrence, eligible[-1]


def target_kwh_for_pct(
    inp: PlannerInput, target_pct: float, usable_kwh: float
) -> float:
    """Convert an absolute SoC target to model kWh above the MILP SoC origin.

    Uses the same resolver as the engine's model capacity (and as
    ``_forecast_export_reserve_kwh``), so a dynamic floor or the live-SoC cap
    moves the origin consistently.  Clamped to ``[0, usable_kwh]``.
    """
    rated_kwh = max(finite_or(inp.battery_rated_capacity_kwh, 0.0), 0.0)
    model_usable_kwh = max(finite_or(usable_kwh, 0.0), 0.0)
    _hardware_floor, effective_floor_pct, maximum_soc_pct = resolve_soc_bounds_pct(
        inp.battery_end_of_discharge_soc_pct,
        inp.battery_max_soc_pct,
        inp.dynamic_discharge_floor_pct,
        inp.battery_soc_pct,
    )
    clamped_pct = min(max(target_pct, 0.0), maximum_soc_pct)
    target_kwh = rated_kwh * max(clamped_pct - effective_floor_pct, 0.0) / 100.0
    return min(max(target_kwh, 0.0), model_usable_kwh)


def target_penalty_per_kwh(
    slots: Sequence[PlannedSlot],
    future_idx: Sequence[int],
    target_lp_index: int,
    *,
    charge_efficiency_pct: float,
    cycle_cost_per_kwh: float,
    ev_configs: Sequence[EVConfig] | None,
) -> float:
    """Return ``P = min(max_{t≤T} p_exp/η_chg + cycle + ε, P_ev_floor − ε)``.

    ``P`` outbids the best export value before the deadline, so the MILP
    prefers storing otherwise-exported PV to paying the shortfall, and it
    stays below every active EV deadline penalty, so deadline-bound EVs keep
    priority.  With grid import pinned in stage 2, a high ``P`` can never buy
    grid energy.
    """
    charge_eff = clamp_efficiency(charge_efficiency_pct)
    best_export = 0.0
    for i in future_idx[: target_lp_index + 1]:
        best_export = max(best_export, finite_or(slots[i].price.export_price, 0.0))
    penalty = best_export / charge_eff + max(cycle_cost_per_kwh, 0.0) + PENALTY_EPSILON

    p_imp_max = max(
        (finite_or(slots[i].price.import_price, 0.0) for i in future_idx),
        default=0.1,
    )
    ev_penalties = [
        p
        for ev in ev_configs or []
        if (p := ev_deadline_penalty_per_kwh(ev, p_imp_max, len(future_idx)))
        is not None
    ]
    if ev_penalties:
        penalty = min(penalty, min(ev_penalties) - PENALTY_EPSILON)
    return max(penalty, 0.0)


def resolve_battery_target(
    inp: PlannerInput,
    slots: Sequence[PlannedSlot],
    now: datetime,
    *,
    usable_kwh: float,
    cycle_cost_per_kwh: float,
    ev_configs: Sequence[EVConfig] | None = None,
) -> BatteryTargetSpec | None:
    """Resolve the next house-battery target occurrence for this run.

    Returns ``None`` when the feature is disabled, the configured time is
    invalid, the battery has no usable capacity, or the horizon ends before
    the next occurrence.
    """
    if not inp.battery_target_soc_enabled:
        return None
    target = _parse_target_time(inp.battery_target_soc_time)
    if target is None:
        log_planner(
            "warning",
            "[battery_target] invalid target time %r — target ignored",
            inp.battery_target_soc_time,
        )
        return None
    if finite_or(usable_kwh, 0.0) <= 1e-9:
        return None
    resolved = next_target_slot_index(slots, now, target)
    if resolved is None:
        log_planner(
            "debug",
            "[battery_target] next occurrence of %s is beyond the horizon",
            target.isoformat(),
        )
        return None
    occurrence, slot_index = resolved
    future_idx = future_slot_indices((s.end for s in slots), now)
    target_pct = finite_or(inp.battery_target_soc_pct, 100.0)
    spec = BatteryTargetSpec(
        target_time=occurrence,
        slot_end=slots[slot_index].end,
        target_pct=target_pct,
        target_kwh=target_kwh_for_pct(inp, target_pct, usable_kwh),
        penalty_per_kwh=target_penalty_per_kwh(
            slots,
            future_idx,
            future_idx.index(slot_index),
            charge_efficiency_pct=inp.battery_charge_efficiency_pct,
            cycle_cost_per_kwh=cycle_cost_per_kwh,
            ev_configs=ev_configs,
        ),
    )
    log_planner(
        "debug",
        "[battery_target] next occurrence=%s  slot_end=%s  target=%.1f%%  "
        "target_kwh=%.3f  penalty=%.4f/kWh",
        spec.target_time.isoformat(),
        spec.slot_end.isoformat(),
        spec.target_pct,
        spec.target_kwh,
        spec.penalty_per_kwh,
    )
    return spec


def target_slot(
    slots: Sequence[PlannedSlot], spec: BatteryTargetSpec
) -> PlannedSlot | None:
    """Return the slot the target is measured at, or ``None`` if absent."""
    key = utc_key(spec.slot_end)
    return next((s for s in slots if utc_key(s.end) == key), None)


def battery_target_penalty(
    slots: Sequence[PlannedSlot], spec: BatteryTargetSpec | None
) -> float:
    """Return ``P × shortfall`` at the target slot for a simulated plan.

    Reads ``estimated_battery_capacity_kwh`` (model kWh, written by
    ``simulate_soc``) at slot ``T``.  Selector-only: added to ``score``,
    never to ``total_cost``.
    """
    if spec is None:
        return 0.0
    slot = target_slot(slots, spec)
    if slot is None:
        return 0.0
    projected = finite_or(slot.estimated_battery_capacity_kwh, 0.0)
    shortfall = max(spec.target_kwh - projected, 0.0)
    if not math.isfinite(shortfall) or shortfall < SCORE_TOLERANCE_KWH:
        return 0.0
    return spec.penalty_per_kwh * shortfall


def summarize_battery_target(
    spec: BatteryTargetSpec | None,
    candidates: Sequence[Any],
    winner_slots: Sequence[PlannedSlot],
) -> dict[str, Any] | None:
    """Return the next-occurrence diagnostics published by the planner.

    Starts from the MILP candidate's ``diagnostics["battery_target"]`` (the
    stage-1/stage-2 record) and adds the selected plan's projection, which
    differs from the MILP's only when the selector fell back to ``passive``.
    """
    if spec is None:
        return None
    summary: dict[str, Any] = {
        "target_time": spec.target_time.isoformat(),
        "target_slot_end": spec.slot_end.isoformat(),
        "target_pct": round(spec.target_pct, 2),
        "target_kwh": round(spec.target_kwh, 3),
        "penalty_per_kwh": round(spec.penalty_per_kwh, 6),
        "stage2_ran": False,
        "stage2_status": "milp_unavailable",
    }
    for candidate in candidates:
        diagnostics = getattr(candidate, "diagnostics", None)
        if isinstance(diagnostics, dict) and isinstance(
            diagnostics.get("battery_target"), dict
        ):
            summary.update(diagnostics["battery_target"])
            break
    slot = target_slot(winner_slots, spec)
    if slot is not None:
        projected = finite_or(slot.estimated_battery_capacity_kwh, 0.0)
        summary["selected_projected_kwh"] = round(projected, 3)
        summary["selected_shortfall_kwh"] = round(
            max(spec.target_kwh - projected, 0.0), 3
        )
    return summary

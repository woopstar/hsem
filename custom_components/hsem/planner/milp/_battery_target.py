"""Two-stage MILP solve for the house-battery target SoC (issue #1109).

The target may only be funded by PV the normal plan would otherwise export.
The battery charge column ``ec[t]`` mixes grid- and PV-sourced energy, so no
single linear penalty can express that (see #1015 for the same limitation).
The counterfactual is solved directly instead:

1. **Stage 1** is the normal plan, the existing
   :func:`solve_milp_with_past_target_reservation` call, unchanged.
2. When stage 1 misses the target at the next occurrence, **stage 2**
   re-solves with the same inputs plus the rows of
   :mod:`._battery_target_rows`: a soft target row priced at ``P``, grid
   import **pinned** to stage 1 in every build-window slot ``t ≤ T`` and
   **capped** at stage 1 after it.  With import fixed, the only way left to
   raise ``soc[T]`` is to export less PV, lowest-value slots first.

Stage 2 is skipped when the target is disabled, has no occurrence in the
horizon, or stage 1 already meets it; the stage-1 result is then returned
unchanged.  When stage 2 fails, the stage-1 result is returned with a warning.

**Charge-past-target EVs (Option A, agreed on the issue):** the house battery
takes the otherwise-exported PV first.  Stage 2 first solves with every
charge-past-target EV removed, then re-solves with each such EV's
``past_target_reserved_ac_kwh`` taken from that house-first plan, so the EV
only gets the PV the battery target leaves unused.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from custom_components.hsem.planner.battery_target import (
    TARGET_TOLERANCE_KWH,
    BatteryTargetSpec,
)
from custom_components.hsem.planner.milp._battery_target_rows import (
    BatteryTargetRows,
)
from custom_components.hsem.planner.milp._past_target_reservation import (
    reserved_ac_kwh_per_future_slot,
    solve_milp_with_past_target_reservation,
)
from custom_components.hsem.planner.milp_optimizer import solve_milp
from custom_components.hsem.utils.datetime_utils import future_slot_indices, utc_key
from custom_components.hsem.utils.logger import log_planner

if TYPE_CHECKING:
    from custom_components.hsem.models.ev_config import EVConfig
    from custom_components.hsem.models.planned_slot import PlannedSlot

#: Stage-1 grid import at or below this (kWh) counts as "imported nothing".
IMPORT_ZERO_TOLERANCE_KWH = 1e-6

#: Diagnostics key the wrapper writes into the MILP diagnostics dict.
DIAGNOSTICS_KEY = "battery_target"

#: Stage-1 diagnostics key holding the LP ``gi[t]`` solution.
_LP_GRID_IMPORT_KEY = "lp_grid_import_kwh"


def projected_kwh_at(
    out_slots: list[PlannedSlot],
    future_idx: list[int],
    target_lp_index: int,
    current_kwh: float,
) -> float:
    """Return ``soc[T]`` in model kWh from a solve's resolved charge flows."""
    net = sum(
        float(out_slots[i].batteries_charged_kwh)
        - float(out_slots[i].batteries_discharged_kwh)
        for i in future_idx[: target_lp_index + 1]
    )
    return float(current_kwh) + net


def build_target_rows(
    spec: BatteryTargetSpec,
    target_lp_index: int,
    stage1_lp_import: list[float],
) -> BatteryTargetRows:
    """Return the stage-2 rows: pin import in ``W = {t ≤ T}``, cap it after.

    The pin is **exact** (``floor == cap == gi_stage1[t]``), not a ±tolerance
    band: a band turns every slot that imported nothing into a
    ``gi[t] ≤ 1e-6 · z[t]`` grid-direction row, a coefficient at HiGHS's own
    feasibility tolerance, and the solver then aborted with "Solve error" on
    roughly one stage-2 model in eight.  Slots where stage 1 imported nothing
    get ``gi[t] = 0``, which leaves the grid-direction binary free to export.
    The stage-1 solution satisfies every bound, so stage 2 is never
    infeasible and never worse on its own objective.
    """
    floor: list[float] = []
    cap: list[float] = []
    for t, raw in enumerate(stage1_lp_import):
        gi = float(raw) if float(raw) > IMPORT_ZERO_TOLERANCE_KWH else 0.0
        cap.append(gi)
        floor.append(gi if t <= target_lp_index else 0.0)
    return BatteryTargetRows(
        target_index=target_lp_index,
        target_kwh=spec.target_kwh,
        penalty_per_kwh=spec.penalty_per_kwh,
        grid_import_floor=tuple(floor),
        grid_import_cap=tuple(cap),
    )


def _solve_stage2(
    slots: list[PlannedSlot],
    now: datetime,
    rows: BatteryTargetRows,
    *,
    ev_configs: list[EVConfig] | None,
    charge_efficiency_pct: float,
    solve_kwargs: dict[str, Any],
) -> tuple[list[PlannedSlot], dict[str, Any]] | None:
    """Solve stage 2, giving the house target priority over past-target EVs."""
    evs = list(ev_configs or [])
    if not any(ev.charge_past_target for ev in evs):
        return solve_milp(
            slots,
            now,
            ev_configs=ev_configs,
            charge_efficiency_pct=charge_efficiency_pct,
            battery_target=rows,
            **solve_kwargs,
        )
    house_first = solve_milp(
        slots,
        now,
        ev_configs=[ev for ev in evs if not ev.charge_past_target],
        charge_efficiency_pct=charge_efficiency_pct,
        battery_target=rows,
        **solve_kwargs,
    )
    if house_first is None:
        return None
    reserved = reserved_ac_kwh_per_future_slot(
        slots, house_first[0], now, charge_efficiency_pct
    )
    staged = [
        replace(ev, past_target_reserved_ac_kwh=reserved)
        if ev.charge_past_target
        else ev
        for ev in evs
    ]
    return solve_milp(
        slots,
        now,
        ev_configs=staged,
        charge_efficiency_pct=charge_efficiency_pct,
        battery_target=rows,
        **solve_kwargs,
    )


def _base_diagnostics(spec: BatteryTargetSpec) -> dict[str, Any]:
    """Return the per-occurrence diagnostics shared by every outcome."""
    return {
        "target_time": spec.target_time.isoformat(),
        "target_slot_end": spec.slot_end.isoformat(),
        "target_pct": round(spec.target_pct, 2),
        "target_kwh": round(spec.target_kwh, 3),
        "penalty_per_kwh": round(spec.penalty_per_kwh, 6),
        "stage1_projected_kwh": None,
        "projected_kwh": None,
        "shortfall_kwh": None,
        "stage2_ran": False,
        "stage2_status": "not_run",
        "max_import_delta_kwh": 0.0,
        "max_import_increase_after_kwh": 0.0,
    }


def _finish(
    result: tuple[list[PlannedSlot], dict[str, Any]],
    diagnostics: dict[str, Any],
    projected: float | None,
    spec: BatteryTargetSpec,
) -> tuple[list[PlannedSlot], dict[str, Any]]:
    """Attach the target diagnostics to *result* and return it."""
    if projected is not None:
        diagnostics["projected_kwh"] = round(projected, 3)
        diagnostics["shortfall_kwh"] = round(max(spec.target_kwh - projected, 0.0), 3)
    result[1][DIAGNOSTICS_KEY] = diagnostics
    return result


def solve_milp_with_battery_target(
    slots: list[PlannedSlot],
    now: datetime,
    *,
    battery_target: BatteryTargetSpec | None,
    current_kwh: float,
    ev_configs: list[EVConfig] | None = None,
    charge_efficiency_pct: float = 97.0,
    **solve_kwargs: Any,
) -> tuple[list[PlannedSlot], dict[str, Any]] | None:
    """Solve the MILP, then re-solve towards the house-battery target if needed.

    Accepts the arguments of
    :func:`~custom_components.hsem.planner.milp._past_target_reservation.solve_milp_with_past_target_reservation`
    plus *battery_target*.  With ``battery_target=None`` it is exactly that
    call, so a disabled target is bit-for-bit identical to the normal plan.

    Returns:
        The stage-2 result when stage 2 ran and succeeded, otherwise the
        stage-1 result (``None`` only when stage 1 itself fails).  Either
        carries ``diagnostics["battery_target"]`` when a target is configured.
    """
    stage1 = solve_milp_with_past_target_reservation(
        slots,
        now,
        current_kwh=current_kwh,
        ev_configs=ev_configs,
        charge_efficiency_pct=charge_efficiency_pct,
        **solve_kwargs,
    )
    if battery_target is None or stage1 is None:
        return stage1

    spec = battery_target
    diagnostics = _base_diagnostics(spec)
    future_idx = future_slot_indices((s.end for s in slots), now)
    end_key = utc_key(spec.slot_end)
    target_lp_index = next(
        (t for t, i in enumerate(future_idx) if utc_key(slots[i].end) == end_key),
        None,
    )
    if target_lp_index is None:
        diagnostics["stage2_status"] = "no_occurrence"
        return _finish(stage1, diagnostics, None, spec)

    stage1_slots, stage1_diag = stage1
    stage1_projected = projected_kwh_at(
        stage1_slots, future_idx, target_lp_index, current_kwh
    )
    diagnostics["stage1_projected_kwh"] = round(stage1_projected, 3)
    if stage1_projected >= spec.target_kwh - TARGET_TOLERANCE_KWH:
        diagnostics["stage2_status"] = "target_met"
        return _finish(stage1, diagnostics, stage1_projected, spec)

    stage1_lp_import = stage1_diag.get(_LP_GRID_IMPORT_KEY)
    if not isinstance(stage1_lp_import, list) or len(stage1_lp_import) != len(
        future_idx
    ):
        log_planner(
            "warning",
            "[battery_target] stage-1 grid import unavailable — keeping the "
            "normal plan",
        )
        diagnostics["stage2_status"] = "stage1_import_unavailable"
        return _finish(stage1, diagnostics, stage1_projected, spec)

    rows = build_target_rows(spec, target_lp_index, stage1_lp_import)
    stage2 = _solve_stage2(
        slots,
        now,
        rows,
        ev_configs=ev_configs,
        charge_efficiency_pct=charge_efficiency_pct,
        solve_kwargs={"current_kwh": current_kwh, **solve_kwargs},
    )
    diagnostics["stage2_ran"] = True
    if stage2 is None:
        log_planner(
            "warning",
            "[battery_target] stage-2 solve failed — keeping the normal plan",
        )
        diagnostics["stage2_status"] = "failed"
        return _finish(stage1, diagnostics, stage1_projected, spec)

    stage2_slots = stage2[0]
    deltas = [
        float(stage2_slots[i].grid_import_kwh) - float(stage1_slots[i].grid_import_kwh)
        for i in future_idx
    ]
    window = deltas[: target_lp_index + 1]
    after = deltas[target_lp_index + 1 :]
    diagnostics["stage2_status"] = "solved"
    diagnostics["max_import_delta_kwh"] = round(max(abs(d) for d in window), 3)
    diagnostics["max_import_increase_after_kwh"] = round(max([0.0, *after]), 3)
    projected = projected_kwh_at(stage2_slots, future_idx, target_lp_index, current_kwh)
    log_planner(
        "debug",
        "[battery_target] stage 2 solved  target=%.3f  stage1=%.3f  "
        "stage2=%.3f  penalty=%.4f/kWh",
        spec.target_kwh,
        stage1_projected,
        projected,
        spec.penalty_per_kwh,
    )
    return _finish(stage2, diagnostics, projected, spec)

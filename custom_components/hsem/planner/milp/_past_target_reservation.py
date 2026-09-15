"""Two-stage MILP solve for charge-past-target EVs (issue #1015).

A charge-past-target EV may charge only from PV surplus the house battery and
the other consumers would not otherwise have used. A single MILP cannot express
that rule exactly: the house battery's charge column ``ec[t]`` mixes grid- and
PV-sourced energy, so every linear "battery first" row either caps *all*
battery charging at the surplus (the battery can no longer grid-charge while a
past-target EV is plugged in) or lets the EV take surplus the battery would
have stored while the battery refills from cheap grid — the EV then draws from
grid in all but name.

The counterfactual is solved directly instead:

1. **Stage 1** solves the plan with every charge-past-target EV removed and
   records, per future slot, the AC energy it spent on the house battery and on
   every remaining EV.
2. **Stage 2** solves the full plan with that reservation attached to each
   charge-past-target EV, whose shared surplus row then caps it at the PV the
   stage-1 plan left unused (``planner/milp/_ev_constraints.py``).

When stage 1 fails to solve, the reservation is infinite: the past-target EV
gets nothing, and everything else is still planned by stage 2. No EV that is
not in charge-past-target mode triggers a second solve.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from custom_components.hsem.planner.milp_optimizer import solve_milp
from custom_components.hsem.utils.datetime_utils import future_slot_indices
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import clamp_efficiency

if TYPE_CHECKING:
    from custom_components.hsem.models.ev_config import EVConfig
    from custom_components.hsem.models.planned_slot import PlannedSlot


def reserved_ac_kwh_per_future_slot(
    slots: list[PlannedSlot],
    stage1_slots: list[PlannedSlot],
    now: datetime,
    charge_efficiency_pct: float,
) -> tuple[float, ...]:
    """Return the AC energy stage 1 spent on the battery and other EVs.

    Args:
        slots: The slot list passed to both solves (not mutated by the MILP).
        stage1_slots: Stage-1 output slots, aligned index-for-index with
            *slots*.
        now: Timezone-aware current datetime, defining the future slots.
        charge_efficiency_pct: House battery charge efficiency, in percent.

    Returns:
        One full-slot-width AC kWh value per MILP future slot, in LP order:
        the battery's AC draw (``batteries_charged_kwh / η_charge``) plus the
        EV load the stage-1 solve added on top of whatever the input slot
        already carried.
    """
    charge_eff = clamp_efficiency(charge_efficiency_pct)
    reserved: list[float] = []
    for i in future_slot_indices((s.end for s in slots), now):
        battery_ac_kwh = max(float(stage1_slots[i].batteries_charged_kwh), 0.0) / (
            charge_eff
        )
        other_ev_ac_kwh = max(
            float(stage1_slots[i].ev_total_planned_load_kwh)
            - float(slots[i].ev_total_planned_load_kwh),
            0.0,
        )
        reserved.append(battery_ac_kwh + other_ev_ac_kwh)
    return tuple(reserved)


def solve_milp_with_past_target_reservation(
    slots: list[PlannedSlot],
    now: datetime,
    *,
    ev_configs: list[EVConfig] | None = None,
    charge_efficiency_pct: float = 97.0,
    **solve_kwargs: Any,
) -> tuple[list[PlannedSlot], dict[str, Any]] | None:
    """Solve the MILP, reserving battery/other-EV energy ahead of past-target EVs.

    Accepts exactly the arguments of
    :func:`~custom_components.hsem.planner.milp_optimizer.solve_milp` and
    returns its result. With no charge-past-target EV it is a single, unchanged
    ``solve_milp`` call.

    Args:
        slots: Planner slots to optimise.
        now: Timezone-aware current datetime.
        ev_configs: EV configurations, in primary/second order (preserved).
        charge_efficiency_pct: House battery charge efficiency, in percent.
        **solve_kwargs: Every other ``solve_milp`` keyword argument, passed
            through unchanged to both stages.

    Returns:
        The stage-2 ``solve_milp`` result, or ``None`` when it fails to solve.
    """
    evs = list(ev_configs or [])
    if not any(ev.charge_past_target for ev in evs):
        return solve_milp(
            slots,
            now,
            ev_configs=ev_configs,
            charge_efficiency_pct=charge_efficiency_pct,
            **solve_kwargs,
        )

    stage1 = solve_milp(
        slots,
        now,
        ev_configs=[ev for ev in evs if not ev.charge_past_target],
        charge_efficiency_pct=charge_efficiency_pct,
        **solve_kwargs,
    )
    if stage1 is None:
        future_count = len(future_slot_indices((s.end for s in slots), now))
        reserved = tuple(math.inf for _ in range(future_count))
        log_planner(
            "warning",
            "[milp_ev] past-target stage-1 solve failed — charge-past-target "
            "EVs get no energy this cycle",
        )
    else:
        reserved = reserved_ac_kwh_per_future_slot(
            slots, stage1[0], now, charge_efficiency_pct
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
        **solve_kwargs,
    )

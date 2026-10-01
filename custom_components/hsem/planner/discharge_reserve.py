"""Per-slot discharge reserve from the dynamic discharge floor (issue #1188).

The dynamic discharge floor is a bridge reserve: the energy the house needs
from now until the next refill, times a safety margin.  That reserve shrinks
with every slot the bridge gets shorter, and it is gone once the refill has
happened.  The planner used to model it as one constant floor for the whole
horizon by moving the battery model's origin up to it.  The plan then held the
battery all night although every replan lowered the floor, and it planned the
day after the refill with a battery that was smaller than the real one.

This module turns the floor into a **per-slot lower bound on stored energy**
instead.  The model origin stays at the hardware floor, and every candidate
reads the bound from ``PlannedSlot.discharge_reserve_kwh``:

- the MILP as the right-hand side of its lower SoC rows,
- the SoC simulation as the level greedy discharge must not go below,
- the candidate validation and the MILP post-write inventory check.

The bound of a slot is the reserve required at the slot's **end**, which is
the floor at the start of the next slot.  Serving the house in a slot is what
the reserve is for, so the battery may follow the declining reserve; it may
not export or over-discharge below it.

Two rules keep the bound satisfiable without charging:

1. It never exceeds the energy the battery holds now.  A battery already
   below the reserve cannot discharge, but it is not forced to charge either,
   and it keeps its full charge headroom (the issue #1094 behaviour).
2. It never rises along the horizon.  A partial grid charge in the reference
   plan makes the raw floor step up after that charge; the plan this bound
   constrains is not obliged to charge there, so the step is ignored.  The
   next replan recomputes the floor from its own reference solve.

Pure functions — no I/O, no Home Assistant imports.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.utils.datetime_utils import slot_is_future, utc_key
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.soc_bounds import finite_or, resolve_soc_bounds_pct


def resolve_effective_discharge_floor_pct(
    inp: PlannerInput,
) -> tuple[float, float, float]:
    """Return finite ``(hardware, effective, maximum)`` SoC bounds in percent.

    Thin adapter over :func:`resolve_soc_bounds_pct`, which normalizes the
    three limits (issue #807) and caps the dynamic floor at the live SoC
    (issue #1094).  ``effective`` is the floor in force *now*; the model
    origin is ``hardware`` (issue #1188).
    """
    return resolve_soc_bounds_pct(
        inp.battery_end_of_discharge_soc_pct,
        inp.battery_max_soc_pct,
        inp.dynamic_discharge_floor_pct,
        inp.battery_soc_pct,
    )


def _floor_pct_at_slot_starts(
    slots: list[PlannedSlot], inp: PlannerInput, hardware_floor_pct: float
) -> list[float]:
    """Return the dynamic floor at the start of every slot, plus the horizon end.

    With a profile (``PlannerInput.dynamic_floor_profile``) each slot takes
    the entry with the same UTC start; a slot without one, and the horizon
    end, have no reserve.  Without a profile the scalar floor applies to every
    slot and to the horizon end, which is the constant floor a caller that
    only knows the scalar asks for.

    Args:
        slots: Chronological planner slots.
        inp: Planner input carrying the scalar floor and the optional profile.
        hardware_floor_pct: Normalized hardware end-of-discharge SoC.

    Returns:
        ``len(slots) + 1`` floor values in SoC percent.
    """
    if inp.dynamic_floor_profile is None:
        constant = finite_or(inp.dynamic_discharge_floor_pct, hardware_floor_pct)
        return [constant] * (len(slots) + 1)
    by_start = {
        utc_key(datetime.fromisoformat(start_iso)): finite_or(pct, hardware_floor_pct)
        for start_iso, pct in inp.dynamic_floor_profile
    }
    floors = [by_start.get(utc_key(slot.start), hardware_floor_pct) for slot in slots]
    floors.append(hardware_floor_pct)
    return floors


def apply_discharge_reserve(
    slots: list[PlannedSlot],
    inp: PlannerInput,
    now: datetime,
    current_kwh: float,
) -> None:
    """Write the dynamic floor onto *slots* as ``discharge_reserve_kwh``.

    The value is the stored energy, in kWh above the hardware floor, that the
    plan must still hold at the end of the slot.  It is zero on every slot
    when the dynamic floor is disabled, and on past slots.

    Args:
        slots: Mutable chronological planner slots.
        inp: Planner input carrying the scalar floor and the optional profile.
        now: Timezone-aware current datetime.
        current_kwh: Energy stored above the hardware floor now (kWh).
    """
    for slot in slots:
        slot.discharge_reserve_kwh = 0.0
    if inp.dynamic_discharge_floor_pct is None and inp.dynamic_floor_profile is None:
        return

    hardware_pct, _floor_now, maximum_pct = resolve_effective_discharge_floor_pct(inp)
    rated_kwh = max(finite_or(inp.battery_rated_capacity_kwh, 0.0), 0.0)
    floors = _floor_pct_at_slot_starts(slots, inp, hardware_pct)

    # Never above the energy held now, and never rising along the horizon.
    bound_kwh = max(current_kwh, 0.0)
    first_kwh: float | None = None
    released_at: datetime | None = None
    for index, slot in enumerate(slots):
        if not slot_is_future(slot.end, now):
            continue
        floor_pct = min(max(floors[index + 1], hardware_pct), maximum_pct)
        bound_kwh = min(bound_kwh, rated_kwh * (floor_pct - hardware_pct) / 100.0)
        slot.discharge_reserve_kwh = bound_kwh
        if first_kwh is None:
            first_kwh = bound_kwh
        if released_at is None and bound_kwh <= 1e-9:
            released_at = slot.end

    if first_kwh is not None and first_kwh > 1e-9:
        log_planner(
            "debug",
            "[core] Dynamic discharge reserve active: %.3f kWh above the hardware "
            "floor at the end of the live slot (floor now: %s%%, live SoC: %.1f%%, "
            "stored: %.3f kWh), released at %s",
            first_kwh,
            inp.dynamic_discharge_floor_pct,
            inp.battery_soc_pct,
            current_kwh,
            released_at.isoformat() if released_at is not None else "never",
        )


def wait_mode_reserve_with_floor(
    plan_reserve_kwh: float | None,
    slots: list[PlannedSlot],
    now: datetime,
) -> float | None:
    """Return the wait-mode reserve, never below the live slot's floor reserve.

    The wait-mode self-consumption reserve (issue #914) tells the applier how
    much stored energy the house may **not** use in a ``batteries_wait_mode``
    slot under ``self_consumption_with_reserve``.  It is derived from the
    selected plan's SoC trajectory, which is flat while the dynamic discharge
    floor holds the battery, so on its own it released the energy the floor
    had set aside (issue #1200).

    The live slot's ``discharge_reserve_kwh`` is what the floor requires at
    the end of that slot, in kWh above the hardware floor: the same origin as
    the applier's live capacity.  It is needed now, so the #954 time decay of
    the trajectory reserve is not applied to it.

    Args:
        plan_reserve_kwh: Reserve from
            :func:`~custom_components.hsem.planner.discharge_scheduler.calculate_required_battery_for_plan`,
            or ``None`` when it cannot be derived.
        slots: The selected plan's slots, carrying ``discharge_reserve_kwh``.
        now: Timezone-aware current datetime.

    Returns:
        The larger of the two reserves in kWh, or ``None`` when
        *plan_reserve_kwh* is ``None`` (the applier then keeps strict Wait).
    """
    if plan_reserve_kwh is None:
        return None
    live_slot = min(
        (slot for slot in slots if slot_is_future(slot.end, now)),
        key=lambda slot: utc_key(slot.start),
        default=None,
    )
    floor_reserve_kwh = live_slot.discharge_reserve_kwh if live_slot else 0.0
    return round(max(plan_reserve_kwh, floor_reserve_kwh), 3)

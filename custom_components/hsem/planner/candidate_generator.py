"""Candidate plan generator for the HSEM planner (issues #296, #416).

This module generates multiple independent charge/discharge strategy candidates
from the same baseline slot population, so the selector can compare them and
pick the best valid plan.

Design principles
-----------------
- **Pure Python, no Home Assistant imports** — testable with plain pytest.
- Each candidate is built from a *deep copy* of the pre-populated slots so
  strategies cannot interfere with each other.
- The generator only mutates ``recommendation`` and ``batteries_charged``; the
  full SoC simulation (``simulate_soc``) must be called by the caller after
  receiving the slots in order to populate ``grid_import_kwh``,
  ``grid_export_kwh``, and ``estimated_battery_soc``.
- The **baseline** candidate re-uses slots that have already been processed by
  the normal scheduling pipeline (discharge → charge → excess export →
  optimisation), so it captures the current HSEM behaviour exactly.

Candidates produced
-------------------
1. ``baseline``       — current HSEM scheduling output (slots already processed).
2. ``no_action``      — all recommendations cleared; battery is completely idle.
3. ``grid_charge``    — grid-charge slots are kept; solar charging is removed.
4. ``solar_only``     — only solar-charge slots are kept; grid charging cleared.
5. ``discharge_only`` — discharge slots are kept; all charge slots cleared.
6. ``aggressive``     — cheapest N slots forced to grid-charge regardless of
                        schedule; most expensive M slots forced to discharge.
                        N is derived dynamically from battery headroom and
                        max charge per slot so it scales with the horizon and
                        battery size (fix for issue #416 Bug 2).
7. ``milp``           — globally-optimal LP solution (when scipy is available);
                        falls back gracefully if the solver fails.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime

from custom_components.hsem.datetime_utils import as_tz
from custom_components.hsem.models.planner_inputs import PlannerInput
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.planner.milp_optimizer import (
    CANDIDATE_MILP,
    is_scipy_available,
    solve_milp,
)
from custom_components.hsem.planner.planner_logger import log_planner
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Candidate name constants — shared with selector so both sides speak the
# same identifiers without re-defining strings.
# ---------------------------------------------------------------------------

CANDIDATE_BASELINE = "baseline"
CANDIDATE_NO_ACTION = "no_action"
CANDIDATE_GRID_CHARGE = "grid_charge"
CANDIDATE_SOLAR_ONLY = "solar_only"
CANDIDATE_DISCHARGE_ONLY = "discharge_only"
CANDIDATE_AGGRESSIVE = "aggressive"

# Re-export MILP candidate name so callers only need to import from here
__all__ = [
    "CANDIDATE_BASELINE",
    "CANDIDATE_NO_ACTION",
    "CANDIDATE_GRID_CHARGE",
    "CANDIDATE_SOLAR_ONLY",
    "CANDIDATE_DISCHARGE_ONLY",
    "CANDIDATE_AGGRESSIVE",
    "CANDIDATE_MILP",
    "CandidatePlan",
    "generate_candidates",
]

# Recommendations that represent charging (any source)
_CHARGE_RECS: frozenset[str] = frozenset(
    {
        Recommendations.BatteriesChargeGrid.value,
        Recommendations.BatteriesChargeSolar.value,
    }
)

# Recommendations that represent discharging (any form)
_DISCHARGE_RECS: frozenset[str] = frozenset(
    {
        Recommendations.BatteriesDischargeMode.value,
        Recommendations.ForceBatteriesDischarge.value,
    }
)

# Fallback number of discharge slots when no battery headroom data is available.
# The charge slot count is derived dynamically from battery capacity (Bug 2 fix).
_AGGRESSIVE_DISCHARGE_SLOTS = 3


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class CandidatePlan:
    """A single candidate plan ready for scoring.

    Attributes:
        name:
            Short, machine-readable identifier for this candidate strategy.
        slots:
            Fully populated :class:`PlannedSlot` list.  ``batteries_charged``
            and ``recommendation`` have been written by the generator;
            ``batteries_discharged``, ``grid_import_kwh``, ``grid_export_kwh``,
            and ``estimated_battery_soc`` are written by :func:`simulate_soc`
            **after** the caller receives this object.
        is_valid:
            ``True`` once the plan has passed validity checks (e.g. SoC never
            drops below the end-of-discharge floor).  Set by the selector after
            the SoC simulation runs.
        rejection_reason:
            Human-readable reason when ``is_valid`` is ``False``.
    """

    name: str
    slots: list[PlannedSlot]
    is_valid: bool = True
    rejection_reason: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_candidates(
    baseline_slots: list[PlannedSlot],
    inp: PlannerInput,
    now: datetime,
    max_charge_per_slot: float,
    current_kwh: float = 0.0,
    usable_kwh: float = 0.0,
    max_discharge_per_slot: float | None = None,
) -> list[CandidatePlan]:
    """Generate all candidate plans from the already-populated baseline slots.

    The *baseline_slots* list must have been fully processed by the normal
    scheduling pipeline (prices, consumption, net consumption, discharge
    windows, charge windows, excess export, optimisation) **before** this
    function is called.  The SoC simulation has **not** yet been applied —
    it will be run separately by the selector for each candidate.

    Args:
        baseline_slots:
            Fully scheduled slots (pre-SoC-simulation) representing the
            current HSEM planning output.  This list is **not** mutated; each
            candidate receives its own deep copy.
        inp:
            The planner input for this run.  Used to derive per-slot power
            limits and price thresholds for the aggressive strategy.
        now:
            Timezone-aware current datetime.
        max_charge_per_slot:
            Maximum energy (kWh) storable per slot after conversion losses.
            Used when the aggressive strategy forces charging.
        current_kwh:
            Current battery energy above the discharge floor (kWh).  Used to
            derive the number of charge slots needed to fill the battery for
            the aggressive candidate (Bug 2 fix in issue #416).
        usable_kwh:
            Maximum usable battery capacity (kWh).  Used alongside
            ``current_kwh`` for the aggressive slot count.
        max_discharge_per_slot:
            Maximum energy dischargeable per slot (kWh) passed through to the
            MILP optimizer.  ``None`` means unlimited.

    Returns:
        Ordered list of :class:`CandidatePlan` objects.  The baseline is
        always first so tie-breaking always prefers the current behaviour.
    """
    candidates: list[CandidatePlan] = []

    # 1. Baseline — current scheduling pipeline output (no copy needed for slots
    #    themselves since each candidate works on its own copy below; we do copy
    #    here so all candidates start from identical state).
    candidates.append(
        CandidatePlan(
            name=CANDIDATE_BASELINE,
            slots=_copy_slots(baseline_slots),
        )
    )

    # 2. No-action — battery completely idle
    no_action = _copy_slots(baseline_slots)
    _clear_all_charge_discharge(no_action)
    candidates.append(CandidatePlan(name=CANDIDATE_NO_ACTION, slots=no_action))

    # 3. Grid-charge only — keep discharge windows; drop solar-only charge slots
    grid_charge = _copy_slots(baseline_slots)
    _remove_solar_charge(grid_charge)
    candidates.append(CandidatePlan(name=CANDIDATE_GRID_CHARGE, slots=grid_charge))

    # 4. Solar-only — keep solar charge + discharge windows; drop grid-charge slots
    solar_only = _copy_slots(baseline_slots)
    _remove_grid_charge(solar_only)
    candidates.append(CandidatePlan(name=CANDIDATE_SOLAR_ONLY, slots=solar_only))

    # 5. Discharge-only — keep discharge slots; drop ALL charge slots
    discharge_only = _copy_slots(baseline_slots)
    _remove_all_charge(discharge_only)
    candidates.append(
        CandidatePlan(name=CANDIDATE_DISCHARGE_ONLY, slots=discharge_only)
    )

    # 6. Aggressive — force-charge during N cheapest future slots,
    #    force-discharge during M most-expensive future slots.
    #    N is derived from remaining battery headroom (Bug 2 fix, issue #416).
    aggressive = _copy_slots(baseline_slots)
    _apply_aggressive_strategy(
        aggressive,
        now,
        max_charge_per_slot,
        current_kwh=current_kwh,
        usable_kwh=usable_kwh,
    )
    candidates.append(CandidatePlan(name=CANDIDATE_AGGRESSIVE, slots=aggressive))

    # 7. MILP — globally-optimal LP solution (requires scipy, falls back gracefully)
    if is_scipy_available():
        milp_slots = solve_milp(
            baseline_slots,
            now,
            current_kwh=current_kwh,
            usable_kwh=usable_kwh,
            max_charge_per_slot=max_charge_per_slot,
            max_discharge_per_slot=max_discharge_per_slot,
            cycle_cost_per_kwh=inp.battery_cycle_cost_per_kwh,
        )
        if milp_slots is not None:
            candidates.append(CandidatePlan(name=CANDIDATE_MILP, slots=milp_slots))
            log_planner(
                "debug",
                "[gen] MILP candidate added (scipy available and solver succeeded)",
            )
        else:
            log_planner(
                "debug",
                "[gen] MILP candidate skipped — solver returned None (infeasible or timeout)",
            )
    else:
        log_planner("debug", "[gen] MILP candidate skipped — scipy not available")

    # Log candidate slot-level recommendations for debugging
    log_planner(
        "debug",
        "[gen] Generated %d candidates: %s",
        len(candidates),
        ", ".join(c.name for c in candidates),
    )
    for cand in candidates:
        charge_slots = [
            s.start.strftime("%d %H:%M")
            for s in cand.slots
            if s.recommendation in _CHARGE_RECS
        ]
        discharge_slots = [
            s.start.strftime("%d %H:%M")
            for s in cand.slots
            if s.recommendation in _DISCHARGE_RECS
        ]
        total_charge = sum(s.batteries_charged for s in cand.slots)
        log_planner(
            "debug",
            "[gen] %s: charge_slots=%d (%s)  discharge_slots=%d (%s)  "
            "total_charge=%.3f kWh",
            cand.name,
            len(charge_slots),
            ", ".join(charge_slots) if charge_slots else "—",
            len(discharge_slots),
            ", ".join(discharge_slots) if discharge_slots else "—",
            total_charge,
        )

    return candidates


# ---------------------------------------------------------------------------
# Private helpers — slot mutation strategies
# ---------------------------------------------------------------------------


def _copy_slots(slots: list[PlannedSlot]) -> list[PlannedSlot]:
    """Return an independent copy of *slots* for candidate isolation.

    Uses a shallow copy of each :class:`PlannedSlot` dataclass.  This is
    safe because every field on ``PlannedSlot`` is either an immutable scalar
    (``float``, ``str | None``) or an immutable named-tuple
    (:class:`~custom_components.hsem.utils.prices.SlotPrice`, ``datetime``).
    There are intentionally **no mutable container fields** (lists, dicts)
    on ``PlannedSlot`` — if any are added in the future this function must be
    updated to use ``copy.deepcopy`` instead.

    Each returned slot is an independent object: mutating ``recommendation``,
    ``batteries_charged``, ``ev_planned_load_kwh``, or any other scalar field
    on a copy does **not** affect the original or any other copy.
    """
    return [copy.copy(s) for s in slots]


def _clear_all_charge_discharge(slots: list[PlannedSlot]) -> None:
    """Reset every charge and discharge recommendation to ``None``.

    ``batteries_charged`` is also zeroed on cleared slots so the SoC
    simulation starts from a clean slate for the no-action candidate.

    ``ev_planned_load_kwh`` is intentionally **not** touched: it represents
    real AC-side demand for EV charging that exists regardless of what the
    battery does.  The no-action candidate still carries EV load in its net
    consumption; only battery scheduling is removed.
    """
    for slot in slots:
        if slot.recommendation in _CHARGE_RECS | _DISCHARGE_RECS:
            slot.recommendation = None
            slot.batteries_charged = 0.0


def _remove_solar_charge(slots: list[PlannedSlot]) -> None:
    """Clear solar-charge slots, leaving grid-charge and discharge intact."""
    for slot in slots:
        if slot.recommendation == Recommendations.BatteriesChargeSolar.value:
            slot.recommendation = None
            slot.batteries_charged = 0.0


def _remove_grid_charge(slots: list[PlannedSlot]) -> None:
    """Clear grid-charge slots, leaving solar-charge and discharge intact."""
    for slot in slots:
        if slot.recommendation == Recommendations.BatteriesChargeGrid.value:
            slot.recommendation = None
            slot.batteries_charged = 0.0


def _remove_all_charge(slots: list[PlannedSlot]) -> None:
    """Clear all charge slots, leaving discharge slots intact."""
    for slot in slots:
        if slot.recommendation in _CHARGE_RECS:
            slot.recommendation = None
            slot.batteries_charged = 0.0


def _apply_aggressive_strategy(
    slots: list[PlannedSlot],
    now: datetime,
    max_charge_per_slot: float,
    *,
    current_kwh: float = 0.0,
    usable_kwh: float = 0.0,
) -> None:
    """Force-charge during the cheapest slots and force-discharge during the priciest.

    This strategy ignores schedule windows and min-price-difference guards.
    It provides an upper-bound on arbitrage potential within the planning horizon.

    Selection criteria:
    - Charge candidates: future slots not already assigned to discharge with the
      lowest import prices.  Charge slots before **all** existing discharge windows
      so that charging is never scheduled after a window it is supposed to serve
      (Bug 5 fix — previously only the *first* discharge window was guarded).
    - Discharge candidates: future slots not already assigned to charge with the
      highest import prices.

    The number of charge slots is derived dynamically from the remaining battery
    headroom so it scales with battery size and horizon length.  Previously a
    hard-coded constant of 3 was used regardless of the horizon, which under-
    utilised the battery for large systems and over-committed it for small ones
    (Bug 2 fix, issue #416).

    Args:
        slots: Mutable slot list to update in place.
        now: Timezone-aware current datetime used to filter past slots.
        max_charge_per_slot: Maximum energy storable per slot (kWh).
        current_kwh: Current battery energy above the discharge floor (kWh).
        usable_kwh: Maximum usable battery capacity (kWh).
    """
    import math

    future = [s for s in slots if as_tz(s.end, now.tzinfo) > now]

    # -----------------------------------------------------------------------
    # Bug 2 fix: derive N dynamically from battery headroom.
    # Compute how many slots are needed to fill the battery from its current
    # charge to max capacity.  Fall back to 3 when capacity data is absent.
    # When the battery is already full (headroom ≈ 0) the strategy claims
    # 0 charge slots — there is no point charging a full battery.
    # -----------------------------------------------------------------------
    headroom_kwh = max(usable_kwh - current_kwh, 0.0)
    if abs(max_charge_per_slot) > 1e-9:
        if headroom_kwh > 1e-9:
            aggressive_charge_slots = math.ceil(headroom_kwh / max_charge_per_slot)
        else:
            aggressive_charge_slots = 0  # battery full — no charging needed
    else:
        aggressive_charge_slots = 3  # safe fallback when inputs are degenerate

    # -----------------------------------------------------------------------
    # Bug 5 fix: guard against ALL existing discharge windows, not just the first.
    # Collect the start of every discharge window so that charge slots placed
    # by the aggressive strategy are never scheduled after any window they
    # would need to serve.
    # -----------------------------------------------------------------------
    discharge_starts = sorted(
        s.start for s in future if s.recommendation in _DISCHARGE_RECS
    )
    # The latest safe end time for a charge slot is the start of the *first*
    # discharge window.  Slots entirely before that boundary are eligible.
    earliest_discharge_start = discharge_starts[0] if discharge_starts else None

    # Identify slots free for charging:
    # - Not already discharging.
    # - Must end *before* the first discharge window (if any exists).
    charge_candidates = sorted(
        (
            s
            for s in future
            if s.recommendation not in _DISCHARGE_RECS
            and (
                earliest_discharge_start is None
                or as_tz(s.end, now.tzinfo) <= earliest_discharge_start
            )
        ),
        key=lambda s: (s.price.import_price, s.start),
    )

    # Identify slots free for discharging (not already charging)
    discharge_candidates = sorted(
        (s for s in future if s.recommendation not in _CHARGE_RECS),
        key=lambda s: (-s.price.import_price, s.start),
    )

    # Apply force-charge to cheapest N slots (N derived dynamically above)
    charged = 0
    for slot in charge_candidates:
        if charged >= aggressive_charge_slots:
            break
        if slot.recommendation in _CHARGE_RECS:
            # Already a charge slot — leave energy as-is, just count it
            charged += 1
            continue
        slot.recommendation = Recommendations.BatteriesChargeGrid.value
        slot.batteries_charged = round(max_charge_per_slot, 3)
        charged += 1

    # Apply force-discharge to most-expensive M slots
    discharged = 0
    for slot in discharge_candidates:
        if discharged >= _AGGRESSIVE_DISCHARGE_SLOTS:
            break
        if slot.recommendation in _DISCHARGE_RECS:
            # Already a discharge slot — leave as-is, just count it
            discharged += 1
            continue
        slot.recommendation = Recommendations.BatteriesDischargeMode.value
        discharged += 1

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
                        Diagnostic floor only — never eligible to win selection.
3. ``passive``        — solar charging where PV surplus exists; no grid charge or
                        forced discharge. Models the inverter default behaviour.
4. ``grid_charge``    — grid-charge slots are kept; solar charging is removed.
5. ``solar_only``     — only solar-charge slots are kept; grid charging cleared.
6. ``discharge_only`` — discharge slots are kept; all charge slots cleared.
7. ``aggressive``     — cheapest N slots forced to grid-charge regardless of
                        schedule; most expensive M slots forced to discharge.
                        N is derived dynamically from battery headroom and
                        max charge per slot so it scales with the horizon and
                        battery size (fix for issue #416 Bug 2).
8. ``milp``           — globally-optimal LP solution (when scipy is available);
                        falls back gracefully if the solver fails.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime

from custom_components.hsem.models.planner_inputs import PlannerInput
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.planner.milp_optimizer import (
    CANDIDATE_MILP,
    is_scipy_available,
    solve_milp,
)
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Candidate name constants — shared with selector so both sides speak the
# same identifiers without re-defining strings.
# ---------------------------------------------------------------------------

CANDIDATE_BASELINE = "baseline"
CANDIDATE_NO_ACTION = "no_action"
CANDIDATE_PASSIVE = "passive"
CANDIDATE_GRID_CHARGE = "grid_charge"
CANDIDATE_SOLAR_ONLY = "solar_only"
CANDIDATE_DISCHARGE_ONLY = "discharge_only"
CANDIDATE_AGGRESSIVE = "aggressive"
CANDIDATE_SOC_PLAN = "soc_plan"

# Re-export MILP candidate name so callers only need to import from here
__all__ = [
    "CANDIDATE_BASELINE",
    "CANDIDATE_NO_ACTION",
    "CANDIDATE_PASSIVE",
    "CANDIDATE_GRID_CHARGE",
    "CANDIDATE_SOLAR_ONLY",
    "CANDIDATE_DISCHARGE_ONLY",
    "CANDIDATE_AGGRESSIVE",
    "CANDIDATE_SOC_PLAN",
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

    # 2. No-action — battery completely idle (diagnostic floor only, never
    #    eligible to win selection).
    no_action = _copy_slots(baseline_slots)
    _clear_all_charge_discharge(no_action)
    candidates.append(CandidatePlan(name=CANDIDATE_NO_ACTION, slots=no_action))

    # 3. Passive — solar charging only where PV surplus exists; no grid charge,
    #    no forced discharge. Models the inverter default behaviour.
    passive = _copy_slots(baseline_slots)
    _apply_passive_solar(passive, now)
    candidates.append(CandidatePlan(name=CANDIDATE_PASSIVE, slots=passive))

    # 4. Grid-charge only — keep discharge windows; drop solar-only charge slots
    grid_charge = _copy_slots(baseline_slots)
    _remove_solar_charge(grid_charge)
    candidates.append(CandidatePlan(name=CANDIDATE_GRID_CHARGE, slots=grid_charge))

    # 5. Solar-only — keep solar charge + discharge windows; drop grid-charge slots
    solar_only = _copy_slots(baseline_slots)
    _remove_grid_charge(solar_only)
    candidates.append(CandidatePlan(name=CANDIDATE_SOLAR_ONLY, slots=solar_only))

    # 6. Discharge-only — keep discharge slots; drop ALL charge slots
    discharge_only = _copy_slots(baseline_slots)
    _remove_all_charge(discharge_only)
    candidates.append(
        CandidatePlan(name=CANDIDATE_DISCHARGE_ONLY, slots=discharge_only)
    )

    # 7. Aggressive — force-charge during N cheapest future slots,
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

    # 8. SoC plan — BatPred-inspired: charge only what's needed for the
    #    upcoming discharge windows, then hold (wait mode) until discharge.
    #    This avoids over-charging the battery when the evening peak only
    #    needs a fraction of the battery's capacity.
    soc_plan = _copy_slots(baseline_slots)
    _apply_soc_plan(
        soc_plan,
        now,
        max_charge_per_slot,
        current_kwh=current_kwh,
        usable_kwh=usable_kwh,
        cycle_cost_per_kwh=inp.battery_cycle_cost_per_kwh,
    )
    candidates.append(CandidatePlan(name=CANDIDATE_SOC_PLAN, slots=soc_plan))

    # 9. MILP — globally-optimal LP solution (requires scipy, falls back gracefully)
    if is_scipy_available():
        milp_slots = solve_milp(
            baseline_slots,
            now,
            current_kwh=current_kwh,
            usable_kwh=usable_kwh,
            max_charge_per_slot=max_charge_per_slot,
            max_discharge_per_slot=max_discharge_per_slot,
            cycle_cost_per_kwh=inp.battery_cycle_cost_per_kwh,
            charge_efficiency_pct=inp.battery_charge_efficiency_pct,
            discharge_efficiency_pct=inp.battery_discharge_efficiency_pct,
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


def _apply_passive_solar(slots: list[PlannedSlot], now: datetime) -> None:
    """Allow solar charging wherever estimated_net_consumption < 0 (PV surplus).

    This models the inverter default behaviour: no grid charging, no forced
    discharge, but passive absorption of PV surplus into the battery.
    estimated_net_consumption is negative when PV production exceeds house
    load (including EV).  The surplus magnitude is used directly as
    batteries_charged.  The SoC simulation caps it against actual battery
    limits afterwards.
    """
    for slot in slots:
        # Clear all active scheduling first
        if slot.recommendation in _CHARGE_RECS | _DISCHARGE_RECS:
            slot.recommendation = None
            slot.batteries_charged = 0.0

        # Passively absorb surplus into battery for future slots only
        # NaN < 0.0 is False in Python so no explicit NaN guard is needed
        if (
            as_tz(slot.end, now.tzinfo) > now
            and slot.estimated_net_consumption is not None
            and slot.estimated_net_consumption < 0.0
        ):
            slot.recommendation = Recommendations.BatteriesChargeSolar.value
            slot.batteries_charged = round(-slot.estimated_net_consumption, 3)


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


def _apply_soc_plan(
    slots: list[PlannedSlot],
    now: datetime,
    max_charge_per_slot: float,
    *,
    current_kwh: float = 0.0,
    usable_kwh: float = 0.0,
    cycle_cost_per_kwh: float = 0.0,
) -> None:
    """BatPred-inspired SoC plan: charge only what's needed, then hold.

    This strategy:
    1. Identifies all discharge windows (slots with BatteriesDischargeMode).
    2. Calculates the total net energy needed across all discharge windows.
    3. Clears all existing charge/discharge recommendations.
    4. Charges only enough to cover the needed energy, using the cheapest
       slots before the first discharge window.
    5. Keeps solar charging where PV surplus exists (free energy).
    6. Leaves remaining slots as None — the seasonal fill pass will assign
       BatteriesWaitMode or BatteriesDischargeMode as appropriate.

    Unlike the aggressive strategy which fills the battery completely, this
    strategy charges only what's strictly needed, avoiding unnecessary
    cycle wear and conversion losses on energy that won't be used.

    Args:
        slots: Mutable slot list to update in place.
        now: Timezone-aware current datetime used to filter past slots.
        max_charge_per_slot: Maximum energy storable per slot (kWh).
        current_kwh: Current battery energy above the discharge floor (kWh).
        usable_kwh: Maximum usable battery capacity (kWh).
    """
    import math

    future = [s for s in slots if as_tz(s.end, now.tzinfo) > now]

    # Step 1: Identify discharge windows and calculate total energy needed
    discharge_slots = [s for s in future if s.recommendation in _DISCHARGE_RECS]
    if not discharge_slots:
        # No discharge windows — nothing to plan for.  Keep solar charging
        # but clear all grid charging.
        for slot in slots:
            if slot.recommendation == Recommendations.BatteriesChargeGrid.value:
                slot.recommendation = None
                slot.batteries_charged = 0.0
        return

    # Total net energy needed across all discharge windows.
    # This is the sum of positive net consumption in each discharge slot.
    total_needed_kwh = sum(
        max(s.estimated_net_consumption, 0.0) for s in discharge_slots
    )

    # Account for discharge efficiency: to deliver total_needed_kwh to the
    # house, the battery must release total_needed_kwh / discharge_eff.
    # We approximate discharge_eff as 0.95 (95 %).
    discharge_eff = 0.95
    battery_energy_needed = total_needed_kwh / discharge_eff

    # Subtract what's already in the battery
    charge_needed = max(battery_energy_needed - current_kwh, 0.0)

    # Cap at usable capacity (don't charge more than the battery can hold)
    charge_needed = min(charge_needed, usable_kwh - current_kwh)
    charge_needed = max(charge_needed, 0.0)

    # Step 2: Clear all existing charge/discharge recommendations
    for slot in slots:
        if slot.recommendation in _CHARGE_RECS | _DISCHARGE_RECS:
            slot.recommendation = None
            slot.batteries_charged = 0.0

    # Step 3: Re-apply discharge window labels (cosmetic — SoC simulation
    # handles the actual physics)
    for slot in discharge_slots:
        slot.recommendation = Recommendations.BatteriesDischargeMode.value

    # Step 4: Apply solar charging where PV surplus exists (free energy)
    charged = 0.0
    for slot in sorted(
        (s for s in future if s.recommendation is None),
        key=lambda x: (x.price.import_price, x.start),
    ):
        if charged >= charge_needed:
            break
        # Solar surplus: net consumption < 0 means PV > house load
        if (
            slot.estimated_net_consumption is not None
            and slot.estimated_net_consumption < 0.0
        ):
            available_solar = abs(slot.estimated_net_consumption)
            energy = min(max_charge_per_slot, charge_needed - charged, available_solar)
            if energy > 0:
                slot.recommendation = Recommendations.BatteriesChargeSolar.value
                slot.batteries_charged = round(energy, 3)
                charged += energy

    # Step 5: Charge remaining needed energy from cheapest grid slots
    # before the first discharge window, but only if the price spread
    # covers the cycle cost (avoid uneconomical cycling).
    first_discharge_start = min(
        as_tz(s.start, now.tzinfo) for s in discharge_slots
    )

    # Average discharge price — what we'd save by discharging instead of importing
    avg_discharge_price = (
        sum(s.price.import_price for s in discharge_slots) / len(discharge_slots)
        if discharge_slots
        else 0.0
    )

    grid_candidates = sorted(
        (
            s
            for s in future
            if s.recommendation is None
            and as_tz(s.end, now.tzinfo) <= first_discharge_start
        ),
        key=lambda x: (x.price.import_price, x.start),
    )

    # Only charge when the price spread covers the cycle cost on both
    # directions (charge + discharge) plus a conversion loss guard.
    # This mirrors the guard in _apply_grid_charge.
    cheapest_price = grid_candidates[0].price.import_price if grid_candidates else 0.0
    price_spread = avg_discharge_price - cheapest_price
    # The effective cost is cycle cost per direction times 2 directions,
    # plus ~10% conversion loss on the charge price.
    conversion_loss_guard = cheapest_price * 0.10  # rough approximation
    min_profitable_spread = 2.0 * cycle_cost_per_kwh + conversion_loss_guard

    if price_spread < min_profitable_spread - 1e-9:
        # Spread too small — skip grid charging, just let solar charging happen
        pass
    else:
        for slot in grid_candidates:
            if charged >= charge_needed:
                break
            energy = min(max_charge_per_slot, charge_needed - charged)
            if energy > 0:
                slot.recommendation = Recommendations.BatteriesChargeGrid.value
                slot.batteries_charged = round(energy, 3)
                charged += energy

    # Step 6: Remaining slots stay as None — the seasonal fill pass will
    # assign BatteriesWaitMode or BatteriesDischargeMode as appropriate.

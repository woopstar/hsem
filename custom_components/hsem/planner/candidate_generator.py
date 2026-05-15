"""Candidate plan generator for the HSEM planner (issue #296).

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
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime

from custom_components.hsem.datetime_utils import as_tz
from custom_components.hsem.models.planner_inputs import PlannerInput
from custom_components.hsem.models.planner_outputs import PlannedSlot
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

# Number of cheapest/most-expensive slots the aggressive strategy commandeers
_AGGRESSIVE_CHARGE_SLOTS = 3
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
    aggressive = _copy_slots(baseline_slots)
    _apply_aggressive_strategy(aggressive, now, max_charge_per_slot)
    candidates.append(CandidatePlan(name=CANDIDATE_AGGRESSIVE, slots=aggressive))

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
) -> None:
    """Force-charge during the cheapest slots and force-discharge during the priciest.

    This strategy ignores schedule windows and min-price-difference guards.
    It provides an upper-bound on arbitrage potential within the planning horizon.

    Selection criteria:
    - Charge candidates: future slots not already assigned to discharge with the
      lowest import prices.
    - Discharge candidates: future slots not already assigned to charge with the
      highest import prices.

    Both sets are clamped to :data:`_AGGRESSIVE_CHARGE_SLOTS` and
    :data:`_AGGRESSIVE_DISCHARGE_SLOTS` slots respectively to avoid committing
    the entire planning horizon to a single extreme strategy.

    Args:
        slots: Mutable slot list to update in place.
        now: Timezone-aware current datetime used to filter past slots.
        max_charge_per_slot: Maximum energy storable per slot (kWh).
    """
    future = [s for s in slots if as_tz(s.end, now.tzinfo) > now]

    # Determine the earliest future discharge slot start so that aggressive
    # charging does not bleed into or past discharge windows.
    discharge_starts = sorted(
        s.start for s in future if s.recommendation in _DISCHARGE_RECS
    )
    earliest_discharge_start = discharge_starts[0] if discharge_starts else None

    # Identify slots free for charging:
    # - Not already discharging.
    # - Must end *before* the first discharge window (if one exists) so that
    #   the aggressive strategy does not place charge slots after the window
    #   it is supposed to serve.
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

    # Apply force-charge to cheapest N slots
    charged = 0
    for slot in charge_candidates:
        if charged >= _AGGRESSIVE_CHARGE_SLOTS:
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

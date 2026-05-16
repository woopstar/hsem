"""Candidate plan selector for the HSEM planner (issue #296).

This module scores every candidate produced by
:mod:`~custom_components.hsem.planner.candidate_generator`, validates each
one, and picks the best valid plan.

Selection algorithm
-------------------
1. Run :func:`~custom_components.hsem.planner.soc_simulation.simulate_soc`
   on each candidate to populate ``grid_import_kwh``, ``grid_export_kwh``,
   and ``estimated_battery_soc``.
2. Validate the candidate — a plan is *invalid* if the simulated SoC ever
   violates the end-of-discharge floor by more than a small numerical
   tolerance.  (The SoC simulation already clamps, so this is a sanity check
   for edge cases.)
3. Score all valid candidates with :func:`~cost_function.score_plan`.
4. Pick the candidate with the **lowest total cost** (lower = better).
5. If no candidate is valid (degenerate edge case), fall back to ``baseline``.
6. Return the winning slots plus a list of
   :class:`~custom_components.hsem.models.planner_outputs.RejectedPlan`
   entries describing every non-selected candidate and the reason it lost.

Design constraints
------------------
- **Pure Python, no Home Assistant imports** — testable with plain pytest.
- All mutations operate on the per-candidate slot copies; the caller's
  baseline slots are never touched.
- The selector does NOT re-run the full scheduling pipeline; it only runs
  the SoC simulation (which is fast) and the cost function (also fast).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from custom_components.hsem.models.planner_outputs import RejectedPlan
from custom_components.hsem.planner.candidate_generator import (
    CANDIDATE_BASELINE,
    CANDIDATE_NO_ACTION,
    CandidatePlan,
)
from custom_components.hsem.planner.cost_function import CostWeights, score_plan
from custom_components.hsem.planner.soc_simulation import simulate_soc
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.recommendations import Recommendations

# SoC floor tolerance — plans are accepted even if they dip this many
# percentage points below end_of_discharge_soc_pct (rounding / simulation
# artefact allowance).
_SOC_TOLERANCE_PCT = 0.5


def select_best_candidate(
    candidates: list[CandidatePlan],
    *,
    now: datetime,
    current_kwh: float,
    usable_kwh: float,
    max_soc_capacity_kwh: float,
    max_charge_per_slot: float,
    max_discharge_per_slot: float | None,
    rated_kwh: float,
    end_of_discharge_soc_pct: float,
    cost_weights: CostWeights,
    slot_duration_hours: float,
    charge_efficiency_pct: float = 100.0,
    discharge_efficiency_pct: float = 100.0,
    replacement_price_per_kwh: float | None = None,
) -> tuple[CandidatePlan, list[RejectedPlan]]:
    """Score all candidates, validate them, and return the best one.

    Each candidate in *candidates* has its SoC simulation run in place so
    that the cost function has access to ``grid_import_kwh``,
    ``grid_export_kwh``, and ``estimated_battery_soc`` on every slot.

    Args:
        candidates:
            List of candidate plans produced by
            :func:`~candidate_generator.generate_candidates`.  Modified in
            place (SoC simulation writes to each plan's slot list).
        now:
            Timezone-aware current datetime.
        current_kwh:
            Energy currently stored above the discharge floor (kWh).
        usable_kwh:
            Maximum usable energy (max_soc − min_soc expressed in kWh).
        max_soc_capacity_kwh:
            Absolute ceiling imposed by ``battery_max_soc_pct`` in usable kWh.
        max_charge_per_slot:
            Maximum energy chargeable per slot (kWh, post-conversion-loss).
        max_discharge_per_slot:
            Maximum energy dischargeable per slot (kWh).  ``None`` = unlimited.
        rated_kwh:
            Nameplate battery capacity (kWh).
        end_of_discharge_soc_pct:
            End-of-discharge SoC floor (0-100 %).
        cost_weights:
            Cost weights for :func:`~cost_function.score_plan`.
        slot_duration_hours:
            Duration of each slot in hours (e.g. 0.25 for 15-min slots).
        charge_efficiency_pct:
            Charge-side efficiency (0-100 %).  Forwarded to
            :func:`~soc_simulation.simulate_soc`.  Defaults to 100 %.
        discharge_efficiency_pct:
            Discharge-side efficiency (0-100 %).  Forwarded to
            :func:`~soc_simulation.simulate_soc`.  Defaults to 100 %.
        replacement_price_per_kwh:
            Currency-per-kWh price used by :func:`~cost_function.score_plan`
            to evaluate the terminal-SoC opportunity cost (issue #413).
            A conservative choice is the average future import price across
            the horizon.  ``None`` disables the terminal-SoC term.

    Returns:
        A ``(winner, rejected_plans)`` tuple where *winner* is the
        :class:`CandidatePlan` with the lowest valid selector
        :attr:`~cost_function.PlanCostBreakdown.score` and *rejected_plans*
        lists every non-selected candidate with an explanation of why it
        was not chosen.
    """
    # --- Step 1 & 2: simulate and validate each candidate ---------------
    for candidate in candidates:
        simulate_soc(
            candidate.slots,
            now,
            current_kwh,
            usable_kwh,
            max_soc_capacity_kwh,
            max_charge_per_slot,
            max_discharge_per_slot,
            rated_kwh=rated_kwh,
            end_of_discharge_soc_pct=end_of_discharge_soc_pct,
            charge_efficiency_pct=charge_efficiency_pct,
            discharge_efficiency_pct=discharge_efficiency_pct,
        )
        candidate.is_valid, candidate.rejection_reason = _validate_candidate(
            candidate, end_of_discharge_soc_pct
        )
        log_planner(
            "debug",
            "[selector] candidate=%-20s  valid=%s  reason=%s",
            candidate.name,
            candidate.is_valid,
            candidate.rejection_reason if candidate.rejection_reason else "(none)",
        )

    # --- Step 3: score valid candidates ----------------------------------
    valid = [c for c in candidates if c.is_valid]
    log_planner(
        "debug",
        "[selector] %d/%d candidates valid after SoC validation",
        len(valid),
        len(candidates),
    )

    # --- Step 4: pick winner (lowest cost) among eligible candidates ------
    # Exclude no_action from winner selection — it is a diagnostic floor
    # only and must never win.
    eligible = [c for c in valid if c.name != CANDIDATE_NO_ACTION]

    if not eligible:
        # Degenerate case — fall back to baseline regardless of validity
        winner = _find_by_name(candidates, CANDIDATE_BASELINE) or candidates[0]
        winner.is_valid = True
        winner.rejection_reason = ""
        log_planner(
            "warning", "[selector] No eligible candidates — falling back to baseline"
        )
    else:
        # Score all valid candidates (including no_action for diagnostics)
        for candidate in valid:
            candidate._cost = score_plan(  # type: ignore[attr-defined]
                candidate.slots,
                cost_weights,
                slot_duration_hours=slot_duration_hours,
                now=now,
                initial_battery_kwh=current_kwh,
                replacement_price_per_kwh=replacement_price_per_kwh,
            )
            c_cost = candidate._cost  # type: ignore[attr-defined]
            log_planner(
                "debug",
                "[selector] score  candidate=%-20s  "
                "score=%.4f  total_cost=%.4f  import=%.4f  export_rev=%.4f  "
                "conv_loss=%.4f  cycle=%.4f  soc_pen=%.4f  term_soc=%.4f",
                candidate.name,
                c_cost.score,
                c_cost.total_cost,
                c_cost.import_cost,
                c_cost.export_revenue,
                c_cost.conversion_loss_cost,
                c_cost.cycle_cost,
                c_cost.soc_penalty,
                c_cost.terminal_soc_value,
            )

            # Diagnostic: surface the candidate's terminal SoC trajectory so
            # it is obvious WHY a given candidate's terminal_soc_value has the
            # value it does.  When two candidates have identical term_soc it
            # means they converge to the same end-of-horizon SoC; this trail
            # makes that visible without needing a debugger.
            _future_tail = [s for s in candidate.slots if s.end > now][-3:]
            if _future_tail:
                trail = "  ".join(
                    f"{s.start.strftime('%d %H:%M')}→{s.end.strftime('%H:%M')} "
                    f"rec={s.recommendation or '(none)'}  "
                    f"cap={s.estimated_battery_capacity:.3f}  "
                    f"soc={s.estimated_battery_soc:.1f}%"
                    for s in _future_tail
                )
                log_planner(
                    "debug",
                    "[selector] tail   candidate=%-20s  %s",
                    candidate.name,
                    trail,
                )

        # Sort by selector score ascending; baseline wins ties (it comes first)
        eligible_sorted = sorted(
            eligible,
            key=lambda c: (
                c._cost.score,  # type: ignore[attr-defined]
                # Stable tie-break: baseline index is 0, so it wins ties
                next(
                    (i for i, x in enumerate(candidates) if x is c),
                    len(candidates),
                ),
            ),
        )
        winner = eligible_sorted[0]
        log_planner(
            "info",
            "[selector] SELECTED candidate=%-20s  score=%.4f  total_cost=%.4f",
            winner.name,
            winner._cost.score,  # type: ignore[attr-defined]
            winner._cost.total_cost,  # type: ignore[attr-defined]
        )

    # --- Step 5: build rejected-plan entries for all non-winners ---------
    rejected: list[RejectedPlan] = []

    for candidate in candidates:
        if candidate is winner:
            continue

        if not candidate.is_valid:
            reason = candidate.rejection_reason
        elif candidate.name == CANDIDATE_NO_ACTION:
            reason = (
                "Diagnostic floor only — excluded from winner selection. "
                "The no_action candidate models a fully idle battery and "
                "is never a realistic operating choice."
            )
        else:
            winner_score = getattr(
                getattr(winner, "_cost", None), "score", float("inf")
            )
            candidate_score = getattr(
                getattr(candidate, "_cost", None), "score", float("inf")
            )
            diff = round(candidate_score - winner_score, 4)
            if diff > 1e-6:
                reason = (
                    f"Higher selector score than selected plan "
                    f"({candidate_score:.4f} vs {winner_score:.4f}; "
                    f"Δ = +{diff:.4f})."
                )
            else:
                reason = (
                    f"Tied or marginally worse score "
                    f"({candidate_score:.4f}); baseline preferred."
                )

        rejected.append(
            RejectedPlan(
                name=candidate.name,
                reason=reason,
                # estimated_cost surfaces the selector score so dashboards
                # and tests sort rejected plans the same way the selector
                # ranked them.
                estimated_cost=getattr(getattr(candidate, "_cost", None), "score", 0.0),
            )
        )

    return winner, rejected


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_candidate(
    candidate: CandidatePlan,
    end_of_discharge_soc_pct: float,
) -> tuple[bool, str]:
    """Return ``(is_valid, rejection_reason)`` for *candidate*.

    A plan is invalid when any slot's ``estimated_battery_soc`` falls below
    the end-of-discharge floor by more than :data:`_SOC_TOLERANCE_PCT`.
    The SoC simulation already clamps discharges, so this catches numerical
    edge cases only.

    Args:
        candidate: The candidate to validate.
        end_of_discharge_soc_pct: Minimum allowed battery SoC (0-100).

    Returns:
        ``(True, "")`` when valid; ``(False, reason_string)`` when invalid.
    """
    floor = end_of_discharge_soc_pct - _SOC_TOLERANCE_PCT
    for slot in candidate.slots:
        soc = slot.estimated_battery_soc
        if soc > 0 and soc < floor:
            return (
                False,
                (
                    f"SoC {soc:.1f}% dropped below floor "
                    f"{end_of_discharge_soc_pct:.1f}% "
                    f"at slot starting {slot.start.isoformat()}."
                ),
            )
    return True, ""


def _find_by_name(candidates: list[CandidatePlan], name: str) -> CandidatePlan | None:
    """Return the first candidate with the given name, or ``None``."""
    return next((c for c in candidates if c.name == name), None)


def replacement_price_from_next_discharge(
    slots: list,
    now: datetime,
    top_n: int = 4,
) -> float | None:
    """Derive the terminal-SoC replacement price from the next discharge window.

    The energy stored at end-of-horizon is worth what it would cost to
    re-purchase that energy from the grid during the **first** upcoming
    discharge schedule window.  Within that window the battery discharges
    in priority order from the most expensive slots, so we use the average
    of the *top_n* most expensive import prices within that window.

    In a 48h or 72h horizon the planner marks ``BatteriesDischargeMode``
    across all days, but the replacement price must reflect only the
    closest discharge window — not windows 2+ days away.  We identify the
    first window by collecting all future discharge slots, sorting them by
    start time, and taking the first contiguous block of slots belonging
    to the same schedule occurrence.

    Args:
        slots:
            Any candidate's populated slot list (must have
            ``recommendation``, ``price.import_price``, ``start`` set).
        now:
            Timezone-aware current datetime.  Past slots are excluded.
        top_n:
            Number of most expensive discharge slots to average over.
            Default 4 corresponds to ~1 hour at 15-min resolution.

    Returns:
        Replacement price in currency/kWh, or ``None`` when no future
        discharge slot exists.
    """
    # Collect all future discharge slots sorted by start time
    future_discharge = sorted(
        [
            slot
            for slot in slots
            if (
                slot.recommendation == Recommendations.BatteriesDischargeMode.value
                and as_tz(slot.start, now.tzinfo) > now
                and not math.isnan(slot.price.import_price)
            )
        ],
        key=lambda s: as_tz(s.start, now.tzinfo),
    )

    if not future_discharge:
        return None

    # Find the first contiguous block of discharge slots.  A 15-min gap
    # between consecutive discharge slots signals a new schedule occurrence
    # (the gap between discharge windows).  We take only the first block.
    GAP_THRESHOLD = timedelta(minutes=20)
    first_block: list = [future_discharge[0]]
    tz = now.tzinfo
    for slot in future_discharge[1:]:
        prev_end = as_tz(first_block[-1].end, tz)
        this_start = as_tz(slot.start, tz)
        if this_start - prev_end <= GAP_THRESHOLD:
            first_block.append(slot)
        else:
            break  # reached the next schedule occurrence

    # Average the top_n most expensive import prices within the first block
    first_block.sort(key=lambda s: s.price.import_price, reverse=True)
    top = [s.price.import_price for s in first_block[:top_n]]
    return sum(top) / len(top) if top else None

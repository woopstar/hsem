"""Plan cost function for the HSEM planner (issues #295, #413).

This module scores a candidate plan (a fully-populated list of
:class:`~custom_components.hsem.models.planner_outputs.PlannedSlot` objects)
and exposes two distinct aggregate numbers:

- :attr:`PlanCostBreakdown.total_cost` — the **real-money outcome** of the
  plan within the horizon.  Sum of grid import cost minus export revenue
  plus battery cycle (depreciation) cost plus round-trip conversion loss
  cost.  Auditable; directly comparable to an electricity bill.
- :attr:`PlanCostBreakdown.score` — the **selector objective**.  Equals
  ``total_cost`` plus every synthetic penalty (SoC guard, grid limit,
  override) plus the terminal-SoC opportunity cost.  The candidate selector
  picks the plan with the **lowest score**, not the lowest money cost.

Cost components
---------------
The cost function aggregates eight independently-tunable terms:

Money terms (sum to ``total_cost``):

1. **Import cost** — energy imported from the grid × the sanitised
   (non-negative) import price.  Negative spot prices are clamped to 0 for
   this term — mirrors ``milp_optimizer.py``'s ``p_imp_obj`` clamp, so a
   negative price is never scored as a profit for importing (issue #655).
2. **Export revenue** — energy exported to the grid × export price
   (negative contribution, i.e. revenue reduces total cost).
3. **Battery conversion loss** — energy lost during a charge/discharge cycle,
   priced at the sanitised (non-negative) import price of its own slot —
   the price of the energy that was lost.  Same clamp as import cost.
4. **Battery cycle cost** — depreciation per kWh cycled, derived from the
   battery's purchase price, rated capacity, and expected lifetime cycles.

Selector-only terms (added on top of ``total_cost`` to produce ``score``):

5. **SoC penalties** — quadratic penalty when the end-of-slot SoC is too low
   (below the configured ``min_soc_pct`` guard) or too high (above the
   configured ``max_soc_pct`` guard), multiplied by a configurable weight.
6. **Grid limit penalty** — penalty when grid import or export in any slot
   exceeds the configured grid power limit, proportional to the excess energy.
7. **Override penalty** — per-slot cost added for any slot whose recommendation
   was forced by an override (e.g. read-only mode, manual schedule).  Penalises
   plans that deviate from the hardware's natural optimal state.
8. **Terminal SoC value** — per-slot opportunity cost of charging/discharging,
   capped by the differential between ``replacement_price_per_kwh`` and that
   slot's own sanitised import price:
   ``terminal_premium[t] = max(0, replacement_price_per_kwh - imp_price_obj[t])``.
   Discharging a slot incurs ``+terminal_premium[t]`` per kWh; charging earns
   ``-terminal_premium[t]`` per kWh.  Summed across all slots.  This mirrors
   ``milp_optimizer.py``'s ``c_obj`` terminal-SoC term exactly, so the
   selector's score always matches what the LP actually optimised for
   (issue #655) — when ``replacement_price_per_kwh <= imp_price_obj[t]`` for
   every slot, this reduces to the same net effect as the old flat
   ``(initial_kwh − final_kwh) × replacement_price_per_kwh`` formula, but it
   no longer *over-penalises* discharge in slots where the replacement price
   does not exceed that slot's own import price.

All monetary values are in the caller's local currency.

Design constraints
------------------
- **Pure Python, no Home Assistant imports** — testable with plain pytest.
- **Additive, independently-disableable terms** — any weight set to 0 disables
  that penalty without touching the others.
- **Float-safe** — NaN prices are treated as 0.0 rather than propagating.
- **Immutable input** — slots are *never* mutated; the function is a pure
  read-only scan.
- **Money / selector split** — ``total_cost`` never includes synthetic
  penalties; ``score`` always does.  The selector minimises ``score``.

Backward compatibility
----------------------
:attr:`PlanCostBreakdown.total` is preserved as a deprecated alias for
``score`` so existing code and tests that compared plans by ``.total``
keep selecting the same winner.  New code should use ``total_cost`` (money)
or ``score`` (selector) explicitly.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.cost_helpers import (
    _is_override_slot,
    _resolve_cycle_cost,
)
from custom_components.hsem.planner.cost_types import (  # noqa: F401
    CostWeights,
    PlanCostBreakdown,
)
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import clamp_efficiency
from custom_components.hsem.utils.recommendations import Recommendations

# Re-export CostWeights and PlanCostBreakdown so existing importers don't break.
__all__ = ["CostWeights", "PlanCostBreakdown", "compare_plans", "score_plan"]

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_plan(
    slots: Sequence[PlannedSlot],
    weights: CostWeights | None = None,
    *,
    slot_duration_hours: float = 1.0,
    grid_limit_kw: float | None = None,
    now: datetime | None = None,
    initial_battery_kwh: float | None = None,
    replacement_price_per_kwh: float | None = None,
) -> PlanCostBreakdown:
    """Score a candidate plan and return a full cost breakdown.

    This is a **pure read-only function** — the slot list is never mutated.
    NaN price values are treated as ``0.0`` to avoid silent propagation.

    The grid limit can be passed either via ``weights.grid_limit_kw`` or via
    the keyword argument ``grid_limit_kw``; the keyword argument takes
    precedence when not ``None``.

    Past slots are skipped entirely.  When *now* is provided a slot is
    considered past when ``slot.end <= now``.  When *now* is ``None`` the
    function falls back to checking
    ``slot.recommendation == Recommendations.TimePassed.value``, which is
    the sentinel written by the slot-population step on completed slots.
    Either way, including past slots in the SoC-guard penalty would
    generate a false ``soc_low_penalty`` because the simulator zeros
    ``estimated_battery_soc_pct`` on past slots as a sentinel value.

    Two aggregate numbers are returned (issue #413):

    - ``total_cost`` — money outcome only.  Equals
      ``import_cost − export_revenue + cycle_cost + conversion_loss_cost``.
    - ``score`` — selector objective.  Equals ``total_cost`` plus all
      synthetic penalties (SoC guard, grid limit, override) and the
      terminal-SoC opportunity cost.  The candidate selector minimises
      this value.

    Terminal-SoC accounting (the spec-mandated
    ``terminal_soc_penalty_or_credit`` term) is enabled when both
    ``initial_battery_kwh`` and ``replacement_price_per_kwh`` are provided.
    It prevents the selector from preferring plans that look "cheap" only
    because they empty the battery before the end of the horizon.

    Args:
        slots:
            Ordered list of :class:`PlannedSlot` objects representing one
            candidate plan.  Typically the ``slots`` field of a
            :class:`~custom_components.hsem.models.planner_outputs.PlannerOutput`.
        weights:
            Cost weights and configuration.  Defaults to
            :class:`CostWeights` with all-default values when ``None``.
        slot_duration_hours:
            Duration of each slot in hours.  Used to convert per-slot energy
            (kWh) to power (kW) for the grid-limit check.  Defaults to 1.0
            (hourly slots).
        grid_limit_kw:
            Override for the grid power limit in kW.  When provided, it
            supersedes ``weights.grid_limit_kw``.  ``None`` leaves the
            weights value unchanged.
        now:
            Timezone-aware current datetime.  When provided, any slot whose
            ``end`` is at or before *now* is skipped.  When ``None`` the
            fallback sentinel check (``recommendation == TimePassed``) is
            used instead.
        initial_battery_kwh:
            Energy stored above the discharge floor (kWh) at the start of
            the horizon.  Required (together with
            ``replacement_price_per_kwh``) to enable terminal-SoC accounting.
            ``None`` disables the term.
        replacement_price_per_kwh:
            Currency-per-kWh price used to value the change in stored
            battery energy across the horizon.  A conservative choice is the
            *average future import price* across the planning horizon.
            Required (together with ``initial_battery_kwh``) to enable
            terminal-SoC accounting.  ``None`` disables the term.

    Returns:
        A :class:`PlanCostBreakdown` containing every cost component, the
        money ``total_cost``, and the selector ``score``.
        **Lower ``score`` = better plan** (this is what the selector
        minimises).

    Examples:
        >>> from datetime import datetime
        >>> from zoneinfo import ZoneInfo
        >>> from custom_components.hsem.models.planner_outputs import PlannedSlot
        >>> from custom_components.hsem.utils.prices import SlotPrice
        >>> tz = ZoneInfo("Europe/Copenhagen")
        >>> start = datetime(2024, 6, 15, 0, 0, tzinfo=tz)
        >>> from datetime import timedelta
        >>> slot = PlannedSlot(
        ...     start=start,
        ...     end=start + timedelta(hours=1),
        ...     price=SlotPrice(import_price=0.20, export_price=0.05),
        ...     grid_import_kwh=1.0,
        ...     grid_export_kwh=0.0,
        ...     estimated_battery_soc=50.0,
        ... )
        >>> bd = score_plan([slot])
        >>> bd.import_cost
        0.2
        >>> bd.total_cost
        0.2
        >>> bd.score
        0.2
        >>> bd.total  # deprecated alias for score
        0.2
    """
    if weights is None:
        weights = CostWeights()

    log_planner(
        "debug",
        "[cost] score_plan  slots=%d  initial_battery=%s  repl_price=%s",
        len(slots),
        f"{initial_battery_kwh:.3f}" if initial_battery_kwh is not None else "None",
        (
            f"{replacement_price_per_kwh:.6f}"
            if replacement_price_per_kwh is not None
            else "None"
        ),
    )

    # Resolve grid limit (keyword arg takes precedence)
    effective_grid_limit_kw: float | None = (
        grid_limit_kw if grid_limit_kw is not None else weights.grid_limit_kw
    )

    cycle_cost_kwh = _resolve_cycle_cost(weights)

    # Resolve the effective roundtrip loss fraction.
    # When separate charge/discharge efficiencies are provided (both non-default),
    # we compute the roundtrip loss from them:
    #   roundtrip_loss = 1 - (charge_eff × discharge_eff)
    # Compute roundtrip loss from charge/discharge efficiencies.
    charge_eff = clamp_efficiency(weights.charge_efficiency_pct)
    discharge_eff = clamp_efficiency(weights.discharge_efficiency_pct)

    import_cost = 0.0
    export_revenue = 0.0
    conversion_loss_cost = 0.0
    cycle_cost_total = 0.0
    soc_penalty = 0.0
    grid_limit_penalty = 0.0
    override_penalty = 0.0
    terminal_soc_value = 0.0

    # Discounted versions for the selector score (total_cost stays raw).
    # time_discount_rate < 1.0 means future savings are worth less.
    discount_rate = weights.time_discount_rate
    use_discount = discount_rate < 1.0 - 1e-9 and now is not None
    import_cost_disc = 0.0
    export_revenue_disc = 0.0
    conversion_loss_cost_disc = 0.0
    cycle_cost_total_disc = 0.0
    soc_penalty_disc = 0.0
    grid_limit_penalty_disc = 0.0

    _time_passed_value = Recommendations.TimePassed.value

    for slot in slots:
        # Skip past slots entirely.  The SoC simulation zeros
        # estimated_battery_soc_pct on past slots as a sentinel, which would
        # falsely trigger the SoC-low penalty on every past slot.
        # Energy-flow fields (grid_import_kwh, grid_export_kwh, etc.) are
        # no longer zeroed for past slots (they are preserved for the daily
        # plan-vs-actual tracker), but skipping past slots here has no
        # effect on import cost, cycle cost, or any other term since they
        # belong to a completed time period.
        #
        # Primary guard: slot.end <= now (time-based, no string coupling).
        # Fallback guard: recommendation == TimePassed (used when now is None,
        # e.g. in unit tests that call score_plan without a clock).
        if now is not None:
            if slot.end <= now:
                continue
        elif slot.recommendation == _time_passed_value:
            continue

        # Compute time discount for this slot.
        # discount = discount_rate ^ hours_from_now
        # Past slots are already skipped above, so hours_ahead >= 0.
        if use_discount:
            assert (
                now is not None
            )  # guarded by use_discount = discount_rate < 1.0 and now is not None
            slot_mid = slot.start + (slot.end - slot.start) / 2
            hours_ahead = max((slot_mid - now).total_seconds() / 3600.0, 0.0)
            discount = discount_rate**hours_ahead
        else:
            discount = 1.0

        imp_price = slot.price.import_price
        exp_price = slot.price.export_price

        # Treat NaN prices as zero to avoid propagation
        if math.isnan(imp_price):
            imp_price = 0.0
        if math.isnan(exp_price):
            exp_price = 0.0

        # Sanitised (non-negative) import price — mirrors milp_optimizer.py's
        # p_imp_obj clamp.  A negative spot price must never be scored as a
        # profit for importing or for lossy conversion: the MILP's own
        # objective never rewards those events (its gi[t]/ec[t]/ed[t]
        # coefficients use p_imp_obj = max(p_imp, 0)), so the selector must
        # value the identical physical decisions the same way, or its score
        # no longer matches what the LP actually optimised for (issue #655).
        # The raw (possibly negative) imp_price is still used for export
        # clamping logic elsewhere and is unaffected by this sanitisation.
        imp_price_obj = max(imp_price, 0.0)

        # 1. Import cost — grid_import_kwh already reflects the extra grid draw
        #    needed to store energy through the charge efficiency (i.e. the
        #    simulation writes grid_import_kwh = charge_stored / charge_eff).
        if slot.grid_import_kwh > 1e-9:
            cost = slot.grid_import_kwh * imp_price_obj
            import_cost += cost
            import_cost_disc += cost * discount

        # 2. Export revenue — clamp export prices below export_min_price
        #    to 0 to match the applier's physical export block and the MILP's
        #    clamping (milp_optimizer.py).  Without this the cost function
        #    would report revenue for exports that can never happen.
        if slot.grid_export_kwh > 1e-9:
            effective_exp_price = exp_price
            if (
                weights.export_min_price > 1e-9
                and effective_exp_price < weights.export_min_price
            ):
                effective_exp_price = 0.0
            rev = slot.grid_export_kwh * effective_exp_price
            export_revenue += rev
            export_revenue_disc += rev * discount

        # 3. Conversion loss cost — opportunity cost of energy lost in the
        #    round-trip.  The loss occurred at purchase time (charge slot) and
        #    at delivery time (discharge slot).
        #
        #    Charge-side loss is priced at the sanitised (non-negative)
        #    import price of the charge slot — the price of the energy that
        #    was lost during input (issue #655).
        #
        #    Discharge-side loss is priced based on the slot's actual
        #    resolved energy flow (destination-aware pricing, issue #641):
        #
        #    - If the slot is a net EXPORTER (grid_export_kwh > 0): the
        #      discharge is destined for export, so the lost energy's true
        #      marginal value is the export price (foregone export revenue).
        #      Use the sanitised export price (after min-export-price clamp,
        #      floored at 0).
        #    - Otherwise (slot is importing or idle): the discharge serves
        #      house load, so the lost energy's true marginal value is the
        #      import price (avoided import cost).  Use imp_price_obj.
        #
        #    This differs from the LP's pre-solve objective coefficient,
        #    which uses imp_price_obj unconditionally as a conservative
        #    approximation (the LP cannot know the destination before
        #    solving).  The scorer has access to the solved energy flows and
        #    can make the correct destination-aware valuation.  This is not
        #    a violation of the LP/cost-function consistency rule — the
        #    rule requires that the LP's decisions are scoreable
        #    consistently, not that a necessarily-uninformed pre-solve
        #    coefficient matches a fully-informed post-solve number.
        charge_loss_fraction = 1.0 - charge_eff
        discharge_loss_fraction = 1.0 - discharge_eff
        if slot.batteries_charged_kwh > 1e-9 and charge_loss_fraction > 1e-9:
            lost_kwh_charge = slot.batteries_charged_kwh * charge_loss_fraction
            conv = lost_kwh_charge * imp_price_obj
            conversion_loss_cost += conv
            conversion_loss_cost_disc += conv * discount
        if slot.batteries_discharged_kwh > 1e-9 and discharge_loss_fraction > 1e-9:
            lost_kwh_discharge = slot.batteries_discharged_kwh * discharge_loss_fraction
            # Destination-aware discharge loss pricing (issue #641).
            # If the slot is a net exporter, the discharge is destined for
            # export — price loss at the export price (foregone revenue).
            # Otherwise, price at the import price (avoided import cost).
            if slot.grid_export_kwh > 1e-9:
                # Export-destined discharge: use sanitised export price.
                p_loss = exp_price
                if (
                    weights.export_min_price > 1e-9
                    and p_loss < weights.export_min_price
                ):
                    p_loss = 0.0
                p_loss = max(p_loss, 0.0)
            else:
                # House-load-covering discharge: use import price (unchanged).
                p_loss = imp_price_obj
            conv = lost_kwh_discharge * p_loss
            conversion_loss_cost += conv
            conversion_loss_cost_disc += conv * discount

        # 4. Battery cycle depreciation
        throughput_kwh = max(slot.batteries_charged_kwh, slot.batteries_discharged_kwh)
        if throughput_kwh > 1e-9 and cycle_cost_kwh > 1e-9:
            cycle = throughput_kwh * cycle_cost_kwh
            cycle_cost_total += cycle
            cycle_cost_total_disc += cycle * discount

        # 5. SoC guard penalties (quadratic in the violation magnitude).
        soc = slot.estimated_battery_soc_pct
        if soc < weights.min_soc_pct:
            violation = weights.min_soc_pct - soc
            pen = weights.soc_low_penalty_weight * violation**2
            soc_penalty += pen
            soc_penalty_disc += pen * discount
        elif soc > weights.max_soc_pct:
            violation = soc - weights.max_soc_pct
            pen = weights.soc_high_penalty_weight * violation**2
            soc_penalty += pen
            soc_penalty_disc += pen * discount

        # 6. Grid limit penalty
        if effective_grid_limit_kw is not None and slot_duration_hours > 1e-9:
            import_kw = slot.grid_import_kwh / slot_duration_hours
            export_kw = slot.grid_export_kwh / slot_duration_hours
            for kw in (import_kw, export_kw):
                excess_kw = kw - effective_grid_limit_kw
                if excess_kw > 1e-9:
                    pen = (
                        excess_kw
                        * slot_duration_hours
                        * weights.grid_limit_penalty_per_kwh
                    )
                    grid_limit_penalty += pen
                    grid_limit_penalty_disc += pen * discount

        # 7. Override penalty
        if _is_override_slot(slot) and abs(weights.override_penalty_per_slot) > 1e-9:
            override_penalty += weights.override_penalty_per_slot

        # 8. Terminal-SoC opportunity cost (selector-only).
        #
        # Per-slot incentive capped by the opportunity-cost DIFFERENTIAL
        # between the replacement price and this slot's own (sanitised)
        # import price — mirrors milp_optimizer.py's terminal_premium term
        # exactly, so the selector's score matches what the LP actually
        # optimised for (issue #655).  When replacement_price <=
        # imp_price_obj[t], the premium is zero: charging/discharging in
        # that slot is not discouraged or encouraged by terminal-SoC alone.
        # Charging (batteries_charged_kwh) earns a credit; discharging
        # (batteries_discharged_kwh) incurs a penalty.  Undiscounted —
        # matches milp_optimizer.py's treatment of this term.
        #
        # Gated on initial_battery_kwh as well (even though this per-slot
        # formula no longer needs its value) to preserve the documented
        # enablement contract: terminal-SoC accounting requires BOTH
        # initial_battery_kwh and replacement_price_per_kwh to be provided.
        if (
            initial_battery_kwh is not None
            and replacement_price_per_kwh is not None
            and abs(replacement_price_per_kwh) > 1e-9
        ):
            terminal_premium = max(0.0, replacement_price_per_kwh - imp_price_obj)
            terminal_soc_value += (
                slot.batteries_discharged_kwh - slot.batteries_charged_kwh
            ) * terminal_premium

    # ``total_cost`` is money only — never includes synthetic penalties.
    total_cost = import_cost - export_revenue + conversion_loss_cost + cycle_cost_total

    # ``score`` is the selector objective.  It uses discounted values when
    # time_discount_rate < 1.0 so that uncertain distant savings are weighted
    # less than near-term certain savings.  ``total_cost`` is always raw
    # (undiscounted) so it remains auditable as real money.
    if use_discount:
        score = (
            import_cost_disc
            - export_revenue_disc
            + conversion_loss_cost_disc
            + cycle_cost_total_disc
            + soc_penalty_disc
            + grid_limit_penalty_disc
            + override_penalty
            + terminal_soc_value
        )
    else:
        score = (
            total_cost
            + soc_penalty
            + grid_limit_penalty
            + override_penalty
            + terminal_soc_value
        )

    score_rounded = round(score, 6)

    result = PlanCostBreakdown(
        import_cost=round(import_cost, 6),
        export_revenue=round(export_revenue, 6),
        conversion_loss_cost=round(conversion_loss_cost, 6),
        cycle_cost=round(cycle_cost_total, 6),
        soc_penalty=round(soc_penalty, 6),
        grid_limit_penalty=round(grid_limit_penalty, 6),
        override_penalty=round(override_penalty, 6),
        terminal_soc_value=round(terminal_soc_value, 6),
        total_cost=round(total_cost, 6),
        score=score_rounded,
        # ``total`` is a deprecated alias for ``score`` (issue #413).
        total=score_rounded,
    )

    log_planner(
        "debug",
        "[cost] score_plan DONE  total_cost=%.6f  score=%.6f  "
        "import=%.6f  export_rev=%.6f  conv_loss=%.6f  "
        "cycle=%.6f  soc_pen=%.6f  grid=%.6f  override=%.6f  term_soc=%.6f",
        result.total_cost,
        result.score,
        result.import_cost,
        result.export_revenue,
        result.conversion_loss_cost,
        result.cycle_cost,
        result.soc_penalty,
        result.grid_limit_penalty,
        result.override_penalty,
        result.terminal_soc_value,
    )

    return result


def compare_plans(
    plan_a: Sequence[PlannedSlot],
    plan_b: Sequence[PlannedSlot],
    weights: CostWeights | None = None,
    *,
    slot_duration_hours: float = 1.0,
    now: datetime | None = None,
    initial_battery_kwh: float | None = None,
    replacement_price_per_kwh: float | None = None,
) -> tuple[PlanCostBreakdown, PlanCostBreakdown, str]:
    """Score two candidate plans and return which one wins.

    The winner is the plan with the lower :attr:`PlanCostBreakdown.score`
    (selector objective).  When the scores tie within ``1e-9``, the winner
    is ``"tie"``.

    Args:
        plan_a: First candidate plan (list of slots).
        plan_b: Second candidate plan (list of slots).
        weights: Shared cost weights applied to both plans.
        slot_duration_hours: Duration of each slot in hours.
        now: Forwarded to :func:`score_plan`.
        initial_battery_kwh: Forwarded to :func:`score_plan` to enable
            terminal-SoC accounting.
        replacement_price_per_kwh: Forwarded to :func:`score_plan` to enable
            terminal-SoC accounting.

    Returns:
        A three-tuple ``(breakdown_a, breakdown_b, winner)`` where
        ``winner`` is either ``"plan_a"`` or ``"plan_b"`` (the plan with
        the lower selector score).  ``"tie"`` when both plans are
        equivalent within floating-point tolerance.

    Examples:
        >>> bd_a, bd_b, winner = compare_plans(cheap_slots, expensive_slots)
        >>> winner
        'plan_a'
    """
    bd_a = score_plan(
        plan_a,
        weights,
        slot_duration_hours=slot_duration_hours,
        now=now,
        initial_battery_kwh=initial_battery_kwh,
        replacement_price_per_kwh=replacement_price_per_kwh,
    )
    bd_b = score_plan(
        plan_b,
        weights,
        slot_duration_hours=slot_duration_hours,
        now=now,
        initial_battery_kwh=initial_battery_kwh,
        replacement_price_per_kwh=replacement_price_per_kwh,
    )

    diff = bd_a.score - bd_b.score
    if abs(diff) < 1e-9:
        winner = "tie"
    elif diff < 0:
        winner = "plan_a"
    else:
        winner = "plan_b"

    log_planner(
        "debug",
        "[cost] compare_plans  a_score=%.6f  b_score=%.6f  diff=%.6f  winner=%s",
        bd_a.score,
        bd_b.score,
        diff,
        winner,
    )

    return bd_a, bd_b, winner

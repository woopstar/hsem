"""Plan cost function for the HSEM planner (issues #295, #413).

This module scores a candidate plan (a fully-populated list of
:class:`~custom_components.hsem.models.planner_outputs.PlannedSlot` objects)
and exposes two distinct aggregate numbers:

- :attr:`PlanCostBreakdown.total_cost` — the **real-money outcome** of the
  plan within the horizon. Sum of grid import cost minus export revenue plus
  battery cycle (depreciation) cost. The retained conversion-loss field is
  zero because physical grid flows already include efficiency.
- :attr:`PlanCostBreakdown.score` — the **selector objective**.  Equals
  ``total_cost`` plus the SoC guard and grid-limit penalties and the
  terminal-SoC opportunity cost. The candidate selector picks the plan with
  the **lowest score**, not the lowest money cost.

Cost components
---------------
The cost function aggregates seven independently-tunable terms:

Money terms (sum to ``total_cost``):

1. **Import cost** — energy imported from the grid × the finite signed
   import price.  A negative spot price keeps its sign, so ``import_cost``
   becomes negative when the site is paid to consume — mirroring
   ``milp_optimizer.py``'s ``p_imp_obj``, which no longer clamps at zero
   (issue #655).  Non-finite prices are treated as ``0.0``.
2. **Export revenue** — energy exported to the grid × export price
   (negative contribution, i.e. revenue reduces total cost).
3. **Battery conversion-loss compatibility field** — always zero. Physical
   charge/discharge efficiency is already present in grid import/export.
4. **Battery cycle cost** — depreciation per kWh cycled, derived from the
   battery's purchase price, rated capacity, and expected lifetime cycles.

Selector-only terms (added on top of ``total_cost`` to produce ``score``):

5. **SoC penalties** — quadratic penalty when the end-of-slot SoC is too low
   (below the configured ``min_soc_pct`` guard) or too high (above the
   configured ``max_soc_pct`` guard), multiplied by a configurable weight.
6. **Grid limit penalty** — penalty when grid import or export in any slot
   exceeds the configured grid power limit, proportional to the excess energy.
7. **Terminal SoC value** — per-slot opportunity cost of charging/discharging,
   capped by the differential between ``replacement_price_per_kwh`` and that
   slot's own finite signed import price:
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
    _resolve_cycle_cost,
    compute_charge_premium,
    deferred_export_price_by_slot,
)
from custom_components.hsem.planner.cost_types import (  # noqa: F401
    CostWeights,
    PlanCostBreakdown,
)
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import clamp_efficiency
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.units import hours_ahead

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

    - ``total_cost`` — money outcome only. Equals
      ``import_cost − export_revenue + cycle_cost + conversion_loss_cost``,
      where the compatibility conversion-loss field is zero.
    - ``score`` — selector objective. Equals ``total_cost`` plus the SoC guard,
      grid-limit penalty, and terminal-SoC opportunity cost.  The candidate selector minimises
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

    # Deferred-export correction (issue #592): mirror the MILP's objective
    # so the selector's terminal-SoC charge credit matches what the LP
    # actually optimised for.  Computed once for the whole slot list.
    _deferred_prices: list[float | None] | None = None
    if (
        replacement_price_per_kwh is not None
        and abs(replacement_price_per_kwh) > 1e-9
        and weights.battery_usable_capacity_kwh > 1e-9
        and weights.max_charge_per_slot_kwh > 1e-9
    ):
        _deferred_prices = deferred_export_price_by_slot(
            slots,
            usable_kwh=weights.battery_usable_capacity_kwh,
            max_charge_per_slot=weights.max_charge_per_slot_kwh,
            now=now,
        )

    # Charge efficiency remains necessary for the terminal-inventory charge
    # premium. Physical conversion loss itself is already represented by the
    # grid-flow fields and must not receive a second monetary charge.
    charge_eff = clamp_efficiency(weights.charge_efficiency_pct)

    import_cost = 0.0
    export_revenue = 0.0
    conversion_loss_cost = 0.0
    cycle_cost_total = 0.0
    soc_penalty = 0.0
    grid_limit_penalty = 0.0
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

    for slot_idx, slot in enumerate(slots):
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
            hours_ahead_val = hours_ahead(now, slot_mid)
            discount = discount_rate**hours_ahead_val
        else:
            discount = 1.0

        imp_price = slot.price.import_price
        exp_price = slot.price.export_price

        # Non-finite prices carry no economic authority.
        if not math.isfinite(imp_price):
            imp_price = 0.0
        if not math.isfinite(exp_price):
            exp_price = 0.0

        # Signed import price — mirrors milp_optimizer.py's p_imp_obj, which
        # no longer clamps at zero.  A finite negative spot price is a real
        # economic signal: the site is paid to consume, so import_cost must be
        # allowed to go negative.  Grid flows are bounded and made
        # direction-exclusive by grid_flow_mode in the MILP, so preserving the
        # sign no longer admits an unbounded import/export wash flow.  The
        # selector must value the identical physical decisions the same way the
        # LP did, or its score stops matching the solved objective (issue #655).
        imp_price_obj = imp_price

        # 1. Import cost — grid_import_kwh already reflects the extra grid draw
        #    needed to store energy through the charge efficiency (i.e. the
        #    simulation writes grid_import_kwh = charge_stored / charge_eff).
        if slot.grid_import_kwh > 1e-9:
            cost = slot.grid_import_kwh * imp_price_obj
            import_cost += cost
            import_cost_disc += cost * discount

        # 2. Export revenue — PV export is always counted at the live
        #    export price.  Battery-destined export revenue is zeroed when
        #    the slot is below the user's ``battery_export_min_price`` floor
        #    (issue #752): the MILP caps ``ed[t]`` there, so that export can
        #    never be realised by the battery.  The legacy
        #    ``export_min_price`` blanket clamp has been removed because the
        #    applier no longer physically blocks all grid export (issue
        #    #767); it is now a battery-export floor enforced by the MILP.
        if slot.grid_export_kwh > 1e-9:
            effective_exp_price = exp_price
            if (
                weights.battery_export_min_price > 1e-9
                and effective_exp_price < weights.battery_export_min_price
                and slot.batteries_discharged_kwh > 1e-9
                and slot.solcast_pv_estimate_kwh <= 1e-9
            ):
                effective_exp_price = 0.0
            rev = slot.grid_export_kwh * effective_exp_price
            export_revenue += rev
            export_revenue_disc += rev * discount

        # 3. Conversion losses are already physical in the published grid
        # flows: charge import includes stored/charge_eff, while discharge
        # reduces import or creates export only after discharge_eff. Import
        # cost and export revenue therefore price the loss exactly once. Keep
        # the public compatibility aggregate at zero.

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

        # 7. Terminal-SoC opportunity cost (selector-only).
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
        # SECOND CAP (issue #694): the terminal premium must never make
        # battery charging more attractive than grid export.  Mirrors the
        # identical cap in milp_optimizer.py's _build_objective().
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
            # Cap the CHARGE credit only: the terminal premium for
            # charging is reduced by the opportunity cost of not
            # exporting the same PV surplus (issue #694), and corrected by
            # the deferred-export spread when a future slot's PV surplus
            # exceeds the battery's absorption capacity (issue #592).
            # Mirrors milp/_objective.py exactly.  The discharge penalty
            # is NOT capped.
            _charge_premium = compute_charge_premium(
                replacement_price_per_kwh=replacement_price_per_kwh,
                imp_price_obj=imp_price_obj,
                exp_price=slot.price.export_price,
                charge_eff=charge_eff,
                deferred_export_price=(
                    _deferred_prices[slot_idx] if _deferred_prices else None
                ),
            )
            # Charge earns the capped credit; discharge incurs the full penalty
            terminal_soc_value += (
                -slot.batteries_charged_kwh * _charge_premium
                + slot.batteries_discharged_kwh * terminal_premium
            )

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
            + terminal_soc_value
        )
    else:
        score = total_cost + soc_penalty + grid_limit_penalty + terminal_soc_value

    score_rounded = round(score, 6)

    result = PlanCostBreakdown(
        import_cost=round(import_cost, 6),
        export_revenue=round(export_revenue, 6),
        conversion_loss_cost=round(conversion_loss_cost, 6),
        cycle_cost=round(cycle_cost_total, 6),
        soc_penalty=round(soc_penalty, 6),
        grid_limit_penalty=round(grid_limit_penalty, 6),
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
        "cycle=%.6f  soc_pen=%.6f  grid=%.6f  term_soc=%.6f",
        result.total_cost,
        result.score,
        result.import_cost,
        result.export_revenue,
        result.conversion_loss_cost,
        result.cycle_cost,
        result.soc_penalty,
        result.grid_limit_penalty,
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

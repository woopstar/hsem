"""Plan cost function for the HSEM planner (issue #295).

This module scores a candidate plan (a fully-populated list of
:class:`~custom_components.hsem.models.planner_outputs.PlannedSlot` objects)
as a single numeric value.  **Lower cost is better**, so the planner can
choose between alternatives by picking the plan with the minimum score.

Cost components
---------------
The cost function aggregates seven independently-tunable penalty terms:

1. **Import cost** — energy imported from the grid × import price.
2. **Export revenue** — energy exported to the grid × export price
   (negative contribution, i.e. revenue reduces total cost).
3. **Battery conversion loss** — energy lost during a charge/discharge cycle,
   priced at the *average* of the import and export prices in each slot as a
   proxy for its opportunity cost.
4. **Battery cycle cost** — depreciation per kWh cycled, derived from the
   battery's purchase price, rated capacity, and expected lifetime cycles.
5. **SoC penalties** — quadratic penalty when the end-of-slot SoC is too low
   (below the configured ``min_soc_pct`` guard) or too high (above the
   configured ``max_soc_pct`` guard), multiplied by a configurable weight.
6. **Grid limit penalty** — penalty when grid import or export in any slot
   exceeds the configured grid power limit, proportional to the excess energy.
7. **Override penalty** — per-slot cost added for any slot whose recommendation
   was forced by an override (e.g. read-only mode, manual schedule).  Penalises
   plans that deviate from the hardware's natural optimal state.

All monetary values are in the caller's local currency (e.g. DKK or EUR).

Design constraints
------------------
- **Pure Python, no Home Assistant imports** — testable with plain pytest.
- **Additive, independently-disableable terms** — any weight set to 0 disables
  that penalty without touching the others.
- **Float-safe** — NaN prices are treated as 0.0 rather than propagating.
- **Immutable input** — slots are *never* mutated; the function is a pure
  read-only scan.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Public configuration dataclass
# ---------------------------------------------------------------------------


@dataclass
class CostWeights:
    """Weights and limits used by :func:`score_plan`.

    All monetary weights are dimensionless multipliers applied to the
    corresponding cost term before summation.  Setting any weight to
    ``0.0`` completely disables that term.

    Attributes:
        soc_low_penalty_weight:
            Penalty multiplier for each percentage-point by which the
            end-of-slot SoC falls *below* ``min_soc_pct``.  Applied as a
            quadratic term: ``weight × (violation_pct²)``.
        soc_high_penalty_weight:
            Penalty multiplier for each percentage-point by which the
            end-of-slot SoC exceeds ``max_soc_pct``.  Applied as a
            quadratic term: ``weight × (violation_pct²)``.
        min_soc_pct:
            SoC floor (0-100) below which the SoC-low penalty kicks in.
            Typically equals ``battery_end_of_discharge_soc_pct``.
        max_soc_pct:
            SoC ceiling (0-100) above which the SoC-high penalty kicks in.
            Typically equals ``battery_max_soc_pct``.
        grid_limit_kw:
            Maximum allowed grid import *or* export power per slot in kW.
            Violations are penalised by ``grid_limit_penalty_per_kwh``
            for every kWh of excess energy.  ``None`` disables the check.
        grid_limit_penalty_per_kwh:
            Currency/kWh applied to each kWh that exceeds ``grid_limit_kw``.
        override_penalty_per_slot:
            Flat cost added for every slot flagged as a forced override.
            Penalises plans that bypass normal optimisation.
        cycle_cost_per_kwh:
            Depreciation cost in local currency per kWh cycled through the
            battery.  When ``None`` it is auto-calculated from
            ``battery_purchase_price``, ``battery_rated_capacity_kwh``, and
            ``battery_expected_cycles`` if those values are positive; otherwise
            the term is disabled.
        battery_purchase_price:
            Battery purchase price (local currency).  Used only when
            ``cycle_cost_per_kwh`` is ``None``.
        battery_rated_capacity_kwh:
            Nameplate battery capacity (kWh).  Used only when
            ``cycle_cost_per_kwh`` is ``None``.
        battery_expected_cycles:
            Expected total lifetime charge/discharge cycles.  Used only when
            ``cycle_cost_per_kwh`` is ``None``.
        charge_efficiency_pct:
            Charge-side efficiency as a percentage (0-100).  Energy stored in
            the battery equals input energy × (charge_efficiency_pct / 100).
            Used in the grid-import cost term: grid import for charging equals
            ``batteries_charged / (charge_efficiency_pct / 100)``.
            Defaults to 100 % (no charge-side loss) so existing callers are
            unaffected unless they explicitly pass this value.
        conversion_loss_pct:
            Round-trip conversion loss as a percentage (0-100).  Legacy term
            used to compute the ``conversion_loss_cost`` penalty.  When
            ``charge_efficiency_pct`` and ``discharge_efficiency_pct`` are set,
            the roundtrip loss implied by those values supersedes this field for
            the ``conversion_loss_cost`` calculation.
        discharge_efficiency_pct:
            Discharge-side efficiency as a percentage (0-100).  Energy delivered
            to the house equals battery energy removed × (discharge_efficiency_pct / 100).
            Defaults to 100 % (no discharge-side loss) for backward compatibility.
    """

    # SoC guard penalties
    soc_low_penalty_weight: float = 0.01
    soc_high_penalty_weight: float = 0.001
    min_soc_pct: float = 10.0
    max_soc_pct: float = 100.0

    # Grid limit
    grid_limit_kw: float | None = None
    grid_limit_penalty_per_kwh: float = 0.5

    # Override penalty
    override_penalty_per_slot: float = 0.0

    # Battery cycle depreciation
    cycle_cost_per_kwh: float | None = None
    battery_purchase_price: float = 0.0
    battery_rated_capacity_kwh: float = 10.0
    battery_expected_cycles: int = 6000

    # Separate charge / discharge efficiencies
    charge_efficiency_pct: float = 100.0
    discharge_efficiency_pct: float = 100.0

    # Conversion loss (legacy round-trip term)
    conversion_loss_pct: float = 10.0


@dataclass
class PlanCostBreakdown:
    """Per-term breakdown of the cost computed by :func:`score_plan`.

    Useful for debugging, logging, and surfacing in the plan explanation.

    Attributes:
        import_cost:
            Total cost of grid imports across all slots (≥ 0).
        export_revenue:
            Total revenue from grid exports across all slots (≥ 0).
            This is a *positive* value representing earned money; it is
            *subtracted* from the total cost in :attr:`total`.
        conversion_loss_cost:
            Opportunity cost of energy lost in round-trip battery cycles.
        cycle_cost:
            Battery depreciation cost (kWh cycled × cost per kWh).
        soc_penalty:
            Quadratic SoC guard penalty (too-low + too-high violations).
        grid_limit_penalty:
            Penalty for exceeding the configured grid power limit.
        override_penalty:
            Penalty for forced-override slots.
        total:
            Sum of all terms: ``import_cost − export_revenue
            + conversion_loss_cost + cycle_cost + soc_penalty
            + grid_limit_penalty + override_penalty``.
    """

    import_cost: float = 0.0
    export_revenue: float = 0.0
    conversion_loss_cost: float = 0.0
    cycle_cost: float = 0.0
    soc_penalty: float = 0.0
    grid_limit_penalty: float = 0.0
    override_penalty: float = 0.0
    total: float = 0.0


# ---------------------------------------------------------------------------
# Override detection helpers
# ---------------------------------------------------------------------------

#: Recommendation values that represent schedule-forced modes rather than
#: the optimiser's free choice.  Used to detect override slots.
_OVERRIDE_RECOMMENDATIONS: frozenset[str] = frozenset(
    {
        "batteries_charge_grid",  # schedule-driven grid charge
    }
)


def _is_override_slot(slot: PlannedSlot) -> bool:
    """Return ``True`` if *slot* was set by a forced override.

    Currently an override is defined as a slot whose recommendation is
    ``"batteries_charge_grid"`` (a schedule-driven hard constraint).  Extend
    this set as HSEM gains more override modes.

    Args:
        slot: The slot to inspect.

    Returns:
        ``True`` when the slot represents a forced override.
    """
    return bool(
        slot.recommendation and slot.recommendation in _OVERRIDE_RECOMMENDATIONS
    )


# ---------------------------------------------------------------------------
# Cycle cost helper
# ---------------------------------------------------------------------------


def _resolve_cycle_cost(weights: CostWeights) -> float:
    """Return the battery cycle depreciation cost per kWh cycled.

    If ``weights.cycle_cost_per_kwh`` is explicitly set (not ``None``), that
    value is used directly.  Otherwise the cost is computed from the battery
    economics in *weights*:

        cycle_cost = purchase_price / (rated_capacity_kwh × expected_cycles)

    Returns 0.0 when any required value is non-positive or missing.

    Args:
        weights: Configuration object from which to resolve the cost.

    Returns:
        Depreciation cost in local currency per kWh.
    """
    if weights.cycle_cost_per_kwh is not None:
        return weights.cycle_cost_per_kwh

    if (
        weights.battery_purchase_price > 1e-9
        and weights.battery_rated_capacity_kwh > 1e-9
        and weights.battery_expected_cycles > 0
    ):
        return weights.battery_purchase_price / (
            weights.battery_rated_capacity_kwh * weights.battery_expected_cycles
        )

    return 0.0


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
    the sentinel written by the SoC simulator on completed slots.  Either
    way, including past slots in the SoC-guard penalty would generate a
    false ``soc_low_penalty`` because the simulator zeros
    ``estimated_battery_soc`` on past slots as a sentinel value.

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

    Returns:
        A :class:`PlanCostBreakdown` containing every cost component and
        the total score.  **Lower total = better plan.**

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
        >>> bd.total
        0.2
    """
    if weights is None:
        weights = CostWeights()

    # Resolve grid limit (keyword arg takes precedence)
    effective_grid_limit_kw: float | None = (
        grid_limit_kw if grid_limit_kw is not None else weights.grid_limit_kw
    )

    cycle_cost_kwh = _resolve_cycle_cost(weights)

    # Resolve the effective roundtrip loss fraction.
    # When separate charge/discharge efficiencies are provided (both non-default),
    # we compute the roundtrip loss from them:
    #   roundtrip_loss = 1 - (charge_eff × discharge_eff)
    # Otherwise fall back to the legacy conversion_loss_pct field.
    charge_eff = max(min(weights.charge_efficiency_pct, 100.0), 1.0) / 100.0
    discharge_eff = max(min(weights.discharge_efficiency_pct, 100.0), 1.0) / 100.0
    if (
        weights.charge_efficiency_pct < 100.0 - 1e-9
        or weights.discharge_efficiency_pct < 100.0 - 1e-9
    ):
        # At least one efficiency is below 100 % — use the product-based roundtrip loss.
        loss_fraction = 1.0 - charge_eff * discharge_eff
    else:
        loss_fraction = weights.conversion_loss_pct / 100.0

    import_cost = 0.0
    export_revenue = 0.0
    conversion_loss_cost = 0.0
    cycle_cost_total = 0.0
    soc_penalty = 0.0
    grid_limit_penalty = 0.0
    override_penalty = 0.0

    _time_passed_value = Recommendations.TimePassed.value

    for slot in slots:
        # Skip past slots entirely.  The SoC simulation zeros
        # estimated_battery_soc on past slots as a sentinel, which would
        # falsely trigger the SoC-low penalty on every past slot.  All other
        # energy-flow fields are also zeroed, so skipping past slots has no
        # effect on import cost, cycle cost, or any other term.
        #
        # Primary guard: slot.end <= now (time-based, no string coupling).
        # Fallback guard: recommendation == TimePassed (used when now is None,
        # e.g. in unit tests that call score_plan without a clock).
        if now is not None:
            if slot.end <= now:
                continue
        elif slot.recommendation == _time_passed_value:
            continue

        imp_price = slot.price.import_price
        exp_price = slot.price.export_price

        # Treat NaN prices as zero to avoid propagation
        if math.isnan(imp_price):
            imp_price = 0.0
        if math.isnan(exp_price):
            exp_price = 0.0

        # 1. Import cost — grid_import_kwh already reflects the extra grid draw
        #    needed to store energy through the charge efficiency (i.e. the
        #    simulation writes grid_import_kwh = charge_stored / charge_eff).
        if slot.grid_import_kwh > 1e-9:
            import_cost += slot.grid_import_kwh * imp_price

        # 2. Export revenue
        if slot.grid_export_kwh > 1e-9:
            export_revenue += slot.grid_export_kwh * exp_price

        # 3. Conversion loss cost — opportunity cost of energy burned in the
        #    round-trip (charge loss + discharge loss), priced at mid-market
        #    (average of import and export) as a neutral proxy.
        cycled_kwh = slot.batteries_charged + slot.batteries_discharged
        if cycled_kwh > 1e-9 and loss_fraction > 1e-9:
            lost_kwh = cycled_kwh * loss_fraction
            mid_price = (imp_price + exp_price) / 2.0
            conversion_loss_cost += lost_kwh * mid_price

        # 4. Battery cycle depreciation
        if cycled_kwh > 1e-9 and cycle_cost_kwh > 1e-9:
            cycle_cost_total += cycled_kwh * cycle_cost_kwh

        # 5. SoC guard penalties (quadratic in the violation magnitude).
        #    Only applied to future/current slots where the SoC reflects a
        #    real simulation value, not the zeroed-out sentinel for past slots.
        soc = slot.estimated_battery_soc
        if soc < weights.min_soc_pct:
            violation = weights.min_soc_pct - soc
            soc_penalty += weights.soc_low_penalty_weight * violation**2
        elif soc > weights.max_soc_pct:
            violation = soc - weights.max_soc_pct
            soc_penalty += weights.soc_high_penalty_weight * violation**2

        # 6. Grid limit penalty
        if effective_grid_limit_kw is not None and slot_duration_hours > 1e-9:
            import_kw = slot.grid_import_kwh / slot_duration_hours
            export_kw = slot.grid_export_kwh / slot_duration_hours
            for kw in (import_kw, export_kw):
                excess_kw = kw - effective_grid_limit_kw
                if excess_kw > 1e-9:
                    grid_limit_penalty += (
                        excess_kw
                        * slot_duration_hours
                        * weights.grid_limit_penalty_per_kwh
                    )

        # 7. Override penalty
        if _is_override_slot(slot) and abs(weights.override_penalty_per_slot) > 1e-9:
            override_penalty += weights.override_penalty_per_slot

    total = (
        import_cost
        - export_revenue
        + conversion_loss_cost
        + cycle_cost_total
        + soc_penalty
        + grid_limit_penalty
        + override_penalty
    )

    return PlanCostBreakdown(
        import_cost=round(import_cost, 6),
        export_revenue=round(export_revenue, 6),
        conversion_loss_cost=round(conversion_loss_cost, 6),
        cycle_cost=round(cycle_cost_total, 6),
        soc_penalty=round(soc_penalty, 6),
        grid_limit_penalty=round(grid_limit_penalty, 6),
        override_penalty=round(override_penalty, 6),
        total=round(total, 6),
    )


def compare_plans(
    plan_a: Sequence[PlannedSlot],
    plan_b: Sequence[PlannedSlot],
    weights: CostWeights | None = None,
    *,
    slot_duration_hours: float = 1.0,
) -> tuple[PlanCostBreakdown, PlanCostBreakdown, str]:
    """Score two candidate plans and return which one wins.

    Args:
        plan_a: First candidate plan (list of slots).
        plan_b: Second candidate plan (list of slots).
        weights: Shared cost weights applied to both plans.
        slot_duration_hours: Duration of each slot in hours.

    Returns:
        A three-tuple ``(breakdown_a, breakdown_b, winner)`` where
        ``winner`` is either ``"plan_a"`` or ``"plan_b"`` (the plan
        with the lower total cost).  When both plans have identical scores
        within floating-point tolerance (``< 1e-9``), ``winner`` is
        ``"tie"``.

    Examples:
        >>> bd_a, bd_b, winner = compare_plans(cheap_slots, expensive_slots)
        >>> winner
        'plan_a'
    """
    bd_a = score_plan(plan_a, weights, slot_duration_hours=slot_duration_hours)
    bd_b = score_plan(plan_b, weights, slot_duration_hours=slot_duration_hours)

    diff = bd_a.total - bd_b.total
    if abs(diff) < 1e-9:
        winner = "tie"
    elif diff < 0:
        winner = "plan_a"
    else:
        winner = "plan_b"

    return bd_a, bd_b, winner

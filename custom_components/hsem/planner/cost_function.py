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

1. **Import cost** — energy imported from the grid × import price.
2. **Export revenue** — energy exported to the grid × export price
   (negative contribution, i.e. revenue reduces total cost).
3. **Battery conversion loss** — energy lost during a charge/discharge cycle,
   priced at the *average* of the import and export prices in each slot as a
   proxy for its opportunity cost.
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
8. **Terminal SoC value** — opportunity cost of the change in battery energy
   over the horizon, priced at ``replacement_price_per_kwh``.  A plan that
   ends the horizon with *more* stored energy than it started with receives a
   *credit* (negative term that reduces ``score``); a plan that empties the
   battery pays a *penalty* (positive term that increases ``score``).
   Computed as ``(initial_kwh − final_kwh) × replacement_price_per_kwh``.

All monetary values are in the caller's local currency (e.g. DKK or EUR).

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
from dataclasses import dataclass
from datetime import datetime

from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import clamp_efficiency
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
        discharge_efficiency_pct:
            Discharge-side efficiency as a percentage (0-100).  Energy delivered
            to the house equals battery energy removed × (discharge_efficiency_pct / 100).
            Defaults to 100 % (no discharge-side loss) for backward compatibility.
        time_discount_rate:
            Per-hour exponential discount factor applied to the ``score``
            (selector objective) but **not** to ``total_cost`` (auditable
            (auditable money).  A value of ``1.0`` disables the discount
            entirely.  Default ``0.995`` gives:

            | Horizon | Factor |
            |---|---|
            | 1 hour | 0.995 |
            | 6 hours | 0.970 |
            | 24 hours | 0.887 |
            | 48 hours | 0.787 |

            This prevents the selector from preferring plans that look cheap
            only because they rely on uncertain distant-future slots.
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

    # Time discount for selector score (1.0 = no discount)
    time_discount_rate: float = 0.995


@dataclass
class PlanCostBreakdown:
    """Per-term breakdown of the cost computed by :func:`score_plan`.

    Two aggregate numbers are exposed:

    - :attr:`total_cost` — sum of money terms only.  Auditable; comparable
      to a real electricity bill.  Computed as
      ``import_cost − export_revenue + cycle_cost + conversion_loss_cost``.
    - :attr:`score` — selector objective.  Equals ``total_cost`` plus all
      synthetic penalties and the terminal-SoC opportunity cost.  The
      candidate selector minimises this value.

    Attributes:
        import_cost:
            Total cost of grid imports across all slots (≥ 0).
        export_revenue:
            Total revenue from grid exports across all slots.
            Positive when export prices are positive (money earned);
            negative when export prices are negative (curtailment penalty,
            exporting costs money).  This value is *subtracted* from
            :attr:`total_cost`, so a negative value increases total cost.
        conversion_loss_cost:
            Opportunity cost of energy lost in round-trip battery cycles.
        cycle_cost:
            Battery depreciation cost (kWh cycled × cost per kWh).
        soc_penalty:
            Quadratic SoC guard penalty (too-low + too-high violations).
            Selector-only — does not enter :attr:`total_cost`.
        grid_limit_penalty:
            Penalty for exceeding the configured grid power limit.
            Selector-only — does not enter :attr:`total_cost`.
        override_penalty:
            Penalty for forced-override slots.  Selector-only.
        terminal_soc_value:
            Opportunity cost of the change in stored battery energy across
            the horizon, priced at ``replacement_price_per_kwh``.  Negative
            (credit) when the plan ends with *more* stored energy than it
            started with; positive (penalty) when the plan empties the
            battery.  Selector-only — does not enter :attr:`total_cost`.
        total_cost:
            Money outcome of the plan in the horizon.  Equal to
            ``import_cost − export_revenue + cycle_cost + conversion_loss_cost``.
            Auditable; does **not** include any synthetic penalties.
        score:
            Selector objective.  Equal to
            ``total_cost + soc_penalty + grid_limit_penalty
            + override_penalty + terminal_soc_value``.
            **Lower is better.**  The candidate selector picks the plan
            with the lowest score.
        total:
            Deprecated alias for :attr:`score`, preserved so older code
            and tests that compared plans by ``.total`` keep selecting the
            same winner.  New code should use :attr:`total_cost` or
            :attr:`score` explicitly.
    """

    import_cost: float = 0.0
    export_revenue: float = 0.0
    conversion_loss_cost: float = 0.0
    cycle_cost: float = 0.0
    soc_penalty: float = 0.0
    grid_limit_penalty: float = 0.0
    override_penalty: float = 0.0
    terminal_soc_value: float = 0.0
    total_cost: float = 0.0
    score: float = 0.0
    # Deprecated alias for ``score``; kept for backward compatibility.
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

    Uses usable capacity (rated × DoD fraction) in the denominator, not
    rated capacity, because battery degradation is driven by cycling within
    the usable SoC range.

    The ``2×`` factor in the denominator accounts for the fact that one full
    battery cycle involves energy flow in *both* directions::

        throughput_per_cycle = 2 × usable_kwh
                              (charge once + discharge once)

    Since ``purchase_price / expected_cycles`` is the cost *per full cycle*
    and the cycle cost is expressed *per kWh of throughput*, the cost must
    be spread over the total lifetime throughput:

        cycle_cost_per_kwh = purchase_price / expected_cycles / (2 × usable_kwh)

    This is mathematically equivalent to:

        purchase_price / (2 × usable_kwh × expected_cycles)

    If ``weights.cycle_cost_per_kwh`` is explicitly set (not ``None``), that
    value is used directly.  Returns 0.0 when any required value is non-positive
    or missing.

    Args:
        weights: Configuration object from which to resolve the cost.

    Returns:
        Depreciation cost in local currency per kWh.
    """
    if weights.cycle_cost_per_kwh is not None:
        result = weights.cycle_cost_per_kwh
        log_planner(
            "debug",
            "[cost] _resolve_cycle_cost  explicit=%.6f",
            result,
        )
        return result

    if (
        weights.battery_purchase_price > 1e-9
        and weights.battery_rated_capacity_kwh > 1e-9
        and weights.battery_expected_cycles > 0
    ):
        dod_fraction = (weights.max_soc_pct - weights.min_soc_pct) / 100.0
        usable_kwh = weights.battery_rated_capacity_kwh * dod_fraction
        if usable_kwh < 1e-9:
            usable_kwh = weights.battery_rated_capacity_kwh  # fallback: full rated
        result = weights.battery_purchase_price / (
            2 * usable_kwh * weights.battery_expected_cycles
        )
        log_planner(
            "debug",
            "[cost] _resolve_cycle_cost  purchase=%.2f  usable=%.3f  cycles=%d  "
            "cycle_cost=%.6f",
            weights.battery_purchase_price,
            usable_kwh,
            weights.battery_expected_cycles,
            result,
        )
        return result

    log_planner("debug", "[cost] _resolve_cycle_cost  return 0 (insufficient data)")
    return 0.0


# ---------------------------------------------------------------------------
# Terminal-SoC helper
# ---------------------------------------------------------------------------


def _final_battery_kwh(
    slots: Sequence[PlannedSlot],
    now: datetime | None,
) -> float:
    """Return the estimated stored battery energy (kWh) at end of horizon.

    Scans *slots* in reverse and returns the ``estimated_battery_capacity``
    value of the last slot that is still in the future.

    The SoC simulator writes ``estimated_battery_capacity`` as the remaining
    usable energy above the discharge floor at the *end* of each slot.  Past
    slots have this field zeroed as a sentinel, so we must explicitly skip
    them when picking the horizon's terminal SoC.

    Args:
        slots: Ordered list of planned slots.
        now: Timezone-aware current datetime.  When provided, slots with
            ``slot.end <= now`` are skipped.  When ``None``, slots whose
            recommendation equals the ``TimePassed`` sentinel are skipped.

    Returns:
        Remaining battery energy above the discharge floor (kWh) at the end
        of the last future slot, or ``0.0`` when no future slot exists.
    """
    _time_passed_value = Recommendations.TimePassed.value
    for slot in reversed(slots):
        if now is not None:
            if slot.end <= now:
                continue
        elif slot.recommendation == _time_passed_value:
            continue
        result = slot.estimated_battery_capacity_kwh
        log_planner(
            "debug",
            "[cost] _final_battery_kwh  result=%.3f  slot=%s",
            result,
            slot.start.isoformat(),
        )
        return result
    log_planner("debug", "[cost] _final_battery_kwh  return 0.0 (no future slot)")
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
    the sentinel written by the SoC simulator on completed slots.  Either
    way, including past slots in the SoC-guard penalty would generate a
    false ``soc_low_penalty`` because the simulator zeros
    ``estimated_battery_soc`` on past slots as a sentinel value.

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

        # Compute time discount for this slot.
        # discount = discount_rate ^ hours_from_now
        # Past slots are already skipped above, so hours_ahead >= 0.
        if use_discount:
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

        # 1. Import cost — grid_import_kwh already reflects the extra grid draw
        #    needed to store energy through the charge efficiency (i.e. the
        #    simulation writes grid_import_kwh = charge_stored / charge_eff).
        if slot.grid_import_kwh > 1e-9:
            cost = slot.grid_import_kwh * imp_price
            import_cost += cost
            import_cost_disc += cost * discount

        # 2. Export revenue
        if slot.grid_export_kwh > 1e-9:
            rev = slot.grid_export_kwh * exp_price
            export_revenue += rev
            export_revenue_disc += rev * discount

        # 3. Conversion loss cost — opportunity cost of energy lost in the
        #    round-trip.  The loss occurred at purchase time (charge slot) and
        #    at delivery time (discharge slot).  Each side is priced at the
        #    import price of its own slot — the price of the energy that was
        #    lost.
        charge_loss_fraction = 1.0 - charge_eff
        discharge_loss_fraction = 1.0 - discharge_eff
        if slot.batteries_charged_kwh > 1e-9 and charge_loss_fraction > 1e-9:
            lost_kwh_charge = slot.batteries_charged_kwh * charge_loss_fraction
            conv = lost_kwh_charge * imp_price
            conversion_loss_cost += conv
            conversion_loss_cost_disc += conv * discount
        if slot.batteries_discharged_kwh > 1e-9 and discharge_loss_fraction > 1e-9:
            lost_kwh_discharge = slot.batteries_discharged_kwh * discharge_loss_fraction
            conv = lost_kwh_discharge * imp_price
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
    terminal_soc_value = 0.0
    if (
        initial_battery_kwh is not None
        and replacement_price_per_kwh is not None
        and abs(replacement_price_per_kwh) > 1e-9
    ):
        final_battery_kwh = _final_battery_kwh(slots, now)
        delta_kwh = initial_battery_kwh - final_battery_kwh
        terminal_soc_value = delta_kwh * replacement_price_per_kwh

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

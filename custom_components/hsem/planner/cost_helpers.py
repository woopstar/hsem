"""Cost-function helpers for cycle cost and terminal valuation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.cost_types import CostWeights
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import resolve_cycle_cost
from custom_components.hsem.utils.units import usable_kwh_from_rated


def grid_cash_flow_cost(
    grid_import_kwh: float,
    grid_export_kwh: float,
    import_price: float,
    export_price: float,
    *,
    price_actionable: bool = True,
    export_min_price: float = 0.0,
) -> float:
    """Return auditable signed meter cash flow; positive is net cost."""
    if not price_actionable:
        return 0.0
    effective_import = import_price if math.isfinite(import_price) else 0.0
    effective_export = export_price if math.isfinite(export_price) else 0.0
    if export_min_price > 1e-9 and effective_export < export_min_price:
        effective_export = 0.0
    return (
        max(grid_import_kwh, 0.0) * effective_import
        - max(grid_export_kwh, 0.0) * effective_export
    )


def slot_grid_cash_flow_cost(
    slot: PlannedSlot,
    *,
    export_min_price: float = 0.0,
) -> float:
    """Return one slot's signed meter cash flow from final grid fields."""
    return grid_cash_flow_cost(
        slot.grid_import_kwh,
        slot.grid_export_kwh,
        slot.price.import_price,
        slot.price.export_price,
        price_actionable=bool(getattr(slot, "price_actionable", True)),
        export_min_price=export_min_price,
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

    When ``weights.cycle_cost_per_kwh`` is explicitly set (not ``None``), that
    value is used directly — the caller is responsible for resolving auto vs.
    user margin.  When ``None``, the value is auto-calculated from the battery
    economics fields.  Returns 0.0 when any required value is non-positive
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
        usable_kwh = usable_kwh_from_rated(
            weights.battery_rated_capacity_kwh,
            weights.min_soc_pct,
            weights.max_soc_pct,
        )
        if usable_kwh < 1e-9:
            usable_kwh = weights.battery_rated_capacity_kwh
        result = resolve_cycle_cost(
            purchase_price=weights.battery_purchase_price,
            usable_kwh=usable_kwh,
            expected_cycles=weights.battery_expected_cycles,
            capacity_loss_pct=weights.battery_capacity_loss_pct,
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
# Terminal-SoC charge-premium helper (issues #694, #592)
# ---------------------------------------------------------------------------


def compute_charge_premium(
    *,
    replacement_price_per_kwh: float,
    imp_price_obj: float,
    exp_price: float,
    charge_eff: float,
    deferred_export_price: float | None = None,
) -> float:
    """Return the capped terminal-SoC credit for charging in a slot.

    The charge credit must never make battery charging more attractive than
    exporting the same PV surplus — either **now** (issue #694) or **later,
    when the battery can still be refilled from surplus** (issue #592).

    Base cap (issue #694)::

        charge_premium = max(0, repl − p_imp − p_exp / η_chg)

    Deferred-export cap (issue #592): when the battery still has headroom
    and a *future* slot has PV surplus that exceeds what the battery can
    absorb (i.e. surplus that will be exported regardless), the true
    opportunity cost of charging now is not this slot's export price but
    the difference between selling now and selling later.  Charging now at
    a high export price and refilling later at a low export price forfeits
    ``p_exp_now − p_exp_future`` per kWh.  Passing the minimum such future
    export price as *deferred_export_price* tightens the cap::

        charge_premium = max(0, repl − p_imp − p_exp / η_chg
                                      + min(p_exp_future, p_exp) / η_chg)

    When ``p_exp_future >= p_exp`` the correction is zero (no deferral
    benefit) — the formula degrades gracefully to the #694 cap.

    Args:
        replacement_price_per_kwh: Value of one stored kWh at horizon end.
        imp_price_obj: Sanitised (non-negative) import price for the slot.
        exp_price: Export price for the slot (already clamped by the caller).
        charge_eff: Charge efficiency fraction (0–1).
        deferred_export_price: Minimum export price across *future* slots
            whose PV surplus exceeds the battery's remaining charge
            headroom.  ``None`` disables the deferred-export correction.

    Returns:
        The capped charge credit (≥ 0) to subtract from the objective.
    """
    terminal_premium = max(0.0, replacement_price_per_kwh - imp_price_obj)
    if charge_eff <= 1e-9:
        return terminal_premium
    premium = replacement_price_per_kwh - imp_price_obj - exp_price / charge_eff
    if deferred_export_price is not None:
        premium += min(deferred_export_price, exp_price) / charge_eff
    return max(0.0, premium)


def deferred_export_price_by_slot(
    slots: Sequence[PlannedSlot],
    *,
    usable_kwh: float,
    max_charge_per_slot: float,
    now: datetime | None = None,
) -> list[float | None]:
    """Compute the deferred-export price for every slot index.

    For each slot *t*, this is the minimum export price across **later**
    slots that carry PV surplus the battery cannot absorb (because the
    remaining headroom at that point is smaller than the surplus).  Those
    slots will export regardless of today's charge decision, so their
    export price is the economically correct "refill price" for the
    deferred-export cap (issue #592).

    Slots without any qualifying later slot get ``None`` (no deferral
    opportunity — the base #694 cap applies unchanged).

    Args:
        slots: Ordered slot list (ascending start time).
        usable_kwh: Battery usable capacity (kWh) — the maximum headroom.
        max_charge_per_slot: Per-slot charge power limit (kWh/slot).
        now: Optional clock used to skip past slots.

    Returns:
        A list parallel to *slots* with ``float | None`` entries.
    """
    n = len(slots)
    result: list[float | None] = [None] * n
    # PV surplus beyond house load per slot (what could enter the battery).
    surplus = [
        max(
            s.solcast_pv_estimate_kwh - s.avg_house_consumption_kwh,
            0.0,
        )
        for s in slots
    ]
    # Walk backwards tracking the minimum export price among slots whose
    # surplus exceeds the battery's ability to absorb it in that slot.
    # When surplus exceeds what the battery can take, the excess is
    # exported regardless of any charge decision, so that slot's export
    # price is the true refill price for a deferred charge (issue #592).
    absorbable = min(usable_kwh, max_charge_per_slot)
    best: float | None = None
    for i in range(n - 1, -1, -1):
        result[i] = best
        if now is not None and slots[i].end <= now:
            continue
        if surplus[i] > absorbable + 1e-9:
            p = slots[i].price.export_price
            if not math.isnan(p) and (best is None or p < best):
                best = p
    return result

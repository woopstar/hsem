"""Cost-function helpers for cycle cost and terminal valuation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import tzinfo

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.cost_types import CostWeights
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import resolve_cycle_cost
from custom_components.hsem.utils.units import usable_kwh_from_rated


def grid_cash_flow_cost(
    grid_import_kwh: float,
    grid_export_kwh: float,
    import_price: float,
    export_price: float,
    *,
    export_min_price: float = 0.0,
    export_fee_per_kwh: float = 0.0,
) -> float:
    """Return auditable signed meter cash flow; positive is net cost.

    Non-finite rates carry no economic authority and are treated as ``0.0``.
    An export price below *export_min_price* earns nothing, mirroring the
    MILP's battery-origin export block. *export_fee_per_kwh* (retailer
    margin/balancing fees, issue #925) is subtracted from whatever export
    price survives the floor check — never applied to a slot the floor
    already zeroed, or a zeroed slot would report a manufactured negative
    revenue for export that was never counted in the first place.
    """
    effective_import = import_price if math.isfinite(import_price) else 0.0
    effective_export = export_price if math.isfinite(export_price) else 0.0
    if export_min_price > 1e-9 and effective_export < export_min_price:
        effective_export = 0.0
    else:
        effective_export -= export_fee_per_kwh
    return (
        max(grid_import_kwh, 0.0) * effective_import
        - max(grid_export_kwh, 0.0) * effective_export
    )


def slot_grid_cash_flow_cost(
    slot: PlannedSlot,
    *,
    export_min_price: float = 0.0,
    export_fee_per_kwh: float = 0.0,
) -> float:
    """Return one slot's signed meter cash flow from final grid fields."""
    return grid_cash_flow_cost(
        slot.grid_import_kwh,
        slot.grid_export_kwh,
        slot.price.import_price,
        slot.price.export_price,
        export_min_price=export_min_price,
        export_fee_per_kwh=export_fee_per_kwh,
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
# Terminal-SoC end value (issue #1138)
# ---------------------------------------------------------------------------

# Share of a stored kWh's estimated use value that the end value claims.
# Below 1 so a flat-price horizon still prefers using the energy now over
# holding it for an estimated later use (issue #638).
TERMINAL_USE_VALUE_CONFIDENCE = 0.9

# Local hour at which the night window behind the overnight recharge cost
# ends.  The window starts at 00:00.
TERMINAL_NIGHT_END_HOUR = 6


def terminal_end_value(
    *,
    peak_import: float,
    night_import: float | None,
    charge_eff: float,
    discharge_eff: float,
    cycle_cost_per_kwh: float,
) -> float:
    """Return the end value ``V`` of one DC kWh still stored at horizon end.

    ``V`` is the lower of two estimates of what that kWh is worth after the
    horizon::

        use      = 0.9 × (η_dis × peak − cycle_cost)
        recharge = night_import / η_chg + cycle_cost
        V        = max(0, min(use, recharge))

    ``use`` is what discharging the kWh at the next peak would save,
    discounted because that peak is itself an estimate.  ``recharge`` is what
    storing the same kWh again overnight would cost.  When a cheap night
    follows the horizon, a leftover kWh is worth no more than its
    replacement, so the plan does not buy energy in the horizon only to end
    full.  A negative estimate is floored at zero, which disables the term.

    Args:
        peak_import: Import price of the next peak (currency/kWh).
        night_import: Mean overnight import price, or ``None`` when unknown
            (only the use side then applies).
        charge_eff: Charge efficiency fraction (0-1).
        discharge_eff: Discharge efficiency fraction (0-1).
        cycle_cost_per_kwh: Battery wear per kWh of throughput.

    Returns:
        ``V`` in currency per DC kWh, never negative.
    """
    use = TERMINAL_USE_VALUE_CONFIDENCE * (
        discharge_eff * peak_import - cycle_cost_per_kwh
    )
    if night_import is None or charge_eff <= 1e-9:
        return max(0.0, use)
    recharge = night_import / charge_eff + cycle_cost_per_kwh
    return max(0.0, min(use, recharge))


def terminal_end_value_from_last_day(
    slots: Sequence[PlannedSlot],
    tz: tzinfo | None,
    *,
    top_n: int,
    charge_eff: float,
    discharge_eff: float,
    cycle_cost_per_kwh: float,
) -> float | None:
    """Estimate the terminal end value ``V`` from the last day of prices.

    The day after the horizon is unknown, so the last calendar day with
    prices stands in for it.  ``peak`` is the mean of that day's *top_n*
    import prices and ``night_import`` the mean import price of its slots
    before :data:`TERMINAL_NIGHT_END_HOUR`.  See :func:`terminal_end_value`.

    Days the price source has not published yet already carry the last
    published day's prices (issue #1002), so the horizon's last calendar day
    is the last known day whether it was published or copied.  Past slots
    count too: here they are price data, not decisions.

    Args:
        slots: Chronological slot list with populated prices.
        tz: Timezone that defines calendar days and the night window.
        top_n: Number of most expensive slots that make up the peak,
            ``ceil(usable_kwh / max_discharge_per_slot)`` in the engine.
        charge_eff: Charge efficiency fraction (0-1).
        discharge_eff: Discharge efficiency fraction (0-1).
        cycle_cost_per_kwh: Battery wear per kWh of throughput.

    Returns:
        ``V`` in currency per DC kWh, or ``None`` when no slot has a finite
        import price.
    """
    priced = [s for s in slots if math.isfinite(s.price.import_price)]
    if not priced:
        return None
    last_day = max(as_tz(s.start, tz) for s in priced).date()
    day = [s for s in priced if as_tz(s.start, tz).date() == last_day]
    top = sorted((s.price.import_price for s in day), reverse=True)[: max(top_n, 1)]
    night = [
        s.price.import_price
        for s in day
        if as_tz(s.start, tz).hour < TERMINAL_NIGHT_END_HOUR
    ]
    peak_import = sum(top) / len(top)
    night_import = sum(night) / len(night) if night else None
    value = terminal_end_value(
        peak_import=peak_import,
        night_import=night_import,
        charge_eff=charge_eff,
        discharge_eff=discharge_eff,
        cycle_cost_per_kwh=cycle_cost_per_kwh,
    )
    log_planner(
        "debug",
        "[cost] terminal_end_value  day=%s  peak=%.4f  night=%s  value=%.4f",
        last_day.isoformat(),
        peak_import,
        f"{night_import:.4f}" if night_import is not None else "None",
        value,
    )
    return value


def terminal_soc_value(
    charged_kwh: float,
    discharged_kwh: float,
    end_value_per_kwh: float | None,
) -> float:
    """Return the terminal-SoC term for a DC battery flow (issue #1138).

    ``(discharged − charged) × V``: a penalty when the flow lowers the
    energy stored at horizon end, a credit when it raises it.  The MILP
    builds its ``ec``/``ed`` objective coefficients from unit flows and
    :func:`~custom_components.hsem.planner.cost_function.score_plan` sums
    this over each slot's flows, so both apply the same value behind the
    same activation gate.

    Every slot uses the same ``V`` and ``soc[t] = soc[0] + Σ(ec − ed)``, so
    the horizon total is ``−V × (E_end − E_0)``.  A cycle that leaves the
    end energy unchanged adds exactly zero, and the plan decides it on cash
    and cycle cost alone.  Undiscounted: it values a single point in time,
    the horizon end.

    Args:
        charged_kwh: DC energy stored (kWh).
        discharged_kwh: DC energy removed (kWh).
        end_value_per_kwh: ``V``; ``None`` or zero disables the term.

    Returns:
        The term in currency; ``0.0`` when disabled.
    """
    if end_value_per_kwh is None or abs(end_value_per_kwh) <= 1e-9:
        return 0.0
    return (discharged_kwh - charged_kwh) * end_value_per_kwh

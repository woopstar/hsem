"""Cost function helpers — override detection and cycle cost resolution."""

from __future__ import annotations

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.cost_types import CostWeights
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import resolve_cycle_cost
from custom_components.hsem.utils.units import usable_kwh_from_rated

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

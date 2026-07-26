"""Plan cost types — dataclasses shared by the cost function and its callers.

This module holds the two configuration/result types that were extracted from
:mod:`custom_components.hsem.planner.cost_function` to keep that file under the
30 KB hard limit.

:class:`CostWeights` — tunable weights and limits for ``score_plan``.
:class:`PlanCostBreakdown` — per-term breakdown returned by ``score_plan``.
"""

from __future__ import annotations

from dataclasses import dataclass


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

    # Minimum export price — exports below this are physically blocked by the
    # applier (inverter set to GRID_EXPORT_LIMIT_WATT).  Mirrors the clamping
    # in milp_optimizer.py so that cost_function scores match MILP assumptions.
    export_min_price: float = 0.0

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
            Per-slot opportunity cost of charging/discharging, summed across
            the horizon.  Each slot's contribution is capped by the
            differential between ``replacement_price_per_kwh`` and that
            slot's own sanitised import price (mirrors
            ``milp_optimizer.py``'s terminal-SoC objective term exactly,
            issue #655).  Negative (credit) when the plan nets more
            charging than discharging in slots where the differential is
            positive; positive (penalty) when it nets more discharging.
            Selector-only — does not enter :attr:`total_cost`.
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

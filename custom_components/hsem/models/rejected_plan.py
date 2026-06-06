"""Dataclass for a plan alternative that was considered but not selected."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RejectedPlan:
    """A plan alternative that was considered but not selected.

    Attributes:
        name:
            Short identifier for this alternative strategy
            (e.g. ``"charge_only_grid"``, ``"discharge_only"``).
        reason:
            Human-readable explanation of why this plan was rejected.
        estimated_cost:
            Estimated total grid cost (local currency) under this plan.
            Positive = net import cost; negative = net export revenue.
        import_cost:
            Total grid import cost for the horizon.
        export_revenue:
            Grid export revenue (positive = earned).
        conversion_loss:
            Cost of round-trip conversion losses.
        cycle_cost:
            Battery depreciation cost for cycled kWh.
        score:
            Selector objective (lower = better).
    """

    name: str
    reason: str
    estimated_cost: float = 0.0
    import_cost: float = 0.0
    export_revenue: float = 0.0
    conversion_loss: float = 0.0
    cycle_cost: float = 0.0
    score: float = 0.0

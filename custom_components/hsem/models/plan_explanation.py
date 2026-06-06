"""Dataclass for a human-readable explanation of the selected plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.const import STATE_UNKNOWN

from custom_components.hsem.models.rejected_plan import RejectedPlan


@dataclass
class PlanExplanation:
    """Human-readable explanation of the plan selected by the HSEM planner.

    Attached to :class:`PlannerOutput` after each planning run.  Suitable for
    surfacing as a Home Assistant sensor attribute so users can understand *why*
    a particular strategy was chosen and what alternatives were rejected.

    Attributes:
        selected_strategy:
            Short identifier for the active strategy
            (e.g. ``"charge_grid_discharge_peak"``).
        summary:
            One-sentence human-readable summary of the selected plan.
        score:
            Estimated savings of the selected plan versus doing nothing
            (battery fully idle).  Positive means the plan saves money
            over the horizon; negative means pre-charging overhead exceeds
            the expected discharge savings within this window.  Units are
            local currency.
        estimated_total_cost:
            Estimated net grid cost for the planning horizon (local currency).
            Positive = net import cost; negative = net export revenue.
        price_spread:
            Difference between the maximum and minimum import price in the
            planning horizon (local currency/kWh).  A larger spread indicates
            more arbitrage potential.
        peak_import_price:
            Maximum import price seen across all future slots.
        off_peak_import_price:
            Minimum import price seen across all future slots.
        forecast_pv_kwh:
            Total PV production forecast for the planning horizon (kWh).
        forecast_net_consumption_kwh:
            Total estimated net consumption (load minus PV) for the planning
            horizon (kWh).  Negative means net solar surplus.
        battery_soc_pct:
            Battery state-of-charge at the start of the planning run (%).
        battery_soc_at_end_pct:
            Estimated battery state-of-charge at the end of the planning
            horizon (%).
        constraints:
            List of active constraints or flags that influenced the decision
            (e.g. ``"winter_month"``, ``"no_price_spread"``,
            ``"excess_export_enabled"``).
        rejected_plans:
            Alternative plans that were evaluated and rejected, each with a
            name, reason, and estimated cost.
        hysteresis_active:
            ``True`` when plan-level hysteresis was applied and the previous
            plan was kept despite a new candidate having a slightly better score.
        hysteresis_reason:
            Human-readable explanation of the hysteresis decision, or ``""``
            when hysteresis is inactive or the plan was switched.
        previous_plan_name:
            Name of the winning plan from the previous planner run, or
            ``""`` on first run.
    """

    selected_strategy: str = STATE_UNKNOWN
    winner_name: str = ""  # e.g. "milp", "passive" — matches rejected_plans
    summary: str = ""
    score: float = 0.0
    estimated_total_cost: float = 0.0
    price_spread: float = 0.0
    peak_import_price: float = 0.0
    off_peak_import_price: float = 0.0
    forecast_pv_kwh: float = 0.0
    forecast_net_consumption_kwh: float = 0.0
    battery_soc_pct: float = 0.0
    battery_soc_at_end_pct: float = 0.0
    constraints: list[str] = field(default_factory=list)
    rejected_plans: list[RejectedPlan] = field(default_factory=list)
    # Hysteresis fields (issue #372)
    hysteresis_active: bool = False
    hysteresis_reason: str = ""
    previous_plan_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Serialise the explanation to a plain dict for HA attributes.

        Returns:
            A JSON-safe dictionary representation of the explanation.
        """
        return {
            "selected_strategy": self.selected_strategy,
            "winner_name": self.winner_name,
            "summary": self.summary,
            "score": round(self.score, 4),
            "estimated_total_cost": round(self.estimated_total_cost, 4),
            "price_spread": round(self.price_spread, 4),
            "peak_import_price": round(self.peak_import_price, 4),
            "off_peak_import_price": round(self.off_peak_import_price, 4),
            "forecast_pv_kwh": round(self.forecast_pv_kwh, 3),
            "forecast_net_consumption_kwh": round(self.forecast_net_consumption_kwh, 3),
            "battery_soc_pct": round(self.battery_soc_pct, 1),
            "battery_soc_at_end_pct": round(self.battery_soc_at_end_pct, 1),
            "constraints": list(self.constraints),
            "rejected_plans": [
                {
                    "name": rp.name,
                    "reason": rp.reason,
                    "estimated_cost": round(rp.estimated_cost, 4),
                    "import_cost": round(rp.import_cost, 4),
                    "export_revenue": round(rp.export_revenue, 4),
                    "conversion_loss": round(rp.conversion_loss, 4),
                    "cycle_cost": round(rp.cycle_cost, 4),
                    "score": round(rp.score, 4),
                }
                for rp in self.rejected_plans
            ],
        }

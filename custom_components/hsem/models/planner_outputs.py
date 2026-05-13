"""Pure-Python dataclasses for HSEM planner outputs.

These dataclasses represent every value that the planner produces after
processing a :class:`~custom_components.hsem.models.planner_inputs.PlannerInput`.
They carry *no* Home Assistant dependencies and can be compared, asserted on,
and serialised in plain unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from custom_components.hsem.utils.prices import SlotPrice

if TYPE_CHECKING:
    from custom_components.hsem.models.time_series import TimeSeriesIndex
    from custom_components.hsem.planner.cost_function import PlanCostBreakdown

# ---------------------------------------------------------------------------
# Plan explanation dataclasses
# ---------------------------------------------------------------------------


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
    """

    name: str
    reason: str
    estimated_cost: float = 0.0


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
    """

    selected_strategy: str = "unknown"
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

    def as_dict(self) -> dict[str, Any]:
        """Serialise the explanation to a plain dict for HA attributes.

        Returns:
            A JSON-safe dictionary representation of the explanation.
        """
        return {
            "selected_strategy": self.selected_strategy,
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
                }
                for rp in self.rejected_plans
            ],
        }


@dataclass
class PlannedSlot:
    """Planner decision for a single time slot.

    This is a pure-Python counterpart to
    :class:`~custom_components.hsem.models.hourly_recommendation.HourlyRecommendation`
    that can be constructed and inspected without Home Assistant.

    Electricity prices are stored as a
    :class:`~custom_components.hsem.utils.prices.SlotPrice` named-tuple on
    :attr:`price`.  Access individual prices via ``slot.price.import_price``
    and ``slot.price.export_price``.

    Attributes:
        start:
            Timezone-aware start of the slot.
        end:
            Timezone-aware end of the slot.
        price:
            Import and export prices for this slot as a :class:`SlotPrice`.
            Both values are in local currency/kWh and may be negative.
        solcast_pv_estimate:
            Forecast PV production in kWh for this slot.
        avg_house_consumption:
            Weighted (spike-aware) average house consumption in kWh.
        avg_house_consumption_1d:
            Raw 1-day average contribution in kWh.
        avg_house_consumption_3d:
            Raw 3-day average contribution in kWh.
        avg_house_consumption_7d:
            Raw 7-day average contribution in kWh.
        avg_house_consumption_14d:
            Raw 14-day average contribution in kWh.
        estimated_net_consumption:
            ``avg_house_consumption - solcast_pv_estimate`` in kWh.
            Negative means solar surplus.
        estimated_cost:
            Estimated grid cost (positive = import cost, negative = export
            revenue) in local currency for this slot.
        estimated_battery_soc:
            Estimated battery state-of-charge (%) at the *end* of the slot.
        estimated_battery_capacity:
            Estimated remaining usable battery capacity (kWh) at the *end*
            of the slot.
        batteries_charged:
            Energy scheduled to be charged into the battery during this slot
            (kWh, ≥ 0).  This is the energy *stored* after conversion losses
            are applied.
        batteries_discharged:
            Energy discharged from the battery during this slot (kWh, ≥ 0).
            Populated by the SoC simulation and clamped to the discharge
            power limit and available capacity.
        grid_import_kwh:
            Energy imported from the grid during this slot (kWh, ≥ 0).
            Equals load minus any battery discharge and PV that cover demand.
        grid_export_kwh:
            Energy exported to the grid during this slot (kWh, ≥ 0).
            Equals surplus PV and battery discharge beyond local load.
        recommendation:
            The ``Recommendations`` enum value chosen for this slot
            (stored as its string value so the output stays framework-free)
            or ``None`` if no decision has been made.
    """

    start: datetime
    end: datetime
    price: SlotPrice = field(default_factory=lambda: SlotPrice(0.0, 0.0))
    solcast_pv_estimate: float = 0.0
    avg_house_consumption: float = 0.0
    avg_house_consumption_1d: float = 0.0
    avg_house_consumption_3d: float = 0.0
    avg_house_consumption_7d: float = 0.0
    avg_house_consumption_14d: float = 0.0
    estimated_net_consumption: float = 0.0
    estimated_cost: float = 0.0
    estimated_battery_soc: float = 0.0
    estimated_battery_capacity: float = 0.0
    batteries_charged: float = 0.0
    batteries_discharged: float = 0.0
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    recommendation: str | None = None


@dataclass
class ChargeWindow:
    """A contiguous block of slots assigned to battery charging.

    Groups consecutive :class:`PlannedSlot` entries that share the same
    charge recommendation so tests can reason about charge windows at a
    higher level of abstraction.

    Attributes:
        start:
            Start of the first charging slot.
        end:
            End of the last charging slot.
        total_energy_kwh:
            Total energy scheduled to be charged during this window.
        avg_import_price:
            Mean import price across all slots in the window.
        recommendation:
            The recommendation value that marks these slots (typically
            ``"batteries_charge_grid"`` or ``"batteries_charge_solar"``).
    """

    start: datetime
    end: datetime
    total_energy_kwh: float = 0.0
    avg_import_price: float = 0.0
    recommendation: str = ""


@dataclass
class DischargeWindow:
    """A contiguous block of slots assigned to battery discharging.

    Attributes:
        start:
            Start of the first discharging slot.
        end:
            End of the last discharging slot.
        avg_import_price:
            Mean import price across all slots (proxy for discharge value).
        recommendation:
            The recommendation value that marks these slots (typically
            ``"batteries_discharge_mode"`` or
            ``"force_batteries_discharge"``).
    """

    start: datetime
    end: datetime
    avg_import_price: float = 0.0
    recommendation: str = ""


@dataclass
class PlannerOutput:
    """Complete output of one HSEM planning run.

    Attributes:
        slots:
            Ordered list of per-slot decisions covering the full planning
            horizon.  Ordered chronologically by ``start``.
        charge_windows:
            High-level view of charging windows derived from ``slots``.
        discharge_windows:
            High-level view of discharging windows derived from ``slots``.
        current_recommendation:
            Recommendation that would be applied *right now* (i.e. for the
            slot whose ``start <= now < end``), or ``None`` if no matching
            slot is found.
        battery_soc_at_end:
            Estimated battery SoC (%) at the end of the planning horizon.
        missing_inputs:
            Names / identifiers of any inputs that were absent or invalid
            during planning.  An empty list means all inputs were present.
        warnings:
            Human-readable warning strings emitted during planning.
        time_series_index:
            The shared :class:`~custom_components.hsem.models.time_series.TimeSeriesIndex`
            used during this planning run.  All slot boundaries, price, PV,
            load, import/export and SoC series are aligned to this axis.
            ``None`` when the planner was invoked without a valid horizon.
        extra:
            Arbitrary key-value pairs for debug / introspection purposes.
        explanation:
            Human-readable explanation of why the selected plan was chosen,
            including rejected alternatives, price spread, forecast summary,
            SoC status, and active constraints.
    """

    slots: list[PlannedSlot] = field(default_factory=list)
    charge_windows: list[ChargeWindow] = field(default_factory=list)
    discharge_windows: list[DischargeWindow] = field(default_factory=list)
    current_recommendation: str | None = None
    battery_soc_at_end: float = 0.0
    required_capacity_kwh: float = 0.0
    missing_inputs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    time_series_index: TimeSeriesIndex | None = field(default=None, repr=False)
    extra: dict[str, Any] = field(default_factory=dict)
    #: Human-readable explanation of why the selected plan was chosen and what
    #: alternatives were considered.  Populated by the planner engine.
    explanation: PlanExplanation = field(default_factory=PlanExplanation)
    #: Full cost breakdown for the selected plan, computed by the cost function.
    #: ``None`` when the planner produced no slots (e.g. missing inputs).
    plan_cost: PlanCostBreakdown | None = field(default=None, repr=False)
    #: All candidate plans that were evaluated during this planning run, in the
    #: order they were generated.  The first entry (name ``"baseline"``) always
    #: represents the current HSEM scheduling output.  Each candidate carries
    #: ``is_valid`` and ``rejection_reason`` set by the selector.
    #: Empty when the planner produced no slots (missing inputs).
    candidates: list[Any] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------
    # Convenience helpers used by tests
    # ------------------------------------------------------------------

    def slots_with_recommendation(self, recommendation: str) -> list[PlannedSlot]:
        """Return all slots whose recommendation equals *recommendation*."""
        return [s for s in self.slots if s.recommendation == recommendation]

    def charge_slot_count(self) -> int:
        """Return the number of slots assigned to any type of charging."""
        charge_values = {"batteries_charge_grid", "batteries_charge_solar"}
        return sum(1 for s in self.slots if s.recommendation in charge_values)

    def discharge_slot_count(self) -> int:
        """Return the number of slots assigned to any type of discharging."""
        discharge_values = {"batteries_discharge_mode", "force_batteries_discharge"}
        return sum(1 for s in self.slots if s.recommendation in discharge_values)

    def total_charged_energy_kwh(self) -> float:
        """Sum of ``batteries_charged`` across all slots."""
        return round(sum(s.batteries_charged for s in self.slots), 3)

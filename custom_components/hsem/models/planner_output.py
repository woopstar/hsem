"""Dataclass for the complete output of one HSEM planning run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from custom_components.hsem.models.charge_window import ChargeWindow
from custom_components.hsem.models.data_quality import DataQuality
from custom_components.hsem.models.discharge_window import DischargeWindow
from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.models.planned_slot import PlannedSlot

if TYPE_CHECKING:
    from custom_components.hsem.models.time_series import TimeSeriesIndex
    from custom_components.hsem.planner.cost_function import PlanCostBreakdown
    from custom_components.hsem.planner.ev_planner import EVChargingPlan


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
        data_quality:
            Structured diagnostics about the completeness of price and PV
            inputs for today and tomorrow.  Exposes which hours are missing
            so dashboards and logs can display the gap explicitly rather
            than silently treating missing slots as zero.
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
    #: Structured data-quality report for price and PV inputs.
    data_quality: DataQuality = field(default_factory=DataQuality)
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
    #: EV charging plan for the primary EV.  ``None`` when disabled.
    ev_charging_plan: EVChargingPlan | None = field(default=None, repr=False)
    #: EV charging plan for the second EV.  ``None`` when disabled.
    ev_second_charging_plan: EVChargingPlan | None = field(default=None, repr=False)
    #: Name of the winning candidate plan from the selector (e.g. ``"baseline"``,
    #: ``"aggressive"``).  Used by the coordinator to persist the active plan
    #: name across cycles for hysteresis (issue #372).
    winner_name: str = ""

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
        return round(sum(s.batteries_charged_kwh for s in self.slots), 3)

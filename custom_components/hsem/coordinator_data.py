"""CoordinatorData dataclass — the snapshot exposed to subscriber entities.

Extracted from :mod:`coordinator` to keep file sizes under the 30 KB / 1000-line
limit.  Re-exported from :mod:`coordinator` for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from custom_components.hsem.models.data_quality import DataQuality
from custom_components.hsem.models.financial_tracker import FinancialTracker
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.models.savings_tracker import SavingsTracker
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner.ev_planner import EVChargingPlan
from custom_components.hsem.utils.capacity_learner import CapacityLearner
from custom_components.hsem.utils.inverter_verify import CycleApplySummary
from custom_components.hsem.utils.prediction_tracker import PredictionTracker


@dataclass
class CoordinatorData:
    """Snapshot of a single HSEM update cycle.

    All fields are read-only from the perspective of subscribing entities.
    The coordinator replaces this object atomically at the end of every cycle.

    Attributes:
        cfg: Configuration values read from the config entry.
        live: Live HA entity state snapshot collected at the start of the cycle.
        hourly_recommendations: Full list of planner recommendation slots.
        hourly_recommendation: The recommendation slot active *right now*, or
            ``None`` when no matching slot exists.
        batteries_schedules: Parsed battery charge/discharge schedule windows.
        batteries_schedules_remaining_capacity_needed: Total remaining capacity
            needed across all enabled battery schedules (kWh).
        current_required_battery: Required battery capacity from the planner (kWh).
        state: Working-mode recommendation string for the current slot, or one
            of the :class:`~utils.recommendations.Recommendations` sentinel values.
        last_updated: ISO-format timestamp of the cycle that produced this data.
        next_update: ISO-format timestamp of the *next* scheduled cycle.
    """

    cfg: SensorConfig | None = None
    live: LiveState | None = None
    hourly_recommendations: list[HourlyRecommendation] = field(default_factory=list)
    hourly_recommendation: HourlyRecommendation | None = None
    batteries_schedules: list = field(default_factory=list)
    batteries_schedules_remaining_capacity_needed: float = 0.0
    current_required_battery: float = 0.0
    state: str | None = None
    last_updated: str | None = None
    next_update: str | None = None
    #: Aggregated write-and-verify results from the most recent hardware apply cycle.
    #: ``None`` before the first hardware-write cycle completes.
    apply_summary: CycleApplySummary | None = None
    #: Human-readable explanation of why the selected plan was chosen.
    plan_explanation: PlanExplanation = field(default_factory=PlanExplanation)
    #: Structured data-quality report for price, PV, and load-forecast inputs.
    data_quality: DataQuality = field(default_factory=DataQuality)
    #: EV optimal charging plan for the primary EV (None when disabled).
    ev_charging_plan: EVChargingPlan | None = None
    #: EV optimal charging plan for the second EV (None when disabled).
    ev_second_charging_plan: EVChargingPlan | None = None
    #: ISO-format timestamp of the override expiry, or None when no timed
    #: override is active (issue #317).
    override_expiry: str | None = None
    #: Savings tracker with actual vs missed savings metrics.
    savings_tracker: SavingsTracker = field(default_factory=SavingsTracker)
    #: Prediction accuracy tracker reference (SoC/MAE/action-mix scorecard, issue #601).
    prediction_tracker: PredictionTracker | None = None
    #: Capacity learner for auto-detecting battery usable capacity from
    #: BMS kWh-remaining and SoC readings.
    capacity_learner: CapacityLearner = field(default_factory=CapacityLearner)
    #: Per-hour solar forecast accuracy factors (0-23 → factor).
    #: Used by the solar confidence diagnostic sensor (issue #602).
    solar_hour_factors: dict[int, float] = field(default_factory=dict)
    #: Effective dynamic discharge floor SoC percentage, or None when the
    #: feature is disabled.  Computed by DynamicDischargeFloor.compute_floor().
    effective_discharge_floor_pct: float | None = None
    #: Diagnostics dict from the dynamic floor computation, or None when
    #: the feature is disabled.
    effective_discharge_floor_diag: dict | None = None
    #: Financial tracker with cumulative import cost and export income.
    financial_tracker: FinancialTracker | None = None
    #: OCPP charger session dict (CPID → ChargerSession) for sensor entities.
    ocpp_chargers: dict | None = None
    #: OCPP completed session log for the sessions sensor.
    ocpp_sessions: list | None = None
    #: Second EV OCPP charger session dict (CPID → ChargerSession).
    ocpp_second_chargers: dict | None = None
    #: Second EV OCPP completed session log.
    ocpp_second_sessions: list | None = None

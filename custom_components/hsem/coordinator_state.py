"""Shared coordinator state declared once for the behaviour mixins.

``HSEMDataUpdateCoordinator`` is split into behaviour mixins to satisfy the
repository's 30 KB / 1000-line file limit. Every mixin reads and writes state
that the concrete coordinator owns and initialises in ``__init__``. Without a
single shared declaration a type checker infers each attribute from whichever
mixin assigns it first — narrowing ``float | None`` to ``float`` and reporting
false conflicts.

This class declares that state once, and (at type-check time only) inherits
:class:`DataUpdateCoordinator` so the mixins also see ``hass``, ``data``, and
``async_set_updated_data``. It contains annotations and stubs only; it never
assigns and is never instantiated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.coordinator_helpers import (
    LoadForecastSignature,
)
from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.custom_sensors.state_collector import (  # noqa: F401 — kept for backward compat
    async_collect_all_states,
    build_sensor_config,
)
from custom_components.hsem.models.daily_plan_vs_actual_tracker import (
    DailyPlanVsActualTracker,
)
from custom_components.hsem.models.data_quality import DataQuality
from custom_components.hsem.models.financial_tracker import FinancialTracker
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.savings_tracker import SavingsTracker
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.planner.ev_planner import EVChargingPlan
from custom_components.hsem.utils.capacity_learner import CapacityLearner
from custom_components.hsem.utils.dynamic_floor import DynamicDischargeFloor
from custom_components.hsem.utils.ev_delivered_energy import EVDeliveredEnergyTracker
from custom_components.hsem.utils.forecast_tracker import ForecastTracker
from custom_components.hsem.utils.live_power import LivePowerEstimate, LivePowerWindow
from custom_components.hsem.utils.prediction_tracker import PredictionTracker
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector

if TYPE_CHECKING:
    from custom_components.hsem.ml.consumption_predictor import (
        ConsumptionPredictor,
    )

    _Base = DataUpdateCoordinator[CoordinatorData]
else:
    _Base = object


class CoordinatorSharedState(_Base):
    """Type-only declaration of state shared across the coordinator mixins."""

    _avg_house_consumption_entity_id_cache: dict[str, str]
    _capacity_learner: CapacityLearner
    _cfg: SensorConfig
    _config_entry: ConfigEntry
    _current_load_forecast_signature: LoadForecastSignature | None
    _current_required_battery: float
    _daily_plan_last_accumulated: datetime | None
    _daily_tracker: DailyPlanVsActualTracker
    _data_quality: DataQuality
    _dynamic_floor: DynamicDischargeFloor
    _effective_discharge_floor_diag: dict | None
    _effective_discharge_floor_pct: float | None
    _ev_charging_plan: EVChargingPlan | None
    _ev_delivered_energy_tracker: EVDeliveredEnergyTracker
    _ev_last_command_w: dict[str, float]
    _ev_second_charging_plan: EVChargingPlan | None
    _ev_second_delivered_energy_tracker: EVDeliveredEnergyTracker
    _event_update_pending: bool
    _financial_tracker: FinancialTracker
    _force_working_mode_entity: str | None
    _forecast_tracker: ForecastTracker
    _hourly_recommendation: HourlyRecommendation | None
    _hourly_recommendations: list[HourlyRecommendation]
    _hourly_timer_unsub: Callable[[], None] | None
    _interval_timer_unsub: Callable[[], None] | None
    _last_accumulation_ts: datetime | None
    _last_load_forecast_readiness_reason: str | None
    _last_plan_ev2_deadline: datetime | None
    _last_plan_ev2_smart_charging: bool | None
    _last_plan_ev2_target_soc: float | None
    _last_plan_ev_charging: bool
    _last_plan_ev_connected: bool | None
    _last_plan_ev_deadline: datetime | None
    _last_plan_ev_effective_energy_kwh: float | None
    _last_plan_ev_second_charging: bool
    _last_plan_ev_second_connected: bool | None
    _last_plan_ev_second_effective_energy_kwh: float | None
    _last_plan_ev_second_soc_below_target: bool
    _last_plan_ev_smart_charging: bool | None
    _last_plan_ev_soc_below_target: bool
    _last_plan_ev_target_soc: float | None
    _last_plan_force_mode: str
    _last_plan_import_price: float | None
    _last_plan_load_forecast_signature: LoadForecastSignature | None
    _last_plan_slot_start: datetime | None
    _last_planner_input: PlannerInput | None
    _last_planner_output: PlannerOutput | None
    _listener_unsubs: list
    _live: LiveState | None
    _live_power_window: LivePowerWindow
    _live_power_source_signature: tuple | None
    _last_plan_live_power_estimate: LivePowerEstimate | None
    _live_power_mismatch_since: datetime | None
    _live_power_mismatch_slot_start: datetime | None
    _live_power_replan_pending_slot: datetime | None
    _live_power_replanned_slot_start: datetime | None
    _live_power_replan_count: int
    _live_power_first_replan_direction: int | None
    _live_power_estimate_this_cycle: LivePowerEstimate | None
    _live_power_replan_request_slot_this_cycle: datetime | None
    _live_power_timer_unsub: Callable[[], None] | None
    _load_forecast_recovery_replan_pending: bool
    _ml_predictor: ConsumptionPredictor | None
    _net_consumption_ema: float | None
    _next_update: str | None
    _ocpp_event_debounce_task: asyncio.Task | None
    _ocpp_event_task: asyncio.Task | None
    _ocpp_server: OCPPServer | None
    _ocpp_second_server: OCPPServer | None
    _ocpp_sessions: list
    _options_update_debounce_task: asyncio.Task | None
    _options_update_task: asyncio.Task | None
    _override_expiry: datetime | None
    _plan_explanation: PlanExplanation
    _prediction_tracker: PredictionTracker
    _previous_planner_winner_name: str | None
    _previous_planner_winner_score: float
    _savings_tracker: SavingsTracker
    _snapshot: StateSnapshot | None
    _solar_corrector: SolarForecastCorrector
    _solar_corrector_processed: set[datetime]
    _timer_interval: timedelta | None
    _tracked_entities: set[str]
    _update_generation: int
    _update_lock: asyncio.Lock
    _window_hys_previous_rec: str | None
    _window_hys_previous_slot_start: datetime | None

    # Methods provided by sibling mixins or the concrete coordinator.
    async def _async_handle_update(self, event: Any = None) -> None: ...

    async def _run_planner_phase(self, *args: Any, **kwargs: Any) -> Any: ...

    async def _set_update_interval(
        self, override_minutes: int | None = None
    ) -> None: ...

    def _apply_planner_output(self, output: Any) -> None: ...

    def _persist_plan_state(self, *args: Any, **kwargs: Any) -> None: ...

    # Methods provided by CoordinatorLivePowerMixin.
    def _seed_live_power_window(
        self, now: datetime, cfg: Any, live: Any
    ) -> LivePowerEstimate:
        raise NotImplementedError

    def _actionable_live_power_replan_slot(
        self, now: datetime, estimate: LivePowerEstimate
    ) -> datetime | None: ...

    def _accept_live_power_plan_estimate(
        self,
        estimate: LivePowerEstimate,
        *,
        plan_now: datetime,
        requested_slot: datetime | None,
    ) -> None: ...

    def _live_power_ev_ambiguous(self, cfg: Any, live: Any) -> bool:
        raise NotImplementedError

    def _live_power_window_instance(self) -> LivePowerWindow:
        raise NotImplementedError

    def _clear_live_power_replan_state(self) -> None: ...

    def _reset_live_power_replan_budget(self) -> None: ...

    def reset_live_power_state(self) -> None: ...

    async def async_monitor_live_power(self, now: datetime | None = None) -> None: ...

    @staticmethod
    def _ev_effective_energy_kwh(
        ev_live: Any, battery_capacity_kwh: float
    ) -> float | None: ...

    # Method provided by CoordinatorEvDeadlinePacingMixin.
    def _ev_deadline_pacing_requires_replan(
        self, live: LiveState, now: datetime
    ) -> bool:
        raise NotImplementedError

    # Method provided by CoordinatorEvCommandStabilityMixin.
    def _apply_ev_command_stability(
        self, now: datetime, live: LiveState, cfg: SensorConfig
    ) -> None:
        raise NotImplementedError

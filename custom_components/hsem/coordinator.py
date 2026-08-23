"""DataUpdateCoordinator for the HSEM integration.

Single responsibility: run the shared HSEM polling pipeline once per interval
and expose the result as :class:`CoordinatorData` so that all subscribing
entities can read from one consistent snapshot.

Pipeline stages owned by the coordinator:

1. Reload config from the config entry.
2. Collect live HA entity states (:mod:`state_collector`).
3. Reset and generate recommendation time-slots.
4. Build battery-schedule objects from config.
5. Populate weighted house-consumption averages.
6. Populate electricity prices and Solcast PV estimates.
7. Run the pure-Python planner engine.
8. Resolve the current time-slot recommendation.

Hardware writes (inverter + battery commands) are **not** performed here; they
remain in :class:`~custom_components.hsem.custom_sensors.working_mode_sensor.HSEMWorkingModeSensor`
so that a "read_only" or "degraded mode" guard can still gate them at the entity
level.

Usage
-----
The coordinator is created in :func:`custom_components.hsem.__init__.async_setup_entry`
and stored on ``entry.runtime_data.coordinator``.  Each sensor platform retrieves
it from the config entry and passes it to the relevant entity constructors.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.hsem.coordinator_cycle import CoordinatorCycleMixin
from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.coordinator_helpers import (
    LoadForecastSignature,
    apply_force_charge_now,
    apply_load_forecast_hold,
    assess_load_forecast,
    future_consumption_profile_is_nonzero,
    live_demand_contradicts_zero_profile,
    load_forecast_signatures_match,
)
from custom_components.hsem.coordinator_lifecycle import CoordinatorLifecycleMixin
from custom_components.hsem.coordinator_planner_phase import (
    CoordinatorPlannerPhaseMixin,
)
from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.custom_sensors.state_collector import (  # noqa: F401 — kept for backward compat
    async_collect_all_states,
    build_battery_schedules,
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
from custom_components.hsem.utils.logger import (
    HSEM_LOGGER as _LOGGER,
    async_log,
)
from custom_components.hsem.utils.prediction_tracker import PredictionTracker
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector

if TYPE_CHECKING:
    from custom_components.hsem.ml.consumption_predictor import ConsumptionPredictor

# Compatibility exports retained for existing tests and integrations.
_apply_force_charge_now = apply_force_charge_now
_apply_load_forecast_hold = apply_load_forecast_hold
_assess_load_forecast = assess_load_forecast
_future_consumption_profile_is_nonzero = future_consumption_profile_is_nonzero
_live_demand_contradicts_zero_profile = live_demand_contradicts_zero_profile
_load_forecast_signatures_match = load_forecast_signatures_match


#: Consecutive failed cycles retried while newer state is pending before the
#: error is allowed to propagate. Unlike the fork's debounced loop this retry
#: has no sleep, so an unbounded retry on a permanently failing cycle would
#: hot-spin the event loop. After the bound the error surfaces to Home
#: Assistant and the ordinary interval timer takes over.
_MAX_FAILED_UPDATE_RETRIES = 2


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class HSEMDataUpdateCoordinator(
    CoordinatorLifecycleMixin,
    CoordinatorCycleMixin,
    CoordinatorPlannerPhaseMixin,
    DataUpdateCoordinator[CoordinatorData],
):
    """DataUpdateCoordinator for HSEM.

    Manages the shared polling lifecycle:

    - Registers a periodic timer (``update_interval`` minutes from config) via
      :func:`~homeassistant.helpers.event.async_track_time_interval`.
    - Registers an hourly time-change listener at HH:00:10 to guarantee an
      update at the top of every hour even if the interval timer drifts.
    - Runs the full pipeline under an :class:`asyncio.Lock` so that concurrent
      triggers (e.g. a state-change event arriving during an in-progress cycle)
      are silently dropped rather than queued.

    Entities subscribe via
    :class:`~homeassistant.helpers.update_coordinator.CoordinatorEntity` and
    receive a push notification each time :attr:`data` is refreshed.
    """

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialise the coordinator.

        Args:
            hass: The Home Assistant instance.
            config_entry: The HSEM config entry whose options drive the pipeline.
        """
        super().__init__(
            hass,
            _LOGGER,
            name="HSEM",
            # DataUpdateCoordinator manages an internal timer; we build our own
            # interval timer below for dynamic interval support, so set None to
            # disable the built-in timer entirely (Bronze rule: appropriate-polling).
            update_interval=None,
        )
        self._config_entry = config_entry

        # Lock prevents concurrent executions of the update pipeline.
        self._update_lock = asyncio.Lock()
        # Registered state changes advance this generation before a cycle runs.
        # If it changes in flight, the stale cycle is discarded and one durable
        # follow-up cycle runs from a fresh snapshot.
        self._update_generation: int = 0
        self._event_update_pending: bool = False

        # Timer handles — cancelled/re-registered when the interval changes.
        self._interval_timer_unsub: Callable[[], None] | None = None
        self._hourly_timer_unsub: Callable[[], None] | None = None
        self._timer_interval: timedelta | None = None

        # Per-cycle mutable state (not exposed directly; packaged into CoordinatorData).
        self._cfg: SensorConfig = build_sensor_config(config_entry)
        self._live: LiveState | None = None
        self._snapshot: StateSnapshot | None = None
        self._hourly_recommendations: list[HourlyRecommendation] = []
        self._hourly_recommendation: HourlyRecommendation | None = None
        self._batteries_schedules: list = []
        self._batteries_schedules_remaining_capacity_needed: float = 0.0
        self._current_required_battery: float = 0.0
        self._next_update: str | None = None

        # Entity resolution cache (persisted across cycles).
        self._force_working_mode_entity: str | None = None
        self._tracked_entities: set[str] = set()
        # Unsubscribe callbacks for state-change listeners registered via
        # state_collector._register_listeners.  Cancelled during async_teardown.
        self._listener_unsubs: list = []
        self._avg_house_consumption_entity_id_cache: dict[str, str] = {}
        # Most recent plan explanation produced by the planner engine.
        self._plan_explanation: PlanExplanation = PlanExplanation()
        # Most recent data quality report produced by the planner engine.
        self._data_quality: DataQuality = DataQuality()
        # Most recent EV charging plans from the planner engine.
        self._ev_charging_plan: EVChargingPlan | None = None
        self._ev_second_charging_plan: EVChargingPlan | None = None
        # Most recent planner input/output retained for diagnostics dumps.
        self._last_planner_input: PlannerInput | None = None
        self._last_planner_output: PlannerOutput | None = None

        # Previous planner winner name and score for hysteresis (issue #372).
        # Persisted across cycles so the planner can compare against the
        # previously active plan.
        self._previous_planner_winner_name: str | None = None
        self._previous_planner_winner_score: float = 0.0

        # Window-level hysteresis state (issue #315).
        # Persisted across cycles so the hold-time check can compare against
        # the previously active current-slot recommendation.

        # EMA-smoothed live net consumption (W).  Damped so transients
        # (støvsuger, kaffemaskine, cloud shadows) don't kill the EV
        # charging setpoint for the rest of a 15-minute slot.  Initialised
        # on the first cycle and updated every subsequent cycle.
        self._net_consumption_ema: float | None = None
        self._window_hys_previous_rec: str | None = None
        self._window_hys_previous_slot_start: datetime | None = None

        # Event-driven re-planning — track state at last plan to avoid
        # re-solving the MILP when nothing material has changed.
        self._last_plan_ev_connected: bool | None = False
        self._last_plan_ev_charging: bool = False
        self._last_plan_ev_soc_below_target: bool = False
        self._last_plan_ev_effective_energy_kwh: float | None = None
        self._last_plan_ev_second_connected: bool | None = False
        self._last_plan_ev_second_charging: bool = False
        self._last_plan_ev_second_soc_below_target: bool = False
        self._last_plan_ev_second_effective_energy_kwh: float | None = None
        self._last_plan_force_mode: str = "auto"
        self._last_plan_slot_start: datetime | None = None
        self._last_plan_import_price: float | None = None
        self._last_plan_load_forecast_signature: LoadForecastSignature | None = None
        self._current_load_forecast_signature: LoadForecastSignature | None = None
        self._load_forecast_recovery_replan_pending: bool = False
        self._last_load_forecast_readiness_reason: str | None = None
        # EV planned-load config that affects planner optimisation.
        self._last_plan_ev_target_soc: float | None = None
        self._last_plan_ev_smart_charging: bool | None = None
        self._last_plan_ev_deadline: datetime | None = None
        self._last_plan_ev2_target_soc: float | None = None
        self._last_plan_ev2_smart_charging: bool | None = None
        self._last_plan_ev2_deadline: datetime | None = None

        # Solar forecast accuracy auto-corrector (issue #602).
        self._solar_corrector: SolarForecastCorrector = SolarForecastCorrector()
        # Set of slot start times already fed to the solar corrector.
        self._solar_corrector_processed: set[datetime] = set()

        # Forecast-vs-actual tracker (predicted-vs-actual tracking, issue #373).
        self._forecast_tracker: ForecastTracker = ForecastTracker(max_slots=2880)
        # Prediction accuracy tracker — SoC/MAE/action-mix scorecard (issue #601).
        self._prediction_tracker: PredictionTracker = PredictionTracker(
            max_records=2880
        )
        # Daily plan-vs-actual tracker (diagnostic sensor with 90-day history).
        # The history file path is set in async_setup() once hass.config is available.
        self._daily_tracker: DailyPlanVsActualTracker = DailyPlanVsActualTracker()
        # Savings tracker (actual vs missed savings with 90-day history).
        self._savings_tracker: SavingsTracker = SavingsTracker()
        # Financial tracker — cumulative import cost and export income (never reset).
        # The history file path is set in async_setup() once hass.config is available.
        self._financial_tracker: FinancialTracker = FinancialTracker()
        # Last slot end time accumulated from planner output (prevents double-counting).
        self._daily_plan_last_accumulated: datetime | None = None
        # Timestamp of the last actual-energy accumulation cycle.
        self._last_accumulation_ts: datetime | None = None

        # Override expiry timestamp for timed manual overrides (issue #317).
        # Set by set_temporary_override when duration_minutes is provided.
        # Checked on every update cycle; when expired, the override is cleared
        # automatically and the planner resumes control.
        self._override_expiry: datetime | None = None

        # Dynamic self-learning discharge floor (issue #600).
        self._dynamic_floor: DynamicDischargeFloor = DynamicDischargeFloor()
        self._effective_discharge_floor_pct: float | None = None
        self._effective_discharge_floor_diag: dict | None = None

        # Battery capacity learner (issue #605).
        self._capacity_learner: CapacityLearner = CapacityLearner()
        # Session-local credit for vehicle APIs whose SoC updates lag behind
        # measured charger power. Never persisted across integration restarts.
        self._ev_delivered_energy_tracker = EVDeliveredEnergyTracker()
        self._ev_second_delivered_energy_tracker = EVDeliveredEnergyTracker()

        # Embedded OCPP 1.6 server for EV charger control (issue #603).
        # One server per EV: primary EV on ``_ocpp_server``, optional second
        # EV on ``_ocpp_second_server`` (separate port).
        self._ocpp_server: OCPPServer | None = None
        self._ocpp_second_server: OCPPServer | None = None
        self._ocpp_sessions: list = []

        # ML consumption predictor — cached across cycles so the retrain
        # gate can skip re-fitting when no new history has arrived.
        self._ml_predictor: ConsumptionPredictor | None = None

        # Background task handle for option-change-triggered pipeline runs.
        # Tracked so repeated toggles cancel the pending run and so teardown
        # can cancel a still-running task (issue: switch toggles felt frozen
        # because the update listener awaited the full MILP/ML pipeline).
        self._options_update_task: asyncio.Task | None = None
        # Debounce task for option changes.  Rapid toggles restart this timer
        # so the planner only runs once after the user stops clicking.
        self._options_update_debounce_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def _async_handle_update(self, event: Event | None = None) -> None:
        """Run updates serially and retain state-change events received in flight."""
        if event is not None:
            self._update_generation = getattr(self, "_update_generation", 0) + 1
            self._event_update_pending = True
        if self._update_lock.locked():
            async_log(
                "debug",
                "------ Coordinator update deferred: a previous cycle is still running.",
            )
            return
        async with self._update_lock:
            failed_retries = 0
            while True:
                self._event_update_pending = False
                try:
                    await self._async_run_update_cycle()
                except Exception as err:
                    # An event that arrived while this cycle was running set the
                    # pending flag again. Letting the exception escape here would
                    # strand that flag: the loop exits, nothing reschedules, and
                    # the newer state is never planned against until some later
                    # unrelated event happens to arrive.
                    if (
                        not self._event_update_pending
                        or failed_retries >= _MAX_FAILED_UPDATE_RETRIES
                    ):
                        raise
                    failed_retries += 1
                    async_log(
                        "warning",
                        "------ Coordinator cycle failed at generation %d while "
                        "newer state was pending; retrying (%d/%d): %s",
                        getattr(self, "_update_generation", 0),
                        failed_retries,
                        _MAX_FAILED_UPDATE_RETRIES,
                        err,
                    )
                    continue
                failed_retries = 0
                if not self._event_update_pending:
                    break

    @override
    async def _async_update_data(self) -> CoordinatorData:
        """Called by DataUpdateCoordinator's internal timer (fallback only).

        The coordinator manages its own interval timer; this method acts as
        a safety-net in case the HA-managed polling fires.  It delegates to
        the same guarded handler to avoid double-execution.
        """
        await self._async_handle_update(None)
        # Return the last data if available, else an empty snapshot.
        return self.data if self.data is not None else CoordinatorData()

    # ------------------------------------------------------------------
    # Timer management
    # ------------------------------------------------------------------

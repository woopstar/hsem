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
import contextlib
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from custom_components.hsem.const import EMA_ALPHA_NET_CONSUMPTION
from custom_components.hsem.coordinator_builder import (
    build_planner_input,
    generate_recommendation_intervals,
)
from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.coordinator_helpers import (
    LoadForecastSignature,
    _SimpleSlot,
    apply_current_ev_power_override,
    apply_force_charge_now,
    apply_load_forecast_hold,
    assess_load_forecast,
    future_consumption_profile_is_nonzero,
    live_demand_contradicts_zero_profile,
    load_forecast_signatures_match,
)
from custom_components.hsem.coordinator_tracking import (
    accumulate_daily_plan_actuals,
    accumulate_financials,
    accumulate_forecast_actuals,
    accumulate_savings,
    init_financial_tracker,
    init_prediction_tracker,
    register_forecasts_from_planner,
)
from custom_components.hsem.custom_sensors.hourly_data_populator.consumption import (
    populate_avg_house_consumption_from_snapshot,
)
from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    populate_price_and_solcast_from_snapshot,
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
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.charge_scheduler import apply_window_hysteresis
from custom_components.hsem.planner.ev_planner import EVChargingPlan
from custom_components.hsem.utils.capacity_learner import CapacityLearner
from custom_components.hsem.utils.datetime_utils import (
    as_tz,
    now as hsem_now,
    utc_key,
    utc_now_iso,
)
from custom_components.hsem.utils.dynamic_floor import DynamicDischargeFloor
from custom_components.hsem.utils.forecast_tracker import ForecastTracker
from custom_components.hsem.utils.logger import (
    HSEM_LOGGER as _LOGGER,
    async_log,
    set_hsem_verbose,
)
from custom_components.hsem.utils.misc import ema_filter, get_config_value
from custom_components.hsem.utils.prediction_tracker import PredictionTracker
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector
from custom_components.hsem.utils.units import usable_kwh_from_rated
from custom_components.hsem.utils.weekday_profile import weekday_profile

if TYPE_CHECKING:
    from custom_components.hsem.ml.consumption_predictor import ConsumptionPredictor

# Compatibility exports retained for existing tests and integrations.
_apply_force_charge_now = apply_force_charge_now
_apply_load_forecast_hold = apply_load_forecast_hold
_assess_load_forecast = assess_load_forecast
_future_consumption_profile_is_nonzero = future_consumption_profile_is_nonzero
_live_demand_contradicts_zero_profile = live_demand_contradicts_zero_profile
_load_forecast_signatures_match = load_forecast_signatures_match


# Seconds to wait after the last options change before scheduling a planner
# run.  Rapid switch/number/time toggles restart this timer, so the planner
# only rebuilds once after the user stops clicking.
OPTIONS_UPDATE_DEBOUNCE_SECONDS = 0.25


class _StaleUpdateCycle(Exception):
    """Raised when a newer registered state event invalidates an in-flight cycle."""


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class HSEMDataUpdateCoordinator(DataUpdateCoordinator[CoordinatorData]):
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
        self._last_plan_ev_second_connected: bool | None = False
        self._last_plan_ev_second_charging: bool = False
        self._last_plan_ev_second_soc_below_target: bool = False
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

        # Embedded OCPP 1.6 server for EV charger control (issue #603).
        self._ocpp_server: OCPPServer | None = None
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

    async def async_setup(self) -> None:
        """Register timers and run the first update cycle.

        Call this once after the coordinator is created (from
        :func:`~custom_components.hsem.__init__.async_setup_entry`).
        """
        # Restore prediction diagnostics before the first cycle so an options
        # reload does not restart the scorecard warm-up window.
        try:
            await init_prediction_tracker(self._prediction_tracker, self.hass)
        except Exception as e:
            async_log("error", "Failed to initialise prediction tracker: %s", e)

        # Initialise the financial tracker — lazy load from disk on first access.
        # This must happen before the first update cycle so the tracker
        # is available when accumulation runs.
        try:
            await init_financial_tracker(self._financial_tracker, self.hass)
        except Exception as e:
            async_log("error", "Failed to initialise financial tracker: %s", e)

        # Start the embedded OCPP 1.6 server if enabled (issue #603).
        cfg = build_sensor_config(self._config_entry)
        if cfg.ocpp_enabled:
            try:
                self._ocpp_server = OCPPServer(
                    hass=self.hass,
                    host="0.0.0.0",
                    port=cfg.ocpp_port,
                    start_window_s=cfg.ocpp_start_window_s,
                    stop_window_s=cfg.ocpp_stop_window_s,
                )
                await self._ocpp_server.start()
                async_log("info", "OCPP server started on port %d", cfg.ocpp_port)
            except Exception as e:
                async_log("error", "Failed to start OCPP server: %s", e)
                self._ocpp_server = None

        # Run an immediate first cycle so entities have data before first render.
        await self._async_handle_update(None)

        # Hourly tick — guarantees a refresh at the top of every hour.
        self._hourly_timer_unsub = async_track_time_change(
            self.hass,
            self._async_handle_update,  # type: ignore[arg-type]  # HA stub expects Callable[[datetime], ...]; our callback also serves as coordinator update callback
            hour="*",
            minute=0,
            second=10,
        )

    async def async_teardown(self) -> None:
        """Cancel all registered timers and state-change listeners.

        Called from :func:`~custom_components.hsem.__init__.async_unload_entry`.
        """
        # Cancel the base DataUpdateCoordinator's internal refresh timer
        # (set to 24 h as a fallback).  Without this the timer holds a
        # reference to the coordinator and prevents garbage collection.
        unsub_refresh = getattr(self, "_unsub_refresh", None)
        if unsub_refresh is not None:
            unsub_refresh()
        if self._hourly_timer_unsub is not None:
            self._hourly_timer_unsub()
            self._hourly_timer_unsub = None
        if self._interval_timer_unsub is not None:
            self._interval_timer_unsub()
            self._interval_timer_unsub = None
        for unsub in self._listener_unsubs:
            unsub()
        self._listener_unsubs.clear()
        # Cancel midnight timer from daily tracker if registered.
        daily_tracker = getattr(self, "_daily_tracker", None)
        if daily_tracker is not None:
            midnight = getattr(daily_tracker, "_midnight_unsub", None)
            if midnight is not None:
                midnight()
                daily_tracker._midnight_unsub = None  # type: ignore[attr-defined]

        # Stop the OCPP server if it was started.
        ocpp = getattr(self, "_ocpp_server", None)
        if ocpp is not None:
            await ocpp.stop()
            self._ocpp_server = None

        # Cancel any pending options-update background task and debounce timer.
        task = getattr(self, "_options_update_task", None)
        if task is not None and not task.done():
            task.cancel()
            self._options_update_task = None
        debounce_task = getattr(self, "_options_update_debounce_task", None)
        if debounce_task is not None and not debounce_task.done():
            debounce_task.cancel()
            self._options_update_debounce_task = None

    async def async_options_updated(self) -> None:
        """Schedule a debounced pipeline re-run after an options change.

        Runs the update cycle as a **background task** so the caller (the
        config-entry update listener, triggered synchronously by
        ``async_update_entry`` from switch/number/time entities) returns
        immediately.  Without this, toggling a switch would block the HA
        service call until the entire read → plan (MILP/ML) → apply
        pipeline finished — making the UI feel frozen.

        A short debounce window is used so rapid switch/number/time toggles
        only trigger a single planner run after the user stops clicking.
        The background task is created with ``eager_start=False`` so the
        switch service call returns before any setup work begins.
        """
        if (
            self._options_update_debounce_task is not None
            and not self._options_update_debounce_task.done()
        ):
            self._options_update_debounce_task.cancel()
        self._options_update_debounce_task = self.hass.async_create_task(
            self._async_options_update_debounced(),
            name="hsem_options_update_debounce",
            eager_start=False,
        )

    async def _async_options_update_debounced(self) -> None:
        """Wait for the debounce window, then schedule the planner run."""
        try:
            await asyncio.sleep(OPTIONS_UPDATE_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            # Superseded by a newer options update — not an error.
            async_log(
                "debug",
                "[coordinator] options-update debounce cancelled — "
                "superseded by a newer options change.",
            )
            return

        # Cancel any still-pending previous options-update task before
        # scheduling a fresh run with the latest option state.
        if (
            self._options_update_task is not None
            and not self._options_update_task.done()
        ):
            self._options_update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._options_update_task

        self._options_update_task = self.hass.async_create_task(
            self._async_options_update_background(),
            name="hsem_options_update",
            eager_start=False,
        )
        self._options_update_debounce_task = None

    async def _async_options_update_background(self) -> None:
        """Background wrapper: run the update cycle, swallowing cancellation."""
        try:
            await self._async_handle_update(None)
        except asyncio.CancelledError:
            # Superseded by a newer options update — not an error.
            async_log(
                "debug",
                "[coordinator] options-update task cancelled — superseded by a "
                "newer options change.",
            )

    # ------------------------------------------------------------------
    # Internal update pipeline
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
            while True:
                self._event_update_pending = False
                await self._async_run_update_cycle()
                if not self._event_update_pending:
                    break

    async def _async_collect_and_populate(
        self, now: datetime
    ) -> tuple[bool, str | None]:
        """Collect live state, populate consumption/prices, determine working state.

        Returns:
            (consumption_ok, state) — whether consumption data is ready and the
            current working-mode state string (or None for full pipeline).
        """
        # 1. Reload config from the config entry.
        self._cfg = build_sensor_config(self._config_entry)
        cfg = self._cfg

        # 2. Collect ALL HA entity states once into an immutable snapshot.
        (
            self._snapshot,
            self._force_working_mode_entity,
            new_unsubs,
        ) = await async_collect_all_states(
            self,
            cfg,
            self._force_working_mode_entity,
            self._tracked_entities,
            self._avg_house_consumption_entity_id_cache,
            entry_id=self._config_entry.entry_id,
        )
        self._listener_unsubs.extend(new_unsubs)
        self._live = self._snapshot.live
        live = self._live

        # Feed the capacity learner with BMS readings (issue #605).
        if (
            live.bms_kwh_remaining is not None
            and live.huawei_batteries_soc_pct is not None
        ):
            getattr(self, "_capacity_learner", CapacityLearner()).update(
                live.bms_kwh_remaining, live.huawei_batteries_soc_pct
            )

        # Update the weekday/weekend consumption profile (issue #612).
        house_w = live.house_consumption_power_w
        if house_w is not None and house_w > 0:
            weekday_profile.update(
                dow=now.weekday(),
                slot=now.hour,
                value_kwh=house_w / 1000.0,
            )

        # Apply EMA smoothing to live net consumption.
        self._net_consumption_ema = ema_filter(
            live.net_consumption_w,
            self._net_consumption_ema,
            EMA_ALPHA_NET_CONSUMPTION,
        )
        live.net_consumption_w = self._net_consumption_ema

        # Override expiry check (issue #317).
        if self._override_expiry is not None:
            if now >= self._override_expiry:
                async_log(
                    "debug",
                    "Timed override EXPIRED — clearing select entity to 'auto'.",
                )
                await self.hass.services.async_call(
                    "select",
                    "select_option",
                    {"entity_id": live.force_working_mode, "option": "auto"},
                    blocking=True,
                )
                live.force_working_mode_state = "auto"
                self._override_expiry = None
            elif live.force_working_mode_state == "auto":
                async_log(
                    "debug",
                    "Override manually cleared before expiry — removing expiry tracking.",
                )
                self._override_expiry = None

        # 3. Reset and generate recommendation time-slots.
        self._hourly_recommendation = None
        self._hourly_recommendations = generate_recommendation_intervals(
            cfg.recommendation_interval_minutes,
            cfg.recommendation_interval_length,
        )

        # 4. Build battery-schedule objects from config.
        self._batteries_schedules = build_battery_schedules(cfg)
        self._batteries_schedules.sort(key=lambda x: x.start)

        # 5. Populate weighted house-consumption averages.
        set_hsem_verbose(cfg.verbose_logging)

        if cfg.ml_consumption_enabled:
            from custom_components.hsem.ml.populator import (
                populate_ml_house_consumption,
            )

            (
                consumption_ok,
                self._ml_predictor,
            ) = await populate_ml_house_consumption(
                self.hass,
                self._hourly_recommendations,
                cfg,
                self._ml_predictor,
            )
            async_log(
                "debug",
                "[ml] populate_ml_house_consumption returned %s",
                consumption_ok,
            )

            if not consumption_ok:
                async_log(
                    "debug",
                    "[ml] ML consumption failed — falling back to legacy avg sensors.",
                )
                consumption_ok = populate_avg_house_consumption_from_snapshot(
                    self._hourly_recommendations,
                    self._snapshot,
                    cfg,
                    self._avg_house_consumption_entity_id_cache,
                    entry_id=self._config_entry.entry_id,
                )
        else:
            consumption_ok = populate_avg_house_consumption_from_snapshot(
                self._hourly_recommendations,
                self._snapshot,
                cfg,
                self._avg_house_consumption_entity_id_cache,
                entry_id=self._config_entry.entry_id,
            )
            async_log(
                "debug",
                "[avg] populate_avg_house_consumption_from_snapshot returned %s, "
                "cache has %d entries, snapshot has %d energy_avg values",
                consumption_ok,
                len(self._avg_house_consumption_entity_id_cache),
                len(self._snapshot.energy_average_values),
            )

        load_readiness = assess_load_forecast(
            self._hourly_recommendations,
            now,
            population_succeeded=consumption_ok,
            live_house_demand_w=live.house_consumption_power_w,
        )
        consumption_ok = load_readiness.ready
        self._current_load_forecast_signature = load_readiness.signature
        readiness_reason = load_readiness.reason
        previous_reason = getattr(self, "_last_load_forecast_readiness_reason", None)
        if consumption_ok:
            self._data_quality = replace(
                self._data_quality,
                load_forecast_ready=True,
                load_forecast_reason=None,
            )
            if previous_reason is not None:
                async_log(
                    "info",
                    "[load] Forecast recovered (%s); a fresh plan is required.",
                    previous_reason,
                )
        else:
            assert readiness_reason is not None
            self._load_forecast_recovery_replan_pending = True
            self._data_quality = replace(
                self._data_quality,
                load_forecast_ready=False,
                load_forecast_reason=readiness_reason,
            )
            if readiness_reason != previous_reason:
                async_log(
                    "warning",
                    "[load] Forecast is not ready (%s); automatic control will "
                    "publish a strict storage hold.",
                    readiness_reason,
                )
        self._last_load_forecast_readiness_reason = readiness_reason

        # Adjust timer based on missing-entities or pending-consumption status.
        if live.missing_entities or not consumption_ok:
            await self._set_update_interval(1)
        else:
            await self._set_update_interval()

        # 6. Determine working state: forced, missing, or full pipeline.
        state: str | None = None

        if live.missing_entities and live.force_working_mode_state == "auto":
            state = Recommendations.MissingInputEntities.value
            async_log("debug", "Missing input entities, skipping calculations.")
        elif not consumption_ok and live.force_working_mode_state == "auto":
            pass  # handled below after price/solcast population
        elif live.force_working_mode_state != "auto":
            state = str(live.force_working_mode_state)
            async_log(
                "debug",
                "Force working mode is activated. Setting working mode to %s",
                live.force_working_mode_state,
            )

        # 7. Populate electricity prices and Solcast PV estimates.
        populate_price_and_solcast_from_snapshot(
            self._hourly_recommendations,
            self._snapshot,
            cfg,
        )

        return consumption_ok, state

    def _capture_accepted_plan_state(self) -> dict[str, Any]:
        """Capture accepted plan state that a stale or failed cycle may mutate."""
        names = (
            "_last_planner_input",
            "_last_planner_output",
            "_last_plan_slot_start",
            "_previous_planner_winner_name",
            "_previous_planner_winner_score",
            "_window_hys_previous_rec",
            "_window_hys_previous_slot_start",
            "_current_required_battery",
            "_plan_explanation",
            "_data_quality",
            "_ev_charging_plan",
            "_ev_second_charging_plan",
            "_hourly_recommendation",
            "_hourly_recommendations",
        )
        return {name: getattr(self, name, None) for name in names}

    def _restore_accepted_plan_state(self, state: dict[str, Any]) -> None:
        """Restore accepted plan state after a stale, failed, or cancelled cycle."""
        for name, value in state.items():
            setattr(self, name, value)

    async def _async_run_update_cycle(self) -> None:
        """Execute the full collect → populate → plan cycle.

        On success, packages the results into a :class:`CoordinatorData` and
        calls :meth:`async_set_updated_data` to notify all subscriber entities.

        Raises:
            UpdateFailed: When an unrecoverable error occurs during the pipeline.
        """
        async_log("debug", "------ HSEM Coordinator: starting update cycle")
        captured_generation = getattr(self, "_update_generation", 0)
        accepted_plan_state = self._capture_accepted_plan_state()
        now = hsem_now()

        try:
            # Phases 1-7: collect live state, populate consumption/prices/solcast.
            consumption_ok, state = await self._async_collect_and_populate(now)
            live = self._live
            assert live is not None, "_async_collect_and_populate must set _live"
            cfg = self._cfg

            # -----------------------------------------------------------------------
            # Forecast-vs-actual accumulation (issue #373)
            # -----------------------------------------------------------------------
            (
                self._last_accumulation_ts,
                prediction_record_added,
            ) = accumulate_forecast_actuals(
                now=now,
                live=live,
                hourly_recommendations=self._hourly_recommendations,
                forecast_tracker=getattr(
                    self, "_forecast_tracker", ForecastTracker(max_slots=2880)
                ),
                last_accumulation_ts=self._last_accumulation_ts,
                solar_corrector=getattr(
                    self, "_solar_corrector", SolarForecastCorrector()
                ),
                solar_corrector_processed=getattr(
                    self, "_solar_corrector_processed", set()
                ),
                prediction_tracker=getattr(
                    self, "_prediction_tracker", PredictionTracker(max_records=2880)
                ),
                last_planner_output=getattr(self, "_last_planner_output", None),
            )
            if (
                prediction_record_added
                and self._prediction_tracker.history_file
                and not await self._prediction_tracker.save_history()
            ):
                async_log("warning", "Failed to persist prediction tracker state")

            load_hold = apply_load_forecast_hold(
                self._hourly_recommendations,
                live,
                now,
                load_forecast_ready=consumption_ok,
            )
            if load_hold is not None:
                reason = self._last_load_forecast_readiness_reason
                assert reason is not None
                self._hourly_recommendation = load_hold
                state = load_hold.recommendation
                self._plan_explanation = PlanExplanation(
                    selected_strategy="safety_hold",
                    winner_name="safety_hold",
                    summary=(
                        "Battery held because the house-load forecast is not "
                        f"ready ({reason})."
                    ),
                    constraints=[f"load_forecast:{reason}"],
                )

            fresh_plan = False
            planner_output_to_commit: PlannerOutput | None = None
            if (
                live.force_working_mode_state == "auto"
                and not live.missing_entities
                and consumption_ok
            ):
                (
                    state,
                    fresh_plan,
                    planner_output_to_commit,
                ) = await self._run_planner_phase(
                    now,
                    live,
                    cfg,
                    state,
                    consumption_ok,
                    captured_generation,
                )

        except _StaleUpdateCycle:
            self._restore_accepted_plan_state(accepted_plan_state)
            return
        except asyncio.CancelledError:
            self._restore_accepted_plan_state(accepted_plan_state)
            raise
        except Exception as exc:
            self._restore_accepted_plan_state(accepted_plan_state)
            raise UpdateFailed(f"HSEM update cycle failed: {exc}") from exc

        # Final sort and timestamp.
        self._hourly_recommendations.sort(key=lambda x: x.start)
        last_updated = utc_now_iso()

        # Package OCPP charger state for sensor entities.
        ocpp_chargers: dict | None = None
        ocpp_sessions: list | None = None
        ocpp = getattr(self, "_ocpp_server", None)
        if ocpp is not None:
            ocpp_chargers = ocpp.charger_sessions
            ocpp_sessions = list(self._ocpp_sessions)

        data = CoordinatorData(
            cfg=self._cfg,
            live=self._live,
            hourly_recommendations=list(self._hourly_recommendations),
            hourly_recommendation=self._hourly_recommendation,
            batteries_schedules=list(self._batteries_schedules),
            batteries_schedules_remaining_capacity_needed=(
                self._batteries_schedules_remaining_capacity_needed
            ),
            current_required_battery=self._current_required_battery,
            state=state,
            last_updated=last_updated,
            next_update=self._next_update,
            plan_explanation=self._plan_explanation,
            data_quality=self._data_quality,
            ev_charging_plan=self._ev_charging_plan,
            ev_second_charging_plan=self._ev_second_charging_plan,
            override_expiry=(
                self._override_expiry.isoformat()
                if self._override_expiry is not None
                else None
            ),
            ocpp_chargers=ocpp_chargers,
            ocpp_sessions=ocpp_sessions,
            capacity_learner=getattr(self, "_capacity_learner", CapacityLearner()),
            solar_hour_factors=dict(
                getattr(self, "_solar_corrector", SolarForecastCorrector()).hour_factors
            ),
            effective_discharge_floor_pct=getattr(
                self, "_effective_discharge_floor_pct", None
            ),
            effective_discharge_floor_diag=(
                dict(getattr(self, "_effective_discharge_floor_diag", None) or {})
                if getattr(self, "_effective_discharge_floor_diag", None)
                else None
            ),
            financial_tracker=getattr(self, "_financial_tracker", None),
            prediction_tracker=getattr(self, "_prediction_tracker", None),
            savings_tracker=getattr(self, "_savings_tracker", SavingsTracker()),
        )

        if getattr(self, "_update_generation", 0) != captured_generation:
            async_log(
                "debug",
                "[coordinator] Discarding stale cycle: update generation advanced "
                "%d→%d.",
                captured_generation,
                getattr(self, "_update_generation", 0),
            )
            self._restore_accepted_plan_state(accepted_plan_state)
            return

        # Notify all subscriber entities atomically. No await occurs between the
        # generation check and publication, so a newer event cannot interleave.
        self.async_set_updated_data(data)
        if fresh_plan:
            assert planner_output_to_commit is not None
            self._last_planner_output = planner_output_to_commit
            self._last_plan_slot_start = now
            self._persist_plan_state(
                live,
                load_forecast_signature=self._current_load_forecast_signature,
            )

        # Push the accepted EV target only after the freshness gate passes.
        ocpp_server = getattr(self, "_ocpp_server", None)
        if ocpp_server is not None and self._cfg.ocpp_enabled:
            cpid = self._cfg.ocpp_cpid or "default"
            target_kw = 0.0
            if self._ev_charging_plan is not None:
                target_kw = self._ev_charging_plan.current_slot_planned_load_kwh
                slot_minutes = self._cfg.recommendation_interval_minutes
                if slot_minutes > 0 and target_kw > 0:
                    target_kw = target_kw / slot_minutes * 60.0
                if bool(
                    get_config_value(self._config_entry, "hsem_ev_force_charge_now")
                ):
                    forced_kw = float(
                        get_config_value(
                            self._config_entry,
                            "hsem_ev_planned_load_charger_power_kw",
                        )
                        or 0.0
                    )
                    if forced_kw > 0:
                        target_kw = forced_kw
            await ocpp_server.update_charge_target(cpid, target_kw, now=now)

        async_log("debug", "------ HSEM Coordinator: update cycle complete")

    async def _run_planner_phase(
        self,
        now: datetime,
        live: LiveState,
        cfg: SensorConfig,
        state: str | None,
        consumption_ok: bool,
        captured_generation: int,
    ) -> tuple[str | None, bool, PlannerOutput]:
        """Run the planner engine and apply results to recommendations.

        Handles dynamic floor computation, MILP solve (or plan reuse),
        window hysteresis, EV charger power freeze, and all post-plan
        overrides (auto-full-EV, force-charge-now, load-forecast hold).

        Returns the updated working-mode state string.
        """
        # Compute dynamic discharge floor BEFORE the planner runs.
        dynamic_floor_enabled = bool(
            get_config_value(self._config_entry, "hsem_dynamic_discharge_floor")
        )
        if dynamic_floor_enabled:
            rated_kwh = (live.huawei_batteries_rated_capacity_wh or 0.0) / 1000.0
            min_soc_pct = live.huawei_batteries_end_of_discharge_soc_pct or 0.0
            max_soc_pct = live.huawei_batteries_charging_cutoff_capacity_pct or 100.0
            _usable_kwh = usable_kwh_from_rated(rated_kwh, min_soc_pct, max_soc_pct)
            _current_kwh = (live.huawei_batteries_soc_pct or 0.0) / 100.0 * _usable_kwh
            _bridge_slots: list = []
            for rec in self._hourly_recommendations:
                _bridge_slots.append(
                    _SimpleSlot(
                        start=rec.start,
                        end=rec.end,
                        estimated_net_consumption_kwh=(
                            rec.avg_house_consumption_kwh - rec.solcast_pv_estimate_kwh
                        ),
                        batteries_charged_kwh=rec.batteries_charged_kwh,
                        recommendation=rec.recommendation,
                    )
                )
            floor_pct, floor_diag = self._dynamic_floor.compute_floor(
                now=now,
                slots=_bridge_slots,
                current_kwh=_current_kwh,
                usable_kwh=_usable_kwh,
                configured_min_soc_pct=min_soc_pct,
            )
            self._effective_discharge_floor_pct = floor_pct
            self._effective_discharge_floor_diag = floor_diag
            if live.huawei_batteries_soc_pct is not None:
                self._dynamic_floor.correct_margin(
                    live.huawei_batteries_soc_pct, floor_pct
                )
            _dynamic_floor_pct: float | None = floor_pct
        else:
            self._effective_discharge_floor_pct = None
            self._effective_discharge_floor_diag = None
            _dynamic_floor_pct = None

        # Collect session EV charge power for session-aware MILP (issue #615).
        ev_session_kw: dict[str, float] = {}
        if live.ev.is_charging and live.ev.power_w:
            ev_session_kw["ev"] = (live.ev.power_w or 0.0) / 1000.0
        if (
            cfg.ev_second_enabled
            and live.ev_second.is_charging
            and live.ev_second.power_w
        ):
            ev_session_kw["ev_second"] = (live.ev_second.power_w or 0.0) / 1000.0

        # Determine whether a full re-plan is needed.
        should_replan = self._should_replan(
            live,
            now,
            load_forecast_signature=self._current_load_forecast_signature,
        )

        if should_replan:
            planner_input = build_planner_input(
                cfg=cfg,
                live=live,
                hourly_recommendations=self._hourly_recommendations,
                batteries_schedules=self._batteries_schedules,
                previous_winner_name=self._previous_planner_winner_name,
                previous_winner_score=self._previous_planner_winner_score,
                ev_session_kw=ev_session_kw if ev_session_kw else None,
                dynamic_discharge_floor_pct=_dynamic_floor_pct,
                capacity_learner=getattr(self, "_capacity_learner", CapacityLearner()),
            )
            planner_input.solar_corrector = self._solar_corrector
            self._last_planner_input = planner_input

            total_1d = sum(
                c.avg_1d for c in planner_input.consumption_averages if c.avg_1d > 0
            )
            async_log(
                "debug",
                "[builder] consumption per-hour total reaching planner:"
                " avg_1d=%.2f kWh over %d hours",
                total_1d,
                len(planner_input.consumption_averages),
            )

            set_hsem_verbose(cfg.verbose_logging)
            planner_output = await self.hass.async_add_executor_job(
                run_planner, planner_input
            )
            if getattr(self, "_update_generation", 0) != captured_generation:
                raise _StaleUpdateCycle

            for warning in planner_output.warnings:
                async_log("debug", "[planner] %s", warning)

            self._current_required_battery = planner_output.required_capacity_kwh
            self._data_quality = replace(
                planner_output.data_quality,
                load_forecast_ready=True,
                load_forecast_reason=None,
            )
            self._ev_charging_plan = planner_output.ev_charging_plan
            self._ev_second_charging_plan = planner_output.ev_second_charging_plan

            if live.any_ev_charging:
                has_planned = any(
                    s.ev_total_planned_load_kwh > 1e-9
                    for s in planner_output.slots
                    if s.end > now
                )
                if not has_planned:
                    async_log(
                        "debug",
                        "[planner] WARNING: EV is physically charging but no "
                        "current or future slot has ev_total_planned_load_kwh"
                        " > 0.",
                    )
        else:
            assert self._last_planner_output is not None, (
                "_last_planner_output must be set when _should_replan returns False"
            )
            from copy import deepcopy

            planner_output = deepcopy(self._last_planner_output)
            self._current_required_battery = planner_output.required_capacity_kwh
            self._data_quality = replace(
                planner_output.data_quality,
                load_forecast_ready=True,
                load_forecast_reason=None,
            )
            self._ev_charging_plan = planner_output.ev_charging_plan
            self._ev_second_charging_plan = planner_output.ev_second_charging_plan
            async_log(
                "debug",
                "[replan] Skipping planner — no material changes detected."
                " Reusing plan from %s.",
                self._last_plan_slot_start.isoformat()
                if self._last_plan_slot_start
                else "(unknown)",
            )

        # Window-level hysteresis (issue #315).
        window_hys_minutes = cfg.planner_window_hysteresis_minutes
        if window_hys_minutes > 0:
            held_rec, held_start = apply_window_hysteresis(
                planner_output.slots,
                now,
                window_hysteresis_minutes=window_hys_minutes,
                previous_current_recommendation=self._window_hys_previous_rec,
                previous_current_slot_start=self._window_hys_previous_slot_start,
            )
            self._window_hys_previous_rec = held_rec
            self._window_hys_previous_slot_start = held_start
        else:
            for s in planner_output.slots:
                if as_tz(s.start, now.tzinfo) <= now < as_tz(s.end, now.tzinfo):
                    self._window_hys_previous_rec = s.recommendation
                    self._window_hys_previous_slot_start = s.start
                    break

        # Apply planner output to hourly recommendations.
        self._apply_planner_output(planner_output)

        # 8b. Auto-Full EV on negative price override (issue #609).
        auto_full_enabled = bool(
            get_config_value(self._config_entry, "hsem_ev_auto_full_negative_price")
        )
        if auto_full_enabled and live.import_electricity_price <= 0.0:
            now_slot = next(
                (
                    r
                    for r in self._hourly_recommendations
                    if as_tz(r.start, now.tzinfo) <= now < as_tz(r.end, now.tzinfo)
                ),
                None,
            )
            if now_slot is not None:
                apply_current_ev_power_override(
                    config_entry=self._config_entry,
                    hourly_recommendations=self._hourly_recommendations,
                    ev_plan=self._ev_charging_plan,
                    ev_second_plan=self._ev_second_charging_plan,
                    now=now,
                    override_primary=True,
                    override_second=False,
                    live=live,
                )

        # 8c. Force-charge-now override.
        apply_force_charge_now(
            config_entry=self._config_entry,
            hourly_recommendations=self._hourly_recommendations,
            ev_plan=self._ev_charging_plan,
            ev_second_plan=self._ev_second_charging_plan,
            now=now,
            live=live,
        )

        # 8d. Load-forecast fail-closed hold.
        if not consumption_ok or live_demand_contradicts_zero_profile(
            self._hourly_recommendations, live, now
        ):
            held = apply_load_forecast_hold(
                self._hourly_recommendations,
                live,
                now,
                consumption_ok=consumption_ok,
            )
            if held is not None:
                async_log(
                    "debug",
                    "[load] consumption_ok=%s → holding current slot in "
                    "batteries_wait_mode",
                    consumption_ok,
                )

        # 9. Find the current time-slot recommendation.
        self._hourly_recommendations.sort(key=lambda x: x.start)
        assert now.tzinfo is not None, "hsem_now() must return tz-aware datetime"
        hourly_rec = next(
            (
                r
                for r in self._hourly_recommendations
                if as_tz(r.start, now.tzinfo) <= now < as_tz(r.end, now.tzinfo)
            ),
            None,
        )

        if hourly_rec is not None:
            self._hourly_recommendation = hourly_rec
            state = hourly_rec.recommendation

        # Register forecasts in the forecast tracker.
        register_forecasts_from_planner(planner_output, self._forecast_tracker)

        # Daily plan-vs-actual accumulation.
        try:
            self._daily_plan_last_accumulated = await accumulate_daily_plan_actuals(
                now=now,
                live=live,
                output=planner_output,
                daily_tracker=self._daily_tracker,
                daily_plan_last_accumulated=self._daily_plan_last_accumulated,
                hass=self.hass,
            )
        except Exception as e:
            async_log(
                "error",
                "Daily plan-vs-actual accumulation failed: %s",
                e,
            )

        # Financial tracker accumulation (issue #599).
        try:
            await accumulate_financials(
                now=now,
                live=live,
                financial_tracker=self._financial_tracker,
                hass=self.hass,
                update_interval_minutes=cfg.update_interval,
            )
        except Exception as e:
            async_log(
                "error",
                "Financial tracker accumulation failed: %s",
                e,
            )

        # Savings tracker accumulation (issue #604).
        try:
            await accumulate_savings(
                now=now,
                live=live,
                output=planner_output,
                savings_tracker=self._savings_tracker,
                daily_tracker=self._daily_tracker,
                hourly_recommendation=self._hourly_recommendation,
                hass=self.hass,
            )
        except Exception as e:
            async_log(
                "error",
                "Savings tracker accumulation failed: %s",
                e,
            )

        return state, should_replan, planner_output

    # ------------------------------------------------------------------
    # DataUpdateCoordinator override
    # ------------------------------------------------------------------

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

    async def _set_update_interval(self, override_minutes: int | None = None) -> None:
        """Register or re-register the periodic update timer.

        Args:
            override_minutes: Force a specific interval in minutes (e.g. 1 when
                entities are missing).  When ``None`` the value from config is used.
        """
        cfg = self._cfg
        minutes = (
            override_minutes if override_minutes is not None else cfg.update_interval
        )
        interval = timedelta(minutes=minutes)
        if self._timer_interval != interval:
            self._timer_interval = interval
            await self._register_interval_timer(interval)
        self._next_update = (hsem_now() + interval).isoformat()

    async def _register_interval_timer(self, interval: timedelta) -> None:
        """Cancel any existing interval timer and register a fresh one.

        Args:
            interval: The new polling cadence.
        """
        if self._interval_timer_unsub is not None:
            self._interval_timer_unsub()
            self._interval_timer_unsub = None
        self._interval_timer_unsub = async_track_time_interval(
            self.hass,
            self._async_handle_update,  # type: ignore[arg-type]  # HA stub expects Callable[[datetime], ...]; our callback also serves as coordinator update callback
            interval,
        )
        async_log(
            "debug",
            "HSEM Coordinator: update interval set to %s",
            interval,
        )

    # ------------------------------------------------------------------
    # Planner bridge helpers
    # ------------------------------------------------------------------

    def _should_replan(
        self,
        live: LiveState,
        now: datetime,
        *,
        load_forecast_signature: LoadForecastSignature | None = None,
    ) -> bool:
        """Determine whether the planner should be re-run.

        Returns ``True`` when a material event occurred since the last plan:

        - EV connection state changed (plugged in or unplugged)
        - EV charging state changed (started or stopped)
        - EV SoC crossed the target threshold
        - Forced working mode changed
        - Crossed into a new recommendation slot
        - Import price changed significantly (new price period)

        Returns ``False`` when nothing material changed — the previous
        plan can be reused.
        """
        # First run — always plan.
        if self._last_planner_output is None:
            return True

        if getattr(self, "_load_forecast_recovery_replan_pending", False):
            async_log(
                "debug",
                "[replan] Load forecast recovered after a safety hold — re-planning.",
            )
            return True

        if load_forecast_signature is not None and not load_forecast_signatures_match(
            load_forecast_signature,
            getattr(self, "_last_plan_load_forecast_signature", None),
        ):
            async_log(
                "debug",
                "[replan] Future load forecast changed — re-planning.",
            )
            return True

        # Slot boundary crossed — new slot needs a fresh plan.
        if self._last_plan_slot_start is not None:
            slot_minutes = self._cfg.recommendation_interval_minutes
            # Compute the start of the recommendation slot containing *dt*.
            # Slots are aligned to wall-clock time (00:00, 00:15, …).
            total_minutes = now.hour * 60 + now.minute
            now_slot_idx = total_minutes // slot_minutes
            last_total = (
                self._last_plan_slot_start.hour * 60 + self._last_plan_slot_start.minute
            )
            last_slot_idx = last_total // slot_minutes
            # Also re-plan if the date changed (midnight crossing).
            if (
                now_slot_idx != last_slot_idx
                or now.date() != self._last_plan_slot_start.date()
            ):
                async_log(
                    "debug",
                    "[replan] Slot boundary crossed (last=%s, now=%s) — re-planning.",
                    self._last_plan_slot_start.isoformat(),
                    now.isoformat(),
                )
                return True

        # EV connection state changed.
        if live.ev.is_connected != self._last_plan_ev_connected:
            async_log(
                "debug",
                "[replan] EV connected state changed (%s → %s) — re-planning.",
                self._last_plan_ev_connected,
                live.ev.is_connected,
            )
            return True

        # EV charging state changed.
        if live.ev.is_charging != self._last_plan_ev_charging:
            async_log(
                "debug",
                "[replan] EV charging state changed (%s → %s) — re-planning.",
                self._last_plan_ev_charging,
                live.ev.is_charging,
            )
            return True

        # EV SoC crossed target threshold.
        ev_soc_below = (
            live.ev.soc_pct is not None
            and live.ev.soc_target_pct is not None
            and live.ev.soc_pct < live.ev.soc_target_pct
        )
        if ev_soc_below != self._last_plan_ev_soc_below_target:
            async_log(
                "debug",
                "[replan] EV SoC target threshold crossed — re-planning.",
            )
            return True

        # Second EV connection state changed.
        if live.ev_second.is_connected != self._last_plan_ev_second_connected:
            async_log(
                "debug",
                "[replan] EV2 connected state changed — re-planning.",
            )
            return True

        # Second EV charging state changed.
        if live.ev_second.is_charging != self._last_plan_ev_second_charging:
            async_log(
                "debug",
                "[replan] EV2 charging state changed — re-planning.",
            )
            return True

        # Second EV SoC crossed target threshold.
        ev2_soc_below = (
            live.ev_second.soc_pct is not None
            and live.ev_second.soc_target_pct is not None
            and live.ev_second.soc_pct < live.ev_second.soc_target_pct
        )
        if ev2_soc_below != self._last_plan_ev_second_soc_below_target:
            async_log(
                "debug",
                "[replan] EV2 SoC target threshold crossed — re-planning.",
            )
            return True

        # EV planned-load config changed (target SoC, smart charging, deadline).
        # These are live-state values that reflect the user's config choices.
        if self._last_plan_ev_target_soc is not None:
            cur_target = live.ev_planned_load_target_soc_pct or 80.0
            if abs(cur_target - self._last_plan_ev_target_soc) > 0.5:
                async_log(
                    "debug",
                    "[replan] EV target SoC changed (%.1f → %.1f) — re-planning.",
                    self._last_plan_ev_target_soc,
                    cur_target,
                )
                return True

        if self._last_plan_ev_smart_charging is not None:
            cur_smart = live.ev_planned_load_smart_charging_enabled
            if cur_smart != self._last_plan_ev_smart_charging:
                async_log(
                    "debug",
                    "[replan] EV smart charging toggled (%s → %s) — re-planning.",
                    self._last_plan_ev_smart_charging,
                    cur_smart,
                )
                return True

        if self._last_plan_ev_deadline is not None:
            cur_deadline = live.ev_planned_load_deadline
            if cur_deadline != self._last_plan_ev_deadline:
                async_log(
                    "debug",
                    "[replan] EV deadline changed — re-planning.",
                )
                return True

        if self._last_plan_ev2_target_soc is not None:
            cur_target2 = live.ev_second_planned_load_target_soc_pct or 80.0
            if abs(cur_target2 - self._last_plan_ev2_target_soc) > 0.5:
                async_log(
                    "debug",
                    "[replan] EV2 target SoC changed (%.1f → %.1f) — re-planning.",
                    self._last_plan_ev2_target_soc,
                    cur_target2,
                )
                return True

        if self._last_plan_ev2_smart_charging is not None:
            cur_smart2 = live.ev_second_planned_load_smart_charging_enabled
            if cur_smart2 != self._last_plan_ev2_smart_charging:
                async_log(
                    "debug",
                    "[replan] EV2 smart charging toggled (%s → %s) — re-planning.",
                    self._last_plan_ev2_smart_charging,
                    cur_smart2,
                )
                return True

        if self._last_plan_ev2_deadline is not None:
            cur_deadline2 = live.ev_second_planned_load_deadline
            if cur_deadline2 != self._last_plan_ev2_deadline:
                async_log(
                    "debug",
                    "[replan] EV2 deadline changed — re-planning.",
                )
                return True

        # Forced working mode changed.
        if live.force_working_mode_state != self._last_plan_force_mode:
            async_log(
                "debug",
                "[replan] Force working mode changed — re-planning.",
            )
            return True

        # Import price changed significantly (new price period).
        if self._last_plan_import_price is not None:
            price_delta = abs(
                (live.import_electricity_price or 0.0) - self._last_plan_import_price
            )
            if price_delta > 0.001:
                async_log(
                    "debug",
                    "[replan] Import price changed (%.4f → %.4f) — re-planning.",
                    self._last_plan_import_price,
                    live.import_electricity_price,
                )
                return True

        # Nothing material changed — stick to the plan.
        return False

    def _persist_plan_state(
        self,
        live: LiveState,
        *,
        load_forecast_signature: LoadForecastSignature | None = None,
    ) -> None:
        """Record the current state after a successful plan run.

        Called after every planner run so ``_should_replan`` can compare
        against the state that existed when the plan was created.
        """
        self._last_plan_ev_connected = live.ev.is_connected
        self._last_plan_ev_charging = live.ev.is_charging
        self._last_plan_ev_soc_below_target = (
            live.ev.soc_pct is not None
            and live.ev.soc_target_pct is not None
            and live.ev.soc_pct < live.ev.soc_target_pct
        )
        self._last_plan_ev_second_connected = live.ev_second.is_connected
        self._last_plan_ev_second_charging = live.ev_second.is_charging
        self._last_plan_ev_second_soc_below_target = (
            live.ev_second.soc_pct is not None
            and live.ev_second.soc_target_pct is not None
            and live.ev_second.soc_pct < live.ev_second.soc_target_pct
        )
        self._last_plan_force_mode = live.force_working_mode_state
        self._last_plan_import_price = live.import_electricity_price
        # EV planned-load config values (target SoC, smart charging, deadline).
        self._last_plan_ev_target_soc = live.ev_planned_load_target_soc_pct or 80.0
        self._last_plan_ev_smart_charging = live.ev_planned_load_smart_charging_enabled
        self._last_plan_ev_deadline = live.ev_planned_load_deadline
        self._last_plan_ev2_target_soc = (
            live.ev_second_planned_load_target_soc_pct or 80.0
        )
        self._last_plan_ev2_smart_charging = (
            live.ev_second_planned_load_smart_charging_enabled
        )
        self._last_plan_ev2_deadline = live.ev_second_planned_load_deadline
        if load_forecast_signature is not None:
            self._last_plan_load_forecast_signature = load_forecast_signature
            self._load_forecast_recovery_replan_pending = False

    def _apply_planner_output(self, output: PlannerOutput) -> None:
        """Write :class:`PlannerOutput` decisions back into the recommendation list.

        The lookup normalises both sides to UTC with ``microsecond=0`` so that
        slots remain matched even when the recommendation list was created from
        ``hsem_now()`` while the planner slots were built from timedelta
        arithmetic (always zero microseconds).  Any recommendation slot that
        cannot be matched emits a warning so the mismatch is visible in logs.

        Args:
            output: The :class:`~planner.engine.PlannerOutput` returned by the
                planner engine.
        """
        slot_by_utc = {utc_key(s.start): s for s in output.slots}

        unmatched: list[str] = []
        for rec in self._hourly_recommendations:
            slot = slot_by_utc.get(utc_key(rec.start))
            if slot is None:
                unmatched.append(rec.start.isoformat())
                continue
            rec.recommendation = slot.recommendation
            rec.batteries_charged_kwh = slot.batteries_charged_kwh
            rec.batteries_discharged_kwh = slot.batteries_discharged_kwh
            rec.estimated_net_consumption_kwh = slot.estimated_net_consumption_kwh
            rec.ev_planned_load_kwh = slot.ev_planned_load_kwh
            rec.ev_accounted_load_kwh = slot.ev_accounted_load_kwh
            rec.ev_total_planned_load_kwh = slot.ev_total_planned_load_kwh
            rec.ev_charger_calculated_power = slot.ev_charger_calculated_power
            rec.ev_second_charger_calculated_power = (
                slot.ev_second_charger_calculated_power
            )
            rec.estimated_cost_currency = slot.estimated_cost_currency
            rec.estimated_battery_capacity_kwh = slot.estimated_battery_capacity_kwh
            rec.estimated_battery_soc_pct = slot.estimated_battery_soc_pct
            rec.grid_import_kwh = slot.grid_import_kwh
            rec.grid_export_kwh = slot.grid_export_kwh
            rec.primary_battery_export_kwh = slot.primary_battery_export_kwh
            rec.pv_export_kwh = slot.pv_export_kwh
            # Copy the planner's PV estimate so that solcast_pv_estimate,
            # estimated_net_consumption, and ev_planned_load_kwh are all
            # internally consistent in the final HourlyRecommendation output.
            # The planner may have applied confidence decay or other transforms
            # that differ from the raw value stored by the data populator.
            rec.solcast_pv_estimate_kwh = slot.solcast_pv_estimate_kwh

        if unmatched:
            async_log(
                "warning",
                "[HSEM] _apply_planner_output: %d recommendation slot(s) had no "
                "matching planner output slot — planner fields (ev_planned_load_kwh, "
                "ev_accounted_load_kwh, ev_total_planned_load_kwh, recommendation, …) "
                "will remain at default 0.0 for these slots. "
                "First unmatched rec.start: %s",
                len(unmatched),
                unmatched[0],
            )

        self._batteries_schedules_remaining_capacity_needed = sum(
            s.needed_batteries_capacity for s in self._batteries_schedules if s.enabled
        )
        # Preserve the plan explanation and data quality for the next CoordinatorData snapshot.
        self._plan_explanation = output.explanation
        self._data_quality = output.data_quality

        # Persist the winning candidate name and score for hysteresis (issue #372).
        # The next planner run will compare against these values.
        if output.winner_name and output.candidates:
            winner_score = 0.0
            for c in output.candidates:
                if (
                    c.name == output.winner_name
                    and hasattr(c, "_cost")
                    and c._cost is not None
                ):
                    winner_score = c._cost.score
                    break
            self._previous_planner_winner_name = output.winner_name
            self._previous_planner_winner_score = winner_score

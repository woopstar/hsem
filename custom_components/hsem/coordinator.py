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
and stored in ``hass.data[DOMAIN][entry.entry_id]["coordinator"]``.  Each sensor
platform retrieves it and passes it to the relevant entity constructors.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from custom_components.hsem.coordinator_builder import (
    build_planner_input,
    generate_recommendation_intervals,
)
from custom_components.hsem.custom_sensors.hourly_data_populator import (  # noqa: F401 — kept for backward compat (patched in tests)
    populate_avg_house_consumption_from_snapshot,
    populate_price_and_solcast_from_snapshot,
)
from custom_components.hsem.custom_sensors.state_collector import (  # noqa: F401 — kept for backward compat
    async_collect_all_states,
    build_battery_schedules,
    build_sensor_config,
)
from custom_components.hsem.models.daily_plan_vs_actual import (
    DailyMetrics,
    DailyPlanVsActualTracker,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_inputs import PlannerInput
from custom_components.hsem.models.planner_outputs import (
    DataQuality,
    PlanExplanation,
    PlannerOutput,
)
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.charge_scheduler import apply_window_hysteresis
from custom_components.hsem.planner.ev_planner import EVChargingPlan
from custom_components.hsem.utils.datetime_utils import (
    as_tz,
    now as hsem_now,
    utc_key,
    utc_now_iso,
)
from custom_components.hsem.utils.forecast_tracker import (
    ForecastTracker,
    compute_accumulated_energy,
)
from custom_components.hsem.utils.inverter_verify import CycleApplySummary
from custom_components.hsem.utils.logger import (
    HSEM_LOGGER as _LOGGER,
    async_logger,
    set_planner_verbose,
)
from custom_components.hsem.utils.misc import get_config_value
from custom_components.hsem.utils.recommendations import Recommendations

if TYPE_CHECKING:
    from custom_components.hsem.ml.consumption_predictor import ConsumptionPredictor

# ---------------------------------------------------------------------------
# Data payload exposed to subscriber entities
# ---------------------------------------------------------------------------


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
    #: Structured data-quality report for price and PV inputs.
    data_quality: DataQuality = field(default_factory=DataQuality)
    #: EV optimal charging plan for the primary EV (None when disabled).
    ev_charging_plan: EVChargingPlan | None = None
    #: EV optimal charging plan for the second EV (None when disabled).
    ev_second_charging_plan: EVChargingPlan | None = None
    #: ISO-format timestamp of the override expiry, or None when no timed
    #: override is active (issue #317).
    override_expiry: str | None = None


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
            # interval timer below for dynamic interval support, so set a large
            # fallback here to avoid double-polling.
            update_interval=timedelta(hours=24),
        )
        self._config_entry = config_entry

        # Lock prevents concurrent executions of the update pipeline.
        self._update_lock = asyncio.Lock()

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
        self._window_hys_previous_rec: str | None = None
        self._window_hys_previous_slot_start: datetime | None = None

        # Forecast-vs-actual tracker (predicted-vs-actual tracking, issue #373).
        self._forecast_tracker: ForecastTracker = ForecastTracker(max_slots=192)
        # Daily plan-vs-actual tracker (diagnostic sensor with 90-day history).
        # The history file path is set in async_setup() once hass.config is available.
        self._daily_tracker: DailyPlanVsActualTracker = DailyPlanVsActualTracker()
        self._daily_tracker_initialized: bool = False
        # Midnight timer unsubscribe handler for daily persistence.
        self._midnight_unsub: Callable[[], None] | None = None
        # Last slot end time accumulated from planner output (prevents double-counting).
        self._daily_plan_last_accumulated: datetime | None = None
        # Timestamp of the last actual-energy accumulation cycle.
        self._last_accumulation_ts: datetime | None = None
        # Override expiry timestamp for timed manual overrides (issue #317).
        # Set by set_temporary_override when duration_minutes is provided.
        # Checked on every update cycle; when expired, the override is cleared
        # automatically and the planner resumes control.
        self._override_expiry: datetime | None = None

        # ML consumption predictor — cached across cycles so the retrain
        # gate can skip re-fitting when no new history has arrived.
        self._ml_predictor: ConsumptionPredictor | None = None

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Register timers and run the first update cycle.

        Call this once after the coordinator is created (from
        :func:`~custom_components.hsem.__init__.async_setup_entry`).
        """
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
        midnight = getattr(self, "_midnight_unsub", None)
        if midnight is not None:
            midnight()
            self._midnight_unsub = None

    async def async_options_updated(self) -> None:
        """Re-run the pipeline when the user saves new options."""
        await self._async_handle_update(None)

    # ------------------------------------------------------------------
    # Internal update pipeline
    # ------------------------------------------------------------------

    async def _async_handle_update(self, event: Event | None = None) -> None:
        """Drop concurrent updates; run the update cycle while holding the lock."""
        if self._update_lock.locked():
            await async_logger(
                self,
                "------ Coordinator update skipped: a previous cycle is still running.",
            )
            return
        async with self._update_lock:
            await self._async_run_update_cycle()

    async def _async_run_update_cycle(self) -> None:
        """Execute the full collect → populate → plan cycle.

        On success, packages the results into a :class:`CoordinatorData` and
        calls :meth:`async_set_updated_data` to notify all subscriber entities.

        Raises:
            UpdateFailed: When an unrecoverable error occurs during the pipeline.
        """
        await async_logger(self, "------ HSEM Coordinator: starting update cycle")
        now = hsem_now()

        try:
            # 1. Reload config from the config entry.
            self._cfg = build_sensor_config(self._config_entry)
            cfg = self._cfg

            # 2. Collect ALL HA entity states once into an immutable snapshot.
            #    This single call replaces the three-stage read pattern:
            #    async_collect_live_state → (populate consumption → populate price/solcast).
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

            # -----------------------------------------------------------------------
            # Override expiry check (issue #317)
            # -----------------------------------------------------------------------
            # When a timed override was set via set_temporary_override with
            # duration_minutes, check if it has expired.  If so, auto-clear the
            # select entity back to "auto" so the planner resumes control.
            #
            # Also handle the case where the user manually cleared the override
            # before the expiry — clean up the stored expiry in that case too.
            if self._override_expiry is not None:
                if now >= self._override_expiry:
                    await async_logger(
                        self,
                        "Timed override EXPIRED — clearing select entity to 'auto'.",
                    )
                    # Fire-and-forget: set the select entity back to "auto".
                    await self.hass.services.async_call(
                        "select",
                        "select_option",
                        {
                            "entity_id": live.force_working_mode,
                            "option": "auto",
                        },
                        blocking=True,
                    )
                    live.force_working_mode_state = "auto"
                    self._override_expiry = None
                elif live.force_working_mode_state == "auto":
                    # User manually cleared before expiry — remove the tracking.
                    await async_logger(
                        self,
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
            #
            # Two paths are available:
            #   a) ML prediction — queries the HA recorder for historical
            #      energy data and uses a per-(DOW, hour) time-decay model.
            #   b) Legacy averaging sensors — reads HSEM's own 1d/3d/7d/14d
            #      RestoreEntity rolling-average sensors (the default).
            #
            # The ML path is enabled via `hsem_ml_consumption_enabled`.
            # When it fails (insufficient history, misconfigured, …), the
            # coordinator transparently falls back to the legacy pipeline.
            set_planner_verbose(cfg.verbose_logging)

            if cfg.ml_consumption_enabled:
                # ML consumption prediction from recorder history.
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
                await async_logger(
                    self,
                    f"[ml] populate_ml_house_consumption returned {consumption_ok}",
                )

                if not consumption_ok:
                    # Fallback: ML failed; try legacy avg sensors.
                    await async_logger(
                        self,
                        "[ml] ML consumption failed"
                        " — falling back to legacy avg sensors.",
                    )
                    consumption_ok = populate_avg_house_consumption_from_snapshot(
                        self._hourly_recommendations,
                        self._snapshot,
                        cfg,
                        self._avg_house_consumption_entity_id_cache,
                        entry_id=self._config_entry.entry_id,
                    )
            else:
                # Legacy averaging-sensor pipeline (default).
                consumption_ok = populate_avg_house_consumption_from_snapshot(
                    self._hourly_recommendations,
                    self._snapshot,
                    cfg,
                    self._avg_house_consumption_entity_id_cache,
                    entry_id=self._config_entry.entry_id,
                )
                await async_logger(
                    self,
                    f"[avg] populate_avg_house_consumption_from_snapshot returned {consumption_ok}, "
                    f"cache has {len(self._avg_house_consumption_entity_id_cache)} entries, "
                    f"snapshot has {len(self._snapshot.energy_average_values)} energy_avg values",
                )

            # Adjust timer based on missing-entities or pending-consumption status.
            if live.missing_entities or not consumption_ok:
                await self._set_update_interval(1)
            else:
                await self._set_update_interval()

            # 6. Determine working state: forced, missing, or full pipeline.
            state: str | None = None

            if live.missing_entities and live.force_working_mode_state == "auto":
                state = Recommendations.MissingInputEntities.value
                await async_logger(
                    self, "Missing input entities, skipping calculations."
                )

            elif not consumption_ok and live.force_working_mode_state == "auto":
                # Energy average sensors not yet ready.  Still populate prices
                # and solcast below, but skip the planner (zeroed consumption
                # data would produce wrong results).
                pass  # handled below after price/solcast population

            elif live.force_working_mode_state != "auto":
                state = str(live.force_working_mode_state)
                await async_logger(
                    self,
                    f"Force working mode is activated. Setting working mode to "
                    f"{live.force_working_mode_state}",
                )

            # 7. Populate electricity prices and Solcast PV estimates — always
            #    run, independent of consumption data.
            populate_price_and_solcast_from_snapshot(
                self._hourly_recommendations,
                self._snapshot,
                cfg,
            )

            # -----------------------------------------------------------------------
            # Forecast-vs-actual accumulation (issue #373)
            # -----------------------------------------------------------------------
            # Every cycle, accumulate actual PV and load energy into the current
            # slot based on instantaneous power readings and elapsed time.
            self._accumulate_forecast_actuals(now, live)

            if (
                live.force_working_mode_state == "auto"
                and not live.missing_entities
                and consumption_ok
            ):
                # 8. Run the pure-Python planner engine — only when all data
                #    is ready.  Skip when consumption averages are still
                #    pending (first cycle, sensor restore not done).
                planner_input = build_planner_input(
                    cfg=cfg,
                    live=self._live,
                    hourly_recommendations=self._hourly_recommendations,
                    batteries_schedules=self._batteries_schedules,
                    previous_winner_name=self._previous_planner_winner_name,
                    previous_winner_score=self._previous_planner_winner_score,
                )
                # Retain for diagnostics dumps (cleared on each cycle).
                self._last_planner_input = planner_input
                # Propagate the verbose-logging flag into the pure-Python
                # planner so detailed slot-level decisions appear in the
                # standard Home Assistant log when the user enables
                # verbose logging.
                set_planner_verbose(cfg.verbose_logging)
                planner_output = run_planner(planner_input)
                self._last_planner_output = planner_output

                for warning in planner_output.warnings:
                    await async_logger(self, f"[planner] {warning}")

                self._current_required_battery = planner_output.required_capacity_kwh
                self._data_quality = planner_output.data_quality
                self._ev_charging_plan = planner_output.ev_charging_plan
                self._ev_second_charging_plan = planner_output.ev_second_charging_plan

                # Warn when an EV is physically charging but no current or future
                # slot carries ev_total_planned_load_kwh > 0.  This surfaces the
                # mismatch between live hardware state and planner intent so it is
                # visible in logs without requiring a deep dive into slot attributes.
                if self._live.any_ev_charging:
                    has_planned = any(
                        s.ev_total_planned_load_kwh > 1e-9
                        for s in planner_output.slots
                        if s.end > now
                    )
                    if not has_planned:
                        await async_logger(
                            self,
                            "[planner] WARNING: EV is physically charging but no "
                            "current or future slot has ev_total_planned_load_kwh > 0. "
                            "The EV load is either outside the planning window, "
                            "smart charging is disabled, or base_load_includes_ev is "
                            "set but the plan produced zero accounted load. "
                            "Check EV plan state and slot attributes.",
                        )

                # -----------------------------------------------------------------------
                # Window-level hysteresis — prevent rapid charge↔discharge toggles
                # near schedule-window boundaries (issue #315).
                # -----------------------------------------------------------------------
                # Apply to planner output slots BEFORE _apply_planner_output so that
                # the held recommendation propagates to hourly_recommendations.
                window_hys_minutes = cfg.planner_window_hysteresis_minutes
                if window_hys_minutes > 0:
                    held_rec, held_start = apply_window_hysteresis(
                        planner_output.slots,
                        now,
                        window_hysteresis_minutes=window_hys_minutes,
                        previous_current_recommendation=(self._window_hys_previous_rec),
                        previous_current_slot_start=(
                            self._window_hys_previous_slot_start
                        ),
                    )
                    self._window_hys_previous_rec = held_rec
                    self._window_hys_previous_slot_start = held_start
                else:
                    # Feature disabled — still persist the current recommendation
                    # so that re-enabling picks up the right state.
                    for s in planner_output.slots:
                        if as_tz(s.start, now.tzinfo) <= now < as_tz(s.end, now.tzinfo):
                            self._window_hys_previous_rec = s.recommendation
                            self._window_hys_previous_slot_start = s.start
                            break

                # Apply planner output (with hysteresis-applied slots) to
                # hourly_recommendations so the current slot resolution in
                # step 9 sees the held recommendation.
                self._apply_planner_output(planner_output)

                # 8b. Force-charge-now override: when the user toggles the
                # "EV Force Charge Now" switch, override the current slot's
                # recommendation and calculated power to charge at max speed.
                force_primary = bool(
                    get_config_value(self._config_entry, "hsem_ev_force_charge_now")
                )
                force_second = bool(
                    get_config_value(
                        self._config_entry, "hsem_ev_second_force_charge_now"
                    )
                )
                if force_primary or force_second:
                    now_slot = next(
                        (
                            r
                            for r in self._hourly_recommendations
                            if as_tz(r.start, now.tzinfo)
                            <= now
                            < as_tz(r.end, now.tzinfo)
                        ),
                        None,
                    )
                    if now_slot is not None:
                        # Max AC power = charger_power_kw * 1000
                        if force_primary:
                            now_slot.recommendation = (
                                Recommendations.EVSmartCharging.value
                            )
                            pwr_kw = float(
                                get_config_value(
                                    self._config_entry,
                                    "hsem_ev_planned_load_charger_power_kw",
                                )
                                or 0.0
                            )
                            now_slot.ev_charger_calculated_power = (
                                round(pwr_kw * 1000) if pwr_kw > 0 else 0.0
                            )
                        if force_second:
                            now_slot.recommendation = (
                                Recommendations.EVSmartCharging.value
                            )
                            pwr_kw = float(
                                get_config_value(
                                    self._config_entry,
                                    "hsem_ev_second_planned_load_charger_power_kw",
                                )
                                or 0.0
                            )
                            now_slot.ev_second_charger_calculated_power = (
                                round(pwr_kw * 1000) if pwr_kw > 0 else 0.0
                            )

                # 9. Find the current time-slot recommendation.
                self._hourly_recommendations.sort(key=lambda x: x.start)
                # now.tzinfo is guaranteed non-None because hsem_now() returns
                # a timezone-aware datetime; assert so pyright narrows the type.
                assert now.tzinfo is not None, (
                    "hsem_now() must return tz-aware datetime"
                )
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

                # -----------------------------------------------------------------------
                # Register forecasts in the forecast tracker from the planner output.
                # -----------------------------------------------------------------------
                self._register_forecasts_from_planner(planner_output)

                # -----------------------------------------------------------------------
                # Daily plan-vs-actual accumulation from planner output.
                # -----------------------------------------------------------------------
                try:
                    self._accumulate_daily_plan_actuals(now, live, planner_output)
                except Exception:
                    _LOGGER.exception(
                        "Daily plan-vs-actual accumulation failed — "
                        "continuing without updating daily metrics."
                    )

        except Exception as exc:
            raise UpdateFailed(f"HSEM update cycle failed: {exc}") from exc

        # Final sort and timestamp.
        self._hourly_recommendations.sort(key=lambda x: x.start)
        last_updated = utc_now_iso()

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
        )

        # Notify all subscriber entities atomically.
        self.async_set_updated_data(data)
        await async_logger(self, "------ HSEM Coordinator: update cycle complete")

    # ------------------------------------------------------------------
    # DataUpdateCoordinator override
    # ------------------------------------------------------------------

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
        await async_logger(
            self,
            f"HSEM Coordinator: update interval set to {interval}",
        )

    # ------------------------------------------------------------------
    # Planner bridge helpers
    # ------------------------------------------------------------------

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
            # Copy the planner's PV estimate so that solcast_pv_estimate,
            # estimated_net_consumption, and ev_planned_load_kwh are all
            # internally consistent in the final HourlyRecommendation output.
            # The planner may have applied confidence decay or other transforms
            # that differ from the raw value stored by the data populator.
            rec.solcast_pv_estimate_kwh = slot.solcast_pv_estimate_kwh

        if unmatched:
            _LOGGER.warning(
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

    # ------------------------------------------------------------------
    # Forecast-vs-actual tracking (issue #373)
    # ------------------------------------------------------------------

    def _accumulate_forecast_actuals(self, now: datetime, live: LiveState) -> None:
        """Accumulate actual PV and load energy into the current slot.

        Called every coordinator cycle to accumulate energy from instantaneous
        power readings.  Uses the elapsed time since the last accumulation to
        convert power (W) to energy (kWh).

        Args:
            now: Current time (timezone-aware).
            live: The live HA entity state snapshot.
        """
        # Compute elapsed seconds since last accumulation.
        if self._last_accumulation_ts is not None:
            elapsed = (now - self._last_accumulation_ts).total_seconds()
        else:
            elapsed = 0.0

        self._last_accumulation_ts = now

        if elapsed <= 0:
            return

        # Find the current slot's record.
        if not self._hourly_recommendations:
            return

        # Find the slot whose time range contains 'now'.
        current_slot = None
        for rec in self._hourly_recommendations:
            if as_tz(rec.start, now.tzinfo) <= now < as_tz(rec.end, now.tzinfo):
                current_slot = rec
                break

        if current_slot is None:
            return

        # Get or create the tracker record for this slot.
        tracker_rec = self._forecast_tracker.get_or_create_record(
            current_slot.start, current_slot.end
        )

        # Accumulate PV energy.
        pv_power_w = live.solar_production_power_w
        pv_energy = compute_accumulated_energy(pv_power_w, elapsed)
        tracker_rec.accumulate_pv(pv_energy)

        # Accumulate load energy.
        load_power_w = live.house_consumption_power_w
        load_energy = compute_accumulated_energy(load_power_w, elapsed)
        tracker_rec.accumulate_load(load_energy)

        # Finalise any slots whose end time has passed.
        self._forecast_tracker.finalise_past_records(now)

    def _register_forecasts_from_planner(self, output: PlannerOutput) -> None:
        """Register PV and load forecasts from planner output into the tracker.

        This is called after the planner runs successfully.  Forecast values
        are only set if the tracker record exists and is not yet finalised.

        Args:
            output: The :class:`~planner.engine.PlannerOutput` returned by the
                planner engine.
        """
        for slot in output.slots:
            pv_forecast = getattr(slot, "solcast_pv_estimate_kwh", 0.0)
            load_forecast = getattr(slot, "avg_house_consumption_kwh", 0.0)

            self._forecast_tracker.set_forecasts(
                start=slot.start,
                pv_kwh=pv_forecast,
                load_kwh=load_forecast,
            )

    # ------------------------------------------------------------------
    # Daily plan-vs-actual accumulation (issue #540)
    # ------------------------------------------------------------------

    def _accumulate_daily_plan_actuals(
        self,
        now: datetime,
        live: LiveState,
        output: PlannerOutput,
    ) -> None:
        """Accumulate plan and actual values into the daily tracker.

        Plan side: sum planned import/export/cycle/PV from planner slots
        whose end time has passed.

        Actual side: use cumulative energy meter readings from live state,
        falling back to SoC-based cycle tracking when meters are unavailable.

        Args:
            now: Current datetime (timezone-aware).
            live: Live HA entity state snapshot.
            output: Planner output with slot-level decisions.
        """
        self._init_daily_tracker()
        tracker = self._daily_tracker

        # Check and handle day rollover first.
        tracker.check_day_rollover(now)

        # ---- Plan accumulation ----
        # Accumulate plan values for the current in-progress slot (and any
        # completed slots that may have been missed).  The current slot's
        # plan values are captured before the SoC simulation zeroes them
        # on the next planner run.
        self._daily_plan_last_accumulated = _accumulate_plan_for_slots(
            tracker,
            output.slots,
            now,
            self._daily_plan_last_accumulated,
        )

        # ---- Actual accumulation ----
        # Use cumulative energy meter readings when available.
        # Battery cycle tracking uses SoC delta converted to kWh via rated capacity.
        soc_pct = live.huawei_batteries_soc_pct
        rated_cap_kwh = (live.huawei_batteries_rated_capacity_wh or 0.0) / 1000.0
        tracker.accumulate_actual(
            grid_import_energy_kwh=live.grid_import_energy_kwh,
            grid_export_energy_kwh=live.grid_export_energy_kwh,
            pv_energy_kwh=live.pv_energy_kwh,
            soc_pct=soc_pct,
            rated_capacity_kwh=rated_cap_kwh,
            import_price=live.import_electricity_price,
            export_price=live.export_electricity_price,
        )

    def _init_daily_tracker(self) -> None:
        """Lazily initialise the daily plan-vs-actual tracker.

        Called once on the first access.  Registers the midnight timer
        and loads the history file.  Failures are logged and leave the
        tracker with an empty history file path so the sensor shows
        'no data' rather than crashing the coordinator.
        """
        if getattr(self, "_daily_tracker_initialized", True):
            return

        try:
            config_dir = self.hass.config.config_dir
            self._daily_tracker.history_file = str(
                Path(config_dir) / "hsem_daily_history.json"
            )
            self._daily_tracker.load_history()

            self._midnight_unsub = async_track_time_change(
                self.hass,
                self._async_handle_midnight,
                hour=0,
                minute=0,
                second=0,
            )
            self._daily_tracker_initialized = True
        except Exception:
            _LOGGER.exception(
                "Failed to initialise daily tracker (plan-vs-actual "
                "sensor will be unavailable)"
            )
            self._daily_tracker_initialized = True  # don't retry

    async def _async_handle_midnight(self, _now: datetime) -> None:
        """Handle the midnight timer — persist the day's record and reset.

        This is called by the HA time-change listener at 00:00:00 local time.
        Saves yesterday's record, resets accumulators, and updates today's date
        so the next update cycle does not double-save.

        Args:
            _now: The datetime at which the timer fired (unused).
        """
        tracker = self._daily_tracker
        if tracker.history_file:
            today_record = tracker._build_today_record()
            saved = tracker._save_record_to_history(today_record)
            if saved:
                _LOGGER.info(
                    "Daily plan-vs-actual record saved for %s",
                    tracker.today,
                )
            else:
                _LOGGER.warning(
                    "Failed to save daily plan-vs-actual record for %s",
                    tracker.today,
                )

            # Reset accumulators for the new day so check_day_rollover()
            # does not double-save on the next cycle.
            tracker.today = _now.date().isoformat()
            tracker.actual = DailyMetrics()
            tracker.plan = DailyMetrics()
            tracker.last_soc_pct = None
            tracker._last_import_energy_kwh = None
            tracker._last_export_energy_kwh = None
            tracker._last_pv_energy_kwh = None
            self._daily_plan_last_accumulated = None


# ---------------------------------------------------------------------------
# Module-level helpers for daily plan-vs-actual accumulation
# ---------------------------------------------------------------------------


def _accumulate_plan_for_slots(
    tracker: DailyPlanVsActualTracker,
    slots: list,
    now: datetime,
    last_accumulated: datetime | None,
) -> datetime | None:
    """Accumulate plan values for the current in-progress slot.

    Accumulates the FULL plan value for each slot exactly once, on the
    first cycle where the slot is the current in-progress slot
    (``start <= now < end``).  This captures the plan as it was when
    the slot started, before the SoC simulation zeroes the plan fields
    for past slots on subsequent planner runs.

    Completed past slots are also handled as a safety net for slots
    that may become past between cycles (e.g. after a coordinator
    restart).

    Returns:
        The accumulation marker (start of the current slot if it was
        just accumulated, or the last_accumulated value unchanged).
    """
    for slot in slots:
        slot_start = as_tz(slot.start, now.tzinfo) if hasattr(slot, "start") else None
        slot_end = as_tz(slot.end, now.tzinfo) if hasattr(slot, "end") else None

        # Current in-progress slot: accumulate full plan on first encounter.
        if (
            slot_start is not None
            and slot_end is not None
            and slot_start <= now < slot_end
        ):
            if last_accumulated is None or last_accumulated < slot_start:
                _add_slot_to_tracker(tracker, slot, fraction=1.0)
                return slot_start  # Mark this slot as accumulated
            return last_accumulated  # Already accumulated this slot

        # Safety net: completed past slots that may not have been
        # accumulated yet.  Only active after the first cycle (when
        # last_accumulated is not None) to avoid inflating plan values
        # with stale zeroed fields from past slots on startup.
        if last_accumulated is not None and slot_end is not None and slot_end <= now:
            # Use slot_start in the skip-check because last_accumulated
            # is now a slot-start marker (set by the current-slot branch).
            if slot_start is not None and slot_start <= last_accumulated:
                continue
            _add_slot_to_tracker(tracker, slot, fraction=1.0)

    # If no current slot was found, return the end of the last completed
    # slot as the marker (prevents re-accumulation of past slots).
    return _last_completed_slot_end(slots, now) or last_accumulated


def _add_slot_to_tracker(
    tracker: DailyPlanVsActualTracker,
    slot: object,
    fraction: float = 1.0,
) -> None:
    """Add a single slot's plan values to the tracker, scaled by *fraction*."""
    gi = (getattr(slot, "grid_import_kwh", 0.0) or 0.0) * fraction
    ge = (getattr(slot, "grid_export_kwh", 0.0) or 0.0) * fraction
    chg = (getattr(slot, "batteries_charged_kwh", 0.0) or 0.0) * fraction
    dis = (getattr(slot, "batteries_discharged_kwh", 0.0) or 0.0) * fraction
    pv = (getattr(slot, "solcast_pv_estimate_kwh", 0.0) or 0.0) * fraction
    slot_price = getattr(slot, "price", None)
    import_price = slot_price.import_price if slot_price is not None else 0.0
    export_price = slot_price.export_price if slot_price is not None else 0.0
    cycle_kwh = abs(chg) + abs(dis)
    tracker.accumulate_plan(
        grid_import_kwh=gi,
        grid_export_kwh=ge,
        cycle_kwh=cycle_kwh,
        pv_kwh=pv,
        import_price=import_price,
        export_price=export_price,
    )


def _last_completed_slot_end(slots: list, now: datetime) -> datetime | None:
    """Return the end time of the most recent completed slot, or None."""
    last_end: datetime | None = None
    for slot in slots:
        slot_end = as_tz(slot.end, now.tzinfo) if hasattr(slot, "end") else None
        if slot_end is not None and slot_end <= now:
            if last_end is None or slot_end > last_end:
                last_end = slot_end
    return last_end

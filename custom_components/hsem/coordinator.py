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
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_inputs import PlannerInput
from custom_components.hsem.models.planner_outputs import DataQuality, PlanExplanation
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.charge_scheduler import apply_window_hysteresis
from custom_components.hsem.planner.ev_planner import EVChargingPlan
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.datetime_utils import now as hsem_now
from custom_components.hsem.utils.datetime_utils import utc_key, utc_now_iso
from custom_components.hsem.utils.forecast_tracker import (
    ForecastTracker,
    compute_accumulated_energy,
)
from custom_components.hsem.utils.inverter_verify import CycleApplySummary
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.logger import async_logger, set_planner_verbose
from custom_components.hsem.utils.recommendations import Recommendations

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
        self._interval_timer_unsub = None
        self._hourly_timer_unsub = None
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
        self._last_planner_output = None
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
        # Timestamp of the last actual-energy accumulation cycle.
        self._last_accumulation_ts: datetime | None = None
        # Override expiry timestamp for timed manual overrides (issue #317).
        # Set by set_temporary_override when duration_minutes is provided.
        # Checked on every update cycle; when expired, the override is cleared
        # automatically and the planner resumes control.
        self._override_expiry: datetime | None = None

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
            self._async_handle_update,
            hour="*",
            minute=0,
            second=10,
        )

    async def async_teardown(self) -> None:
        """Cancel all registered timers and state-change listeners.

        Called from :func:`~custom_components.hsem.__init__.async_unload_entry`.
        """
        if self._hourly_timer_unsub is not None:
            self._hourly_timer_unsub()
            self._hourly_timer_unsub = None
        if self._interval_timer_unsub is not None:
            self._interval_timer_unsub()
            self._interval_timer_unsub = None
        for unsub in self._listener_unsubs:
            unsub()
        self._listener_unsubs.clear()

    async def async_options_updated(self) -> None:
        """Re-run the pipeline when the user saves new options."""
        await self._async_handle_update(None)

    # ------------------------------------------------------------------
    # Internal update pipeline
    # ------------------------------------------------------------------

    async def _async_handle_update(self, event=None) -> None:
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
        await async_logger(self, "------ HSEM Coordinator: starting update cycle...")
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

            # 5. Populate weighted house-consumption averages from snapshot
            #    (no HA state lookups — data was pre-collected in step 2).
            #    The energy average sensors are HSEM's own RestoreEntity sensors.
            #    On the very first cycle they may report "unknown" before the
            #    restore completes.  When the populator fails, skip the planner
            #    and retry on the next cycle with a shorter interval.
            set_planner_verbose(cfg.verbose_logging)
            consumption_ok = populate_avg_house_consumption_from_snapshot(
                self._hourly_recommendations,
                self._snapshot,
                cfg,
                self._avg_house_consumption_entity_id_cache,
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
                now.tzinfo,
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
        await async_logger(self, "------ HSEM Coordinator: update cycle complete.")

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
            self.hass, self._async_handle_update, interval
        )
        await async_logger(
            self, f"HSEM Coordinator: update interval set to {interval}."
        )

    # ------------------------------------------------------------------
    # Planner bridge helpers
    # ------------------------------------------------------------------

    def _apply_planner_output(self, output) -> None:
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

    def _register_forecasts_from_planner(self, output) -> None:
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

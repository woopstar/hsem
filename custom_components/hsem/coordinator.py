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
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from custom_components.hsem.custom_sensors.hourly_data_populator import (
    async_populate_avg_house_consumption,
    async_populate_price_and_solcast,
)
from custom_components.hsem.custom_sensors.state_collector import (
    async_collect_live_state,
    build_battery_schedules,
    build_sensor_config,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_inputs import (
    BatteryScheduleInput,
    HourlyConsumptionAverage,
    PlannerInput,
    PricePoint,
    SolcastSlot,
)
from custom_components.hsem.models.planner_outputs import DataQuality, PlanExplanation
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.ev_planner import EVChargingPlan
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.datetime_utils import now as hsem_now
from custom_components.hsem.utils.datetime_utils import utc_now_iso
from custom_components.hsem.utils.inverter_verify import CycleApplySummary
from custom_components.hsem.utils.logger import async_logger, set_planner_verbose
from custom_components.hsem.utils.misc import convert_to_float, convert_to_int
from custom_components.hsem.utils.recommendations import Recommendations

_LOGGER = logging.getLogger(__name__)


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

            # 2. Collect live HA entity states.
            (
                self._live,
                self._force_working_mode_entity,
                new_unsubs,
            ) = await async_collect_live_state(
                self,
                cfg,
                self._force_working_mode_entity,
                self._tracked_entities,
            )
            self._listener_unsubs.extend(new_unsubs)
            live = self._live

            # 3. Reset and generate recommendation time-slots.
            self._hourly_recommendation = None
            self._hourly_recommendations = self._generate_recommendation_intervals(
                cfg.recommendation_interval_minutes,
                cfg.recommendation_interval_length,
            )

            # 4. Build battery-schedule objects from config.
            self._batteries_schedules = build_battery_schedules(cfg)
            self._batteries_schedules.sort(key=lambda x: x.start)

            # 5. Populate weighted house-consumption averages.
            if not await async_populate_avg_house_consumption(
                self,
                self._hourly_recommendations,
                cfg,
                self._avg_house_consumption_entity_id_cache,
            ):
                live.missing_entities = True

            # Adjust timer based on missing-entities status.
            if live.missing_entities:
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

            elif live.force_working_mode_state != "auto":
                state = str(live.force_working_mode_state)
                await async_logger(
                    self,
                    f"Force working mode is activated. Setting working mode to "
                    f"{live.force_working_mode_state}",
                )

            else:
                # 7. Populate electricity prices and Solcast PV estimates.
                await async_populate_price_and_solcast(
                    self, self._hourly_recommendations, cfg, now.tzinfo
                )

                # 8. Run the pure-Python planner engine.
                planner_input = self._build_planner_input()
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

                self._apply_planner_output(planner_output)
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

    def _build_planner_input(self) -> PlannerInput:
        """Assemble a :class:`PlannerInput` from the current pipeline state.

        Returns:
            A fully populated :class:`PlannerInput` ready for the planner engine.
        """
        cfg = self._cfg
        now = hsem_now()

        # self._live is always populated by async_collect_live_state before
        # this method is called; assert to narrow the type for static analysis.
        if self._live is None:
            raise RuntimeError(
                "_build_planner_input called before live state was collected"
            )
        live = self._live

        # Dedup key is (day_offset, hour) so that tomorrow's slots are kept
        # separately from today's even when they share the same wall-clock hour.
        seen_day_hours: set[tuple[int, int]] = set()
        consumption_averages: list[HourlyConsumptionAverage] = []
        price_points: list[PricePoint] = []
        solcast_slots: list[SolcastSlot] = []

        # Number of recommendation slots that fit inside one wall-clock hour.
        # Used to up-scale per-slot energy values back to hourly totals before
        # passing them to the planner engine (which works in Wh per hour).
        slots_per_hour = 60.0 / cfg.recommendation_interval_minutes

        # ---------------------------------------------------------------------------
        # Price interval semantics — see also hourly_data_populator.py
        # ---------------------------------------------------------------------------
        # `eds_share` is the ratio of the EDS update interval to the slot width:
        #
        #   eds_share = energi_data_service_update_interval / recommendation_interval_minutes
        #
        # In `hourly_data_populator._async_update_hourly_field`, each EDS price was
        # divided by `eds_share` before storing into the per-slot recommendation
        # object.  Here we multiply it back to recover the original price rate
        # (currency/kWh) before handing it to the planner.
        #
        # This forward-and-back pair is intentional: the divide converts the price
        # to a per-slot representation suitable for matching slot boundaries, while
        # the multiply here converts it back to the original hourly rate required by
        # the planner's cost function.
        #
        # Common configurations:
        #   EDS 60 min / slots 15 min  →  eds_share = 4.0
        #   EDS 15 min / slots 15 min  →  eds_share = 1.0  (no-op)
        #   EDS 60 min / slots 60 min  →  eds_share = 1.0  (no-op)
        eds_share = (
            cfg.energi_data_service_update_interval
            / cfg.recommendation_interval_minutes
        )

        # Midnight of the planning day — used to compute per-slot day_offset.
        planning_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        for rec in self._hourly_recommendations:
            h = rec.start.hour
            # Compute day_offset: number of whole calendar days between
            # planning midnight and this slot's date.  This preserves the
            # distinction between today's hour-3 and tomorrow's hour-3 for
            # multi-day planning horizons (e.g. 48 h or 72 h).
            day_offset = (rec.start.date() - planning_midnight.date()).days
            day_hour_key = (day_offset, h)
            if day_hour_key in seen_day_hours:
                continue
            seen_day_hours.add(day_hour_key)

            consumption_averages.append(
                HourlyConsumptionAverage(
                    hour=h,
                    avg_1d=round(rec.avg_house_consumption_1d_kwh * slots_per_hour, 3),
                    avg_3d=round(rec.avg_house_consumption_3d_kwh * slots_per_hour, 3),
                    avg_7d=round(rec.avg_house_consumption_7d_kwh * slots_per_hour, 3),
                    avg_14d=round(rec.avg_house_consumption_14d_kwh * slots_per_hour, 3),
                    day_offset=day_offset,
                )
            )
            # Multiply by eds_share to reverse the per-slot divide applied during
            # population; the planner receives the original currency/kWh rate.
            price_points.append(
                PricePoint(
                    hour=h,
                    import_price=round(rec.import_price * eds_share, 5),
                    export_price=round(rec.export_price * eds_share, 5),
                    day_offset=day_offset,
                )
            )
            solcast_slots.append(
                SolcastSlot(
                    hour=h,
                    pv_estimate=round(rec.solcast_pv_estimate_kwh * slots_per_hour, 3),
                    day_offset=day_offset,
                )
            )

        battery_schedules = [
            BatteryScheduleInput(
                enabled=s.enabled,
                start=s.start,
                end=s.end,
            )
            for s in self._batteries_schedules
        ]

        return PlannerInput(
            now_iso=now.isoformat(),
            interval_minutes=cfg.recommendation_interval_minutes,
            interval_length_hours=cfg.recommendation_interval_length,
            battery_soc_pct=convert_to_float(live.huawei_batteries_soc_pct) or 50.0,
            battery_rated_capacity_kwh=(
                convert_to_float(live.huawei_batteries_rated_capacity_wh) or 0.0
            )
            / 1000.0,
            battery_end_of_discharge_soc_pct=convert_to_float(
                live.huawei_batteries_end_of_discharge_soc_pct or 5.0
            )
            or 5.0,
            battery_max_soc_pct=convert_to_float(
                live.huawei_batteries_charging_cutoff_capacity_pct
            )
            or 100.0,
            battery_max_charge_power_w=convert_to_float(
                live.huawei_batteries_max_charge_power_w
            )
            or 5000.0,
            battery_max_discharge_power_w=convert_to_float(
                live.huawei_batteries_max_discharge_power_w
            )
            or None,
            battery_charge_efficiency_pct=convert_to_float(
                cfg.batteries_charge_efficiency
            )
            or 95.0,
            battery_discharge_efficiency_pct=convert_to_float(
                cfg.batteries_discharge_efficiency
            )
            or 95.0,
            battery_purchase_price=convert_to_float(cfg.batteries_purchase_price)
            or 0.0,
            battery_expected_cycles=(
                v
                if (v := convert_to_int(cfg.batteries_expected_cycles)) is not None
                else 6000
            ),
            battery_cycle_cost_per_kwh=convert_to_float(cfg.batteries_cycle_cost)
            or 0.0,
            weight_1d=(
                v
                if (v := convert_to_int(cfg.house_consumption_energy_weight_1d))
                is not None
                else 25
            ),
            weight_3d=(
                v
                if (v := convert_to_int(cfg.house_consumption_energy_weight_3d))
                is not None
                else 30
            ),
            weight_7d=(
                v
                if (v := convert_to_int(cfg.house_consumption_energy_weight_7d))
                is not None
                else 30
            ),
            weight_14d=(
                v
                if (v := convert_to_int(cfg.house_consumption_energy_weight_14d))
                is not None
                else 15
            ),
            consumption_averages=consumption_averages,
            price_points=price_points,
            solcast_slots=solcast_slots,
            battery_schedules=battery_schedules,
            excess_export_enabled=bool(cfg.batteries_enable_excess_export),
            excess_export_discharge_buffer_pct=convert_to_float(
                cfg.batteries_excess_export_discharge_buffer
            )
            or 10.0,
            excess_export_price_threshold=convert_to_float(
                cfg.batteries_excess_export_price_threshold
            )
            or 0.10,
            export_min_price=convert_to_float(cfg.energi_data_service_export_min_price)
            or 0.0,
            months_winter=list(cfg.months_winter or []),
            house_power_includes_ev=bool(cfg.house_power_includes_ev_charger_power),
            is_read_only=bool(cfg.read_only),
            # EV planned load
            ev_planned_load_enabled=bool(cfg.ev_planned_load_enabled),
            ev_planned_load_connected=bool(live.ev_planned_load_connected),
            ev_planned_load_smart_charging_enabled=bool(
                live.ev_planned_load_smart_charging_enabled
            ),
            ev_planned_load_current_soc_pct=convert_to_float(
                live.ev_planned_load_current_soc_pct
            )
            or 0.0,
            ev_planned_load_target_soc_pct=convert_to_float(
                live.ev_planned_load_target_soc_pct
            )
            or cfg.ev_planned_load_target_soc_fixed,
            ev_planned_load_battery_capacity_kwh=convert_to_float(
                cfg.ev_planned_load_battery_capacity_kwh
            )
            or 0.0,
            ev_planned_load_charger_power_kw=convert_to_float(
                cfg.ev_planned_load_charger_power_kw
            )
            or 0.0,
            ev_planned_load_charger_efficiency_pct=convert_to_float(
                cfg.ev_planned_load_charger_efficiency_pct
            )
            or 100.0,
            ev_planned_load_deadline=live.ev_planned_load_deadline,
            ev_planned_load_base_load_includes_ev=bool(
                cfg.ev_planned_load_base_load_includes_ev
            ),
            # Second EV planned load
            ev_second_planned_load_enabled=bool(cfg.ev_second_planned_load_enabled),
            ev_second_planned_load_connected=bool(
                live.ev_second_planned_load_connected
            ),
            ev_second_planned_load_smart_charging_enabled=bool(
                live.ev_second_planned_load_smart_charging_enabled
            ),
            ev_second_planned_load_current_soc_pct=convert_to_float(
                live.ev_second_planned_load_current_soc_pct
            )
            or 0.0,
            ev_second_planned_load_target_soc_pct=convert_to_float(
                live.ev_second_planned_load_target_soc_pct
            )
            or cfg.ev_second_planned_load_target_soc_fixed,
            ev_second_planned_load_battery_capacity_kwh=convert_to_float(
                cfg.ev_second_planned_load_battery_capacity_kwh
            )
            or 0.0,
            ev_second_planned_load_charger_power_kw=convert_to_float(
                cfg.ev_second_planned_load_charger_power_kw
            )
            or 0.0,
            ev_second_planned_load_charger_efficiency_pct=convert_to_float(
                cfg.ev_second_planned_load_charger_efficiency_pct
            )
            or 100.0,
            ev_second_planned_load_deadline=live.ev_second_planned_load_deadline,
            ev_second_planned_load_base_load_includes_ev=bool(
                cfg.ev_second_planned_load_base_load_includes_ev
            ),
            time_discount_rate=0.995,
        )

    @staticmethod
    def _utc_key(dt) -> object:
        """Normalise a timezone-aware datetime to a UTC key for slot matching.

        Two datetimes that represent the **same instant** but carry different
        ``tzinfo`` objects (e.g. ``ZoneInfo('Europe/Copenhagen')`` vs a fixed
        ``+02:00`` offset) hash and compare as equal in Python.  However,
        sub-second fields can differ when the recommendation slot was created
        from ``hsem_now()`` (which already strips microseconds) while the
        planner slot was built from ``timedelta`` arithmetic anchored at
        midnight (microseconds always zero).  Stripping microseconds on both
        sides guarantees a deterministic match regardless of when each was
        created.

        Args:
            dt: A timezone-aware :class:`datetime.datetime`.

        Returns:
            A ``datetime`` normalised to UTC with ``microsecond=0`` that can be
            used as a dictionary key for slot matching.
        """
        from datetime import UTC

        return dt.astimezone(UTC).replace(microsecond=0)

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
        slot_by_utc = {self._utc_key(s.start): s for s in output.slots}

        unmatched: list[str] = []
        for rec in self._hourly_recommendations:
            slot = slot_by_utc.get(self._utc_key(rec.start))
            if slot is None:
                unmatched.append(rec.start.isoformat())
                continue
            rec.recommendation = slot.recommendation
            rec.batteries_charged_kwh = slot.batteries_charged
            rec.batteries_discharged_kwh = slot.batteries_discharged
            rec.estimated_net_consumption_kwh = slot.estimated_net_consumption_kwh
            rec.ev_planned_load_kwh = slot.ev_planned_load_kwh
            rec.ev_accounted_load_kwh = slot.ev_accounted_load_kwh
            rec.ev_total_planned_load_kwh = slot.ev_total_planned_load_kwh
            rec.estimated_cost_currency = slot.estimated_cost
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _generate_recommendation_intervals(
        self, interval_minutes: int, total_hours: int
    ) -> list[HourlyRecommendation]:
        """Generate empty recommendation slots from midnight for ``total_hours`` hours.

        Args:
            interval_minutes: Width of each slot in minutes.
            total_hours: Planning horizon in hours.

        Returns:
            A list of :class:`HourlyRecommendation` objects with all numeric
            fields initialised to ``0.0``.
        """
        now = hsem_now()
        start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        steps = int((total_hours * 60) / interval_minutes)

        intervals = []
        for i in range(steps):
            t_start = start_time + timedelta(minutes=i * interval_minutes)
            t_end = t_start + timedelta(minutes=interval_minutes)
            intervals.append(
                HourlyRecommendation(
                    avg_house_consumption_kwh=0.0,
                    avg_house_consumption_1d_kwh=0.0,
                    avg_house_consumption_3d_kwh=0.0,
                    avg_house_consumption_7d_kwh=0.0,
                    avg_house_consumption_14d_kwh=0.0,
                    batteries_charged=0.0,
                    batteries_discharged=0.0,
                    end=t_end,
                    estimated_battery_capacity=0.0,
                    estimated_battery_soc=0,
                    estimated_cost=0.0,
                    estimated_net_consumption=0.0,
                    ev_planned_load_kwh=0.0,
                    export_price=0.0,
                    grid_export_kwh=0.0,
                    grid_import_kwh=0.0,
                    import_price=0.0,
                    recommendation=None,
                    solcast_pv_estimate=0.0,
                    start=t_start,
                )
            )
        return intervals

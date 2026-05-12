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

import homeassistant.util.dt as dt_util
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
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner import run_planner
from custom_components.hsem.utils.logger import async_logger
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
        self._avg_house_consumption_entity_id_cache: dict[str, str] = {}

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
        """Cancel all registered timers.

        Called from :func:`~custom_components.hsem.__init__.async_unload_entry`.
        """
        if self._hourly_timer_unsub is not None:
            self._hourly_timer_unsub()
            self._hourly_timer_unsub = None
        if self._interval_timer_unsub is not None:
            self._interval_timer_unsub()
            self._interval_timer_unsub = None

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
        now = dt_util.now()

        try:
            # 1. Reload config from the config entry.
            self._cfg = build_sensor_config(self._config_entry)
            cfg = self._cfg

            # 2. Collect live HA entity states.
            (
                self._live,
                self._force_working_mode_entity,
            ) = await async_collect_live_state(
                self,
                cfg,
                self._force_working_mode_entity,
                self._tracked_entities,
            )
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
                planner_output = run_planner(planner_input)

                for warning in planner_output.warnings:
                    await async_logger(self, f"[planner] {warning}")

                self._apply_planner_output(planner_output)
                self._current_required_battery = planner_output.required_capacity_kwh

                # 9. Find the current time-slot recommendation.
                self._hourly_recommendations.sort(key=lambda x: x.start)
                tz = now.tzinfo
                hourly_rec = next(
                    (
                        r
                        for r in self._hourly_recommendations
                        if r.start.astimezone(tz) <= now < r.end.astimezone(tz)
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
        last_updated = now.isoformat()

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
        self._next_update = (dt_util.now() + interval).isoformat()

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
        live = self._live
        now = dt_util.now()

        seen_hours: set[int] = set()
        consumption_averages: list[HourlyConsumptionAverage] = []
        price_points: list[PricePoint] = []
        solcast_slots: list[SolcastSlot] = []

        slots_per_hour = 60.0 / cfg.recommendation_interval_minutes
        eds_share = (
            cfg.energi_data_service_update_interval
            / cfg.recommendation_interval_minutes
        )

        for rec in self._hourly_recommendations:
            h = rec.start.hour
            if h in seen_hours:
                continue
            seen_hours.add(h)

            consumption_averages.append(
                HourlyConsumptionAverage(
                    hour=h,
                    avg_1d=round(rec.avg_house_consumption_1d * slots_per_hour, 3),
                    avg_3d=round(rec.avg_house_consumption_3d * slots_per_hour, 3),
                    avg_7d=round(rec.avg_house_consumption_7d * slots_per_hour, 3),
                    avg_14d=round(rec.avg_house_consumption_14d * slots_per_hour, 3),
                )
            )
            price_points.append(
                PricePoint(
                    hour=h,
                    import_price=round(rec.import_price * eds_share, 5),
                    export_price=round(rec.export_price * eds_share, 5),
                )
            )
            solcast_slots.append(
                SolcastSlot(
                    hour=h,
                    pv_estimate=round(rec.solcast_pv_estimate * slots_per_hour, 3),
                )
            )

        battery_schedules = [
            BatteryScheduleInput(
                enabled=s.enabled,
                start=s.start,
                end=s.end,
                min_price_difference=s.min_price_difference_required,
            )
            for s in self._batteries_schedules
        ]

        return PlannerInput(
            now_iso=now.isoformat(),
            interval_minutes=cfg.recommendation_interval_minutes,
            interval_length_hours=cfg.recommendation_interval_length,
            battery_soc_pct=convert_to_float(live.huawei_batteries_soc_pct),
            battery_rated_capacity_kwh=(
                convert_to_float(live.huawei_batteries_rated_capacity_wh) or 0.0
            )
            / 1000.0,
            battery_end_of_discharge_soc_pct=convert_to_float(
                live.huawei_batteries_end_of_discharge_soc_pct or 5.0
            ),
            battery_max_charge_power_w=convert_to_float(
                live.huawei_batteries_max_charge_power_w
            ),
            battery_max_discharge_power_w=convert_to_float(
                live.huawei_batteries_max_discharge_power_w
            )
            or None,
            battery_conversion_loss_pct=convert_to_float(cfg.batteries_conversion_loss),
            battery_purchase_price=convert_to_float(cfg.batteries_purchase_price),
            battery_expected_cycles=convert_to_int(cfg.batteries_expected_cycles),
            weight_1d=convert_to_int(cfg.house_consumption_energy_weight_1d),
            weight_3d=convert_to_int(cfg.house_consumption_energy_weight_3d),
            weight_7d=convert_to_int(cfg.house_consumption_energy_weight_7d),
            weight_14d=convert_to_int(cfg.house_consumption_energy_weight_14d),
            consumption_averages=consumption_averages,
            price_points=price_points,
            solcast_slots=solcast_slots,
            battery_schedules=battery_schedules,
            excess_export_enabled=bool(cfg.batteries_enable_excess_export),
            excess_export_discharge_buffer_pct=convert_to_float(
                cfg.batteries_excess_export_discharge_buffer
            ),
            excess_export_price_threshold=convert_to_float(
                cfg.batteries_excess_export_price_threshold
            ),
            export_min_price=convert_to_float(cfg.energi_data_service_export_min_price),
            months_winter=list(cfg.months_winter or []),
            house_power_includes_ev=bool(cfg.house_power_includes_ev_charger_power),
            is_read_only=bool(cfg.read_only),
        )

    def _apply_planner_output(self, output) -> None:
        """Write :class:`PlannerOutput` decisions back into the recommendation list.

        Args:
            output: The :class:`~planner.engine.PlannerOutput` returned by the
                planner engine.
        """
        slot_by_start = {s.start: s for s in output.slots}

        for rec in self._hourly_recommendations:
            slot = slot_by_start.get(rec.start)
            if slot is None:
                continue
            rec.recommendation = slot.recommendation
            rec.batteries_charged = slot.batteries_charged
            rec.estimated_net_consumption = slot.estimated_net_consumption
            rec.estimated_cost = slot.estimated_cost
            rec.estimated_battery_capacity = slot.estimated_battery_capacity
            rec.estimated_battery_soc = slot.estimated_battery_soc

        self._batteries_schedules_remaining_capacity_needed = sum(
            s.needed_batteries_capacity for s in self._batteries_schedules if s.enabled
        )

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
        now = dt_util.now()
        start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        steps = int((total_hours * 60) / interval_minutes)

        intervals = []
        for i in range(steps):
            t_start = start_time + timedelta(minutes=i * interval_minutes)
            t_end = t_start + timedelta(minutes=interval_minutes)
            intervals.append(
                HourlyRecommendation(
                    avg_house_consumption=0.0,
                    avg_house_consumption_1d=0.0,
                    avg_house_consumption_3d=0.0,
                    avg_house_consumption_7d=0.0,
                    avg_house_consumption_14d=0.0,
                    batteries_charged=0.0,
                    end=t_end,
                    estimated_battery_capacity=0.0,
                    estimated_battery_soc=0,
                    estimated_cost=0.0,
                    estimated_net_consumption=0.0,
                    export_price=0.0,
                    import_price=0.0,
                    recommendation=None,
                    solcast_pv_estimate=0.0,
                    start=t_start,
                )
            )
        return intervals

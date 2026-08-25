"""Setup, teardown, options handling, timers, and plan persistence.

Extracted from ``coordinator.py`` to satisfy the repository's 30 KB /
1000-line file limit. This is a pure move: the methods keep their exact
behaviour and are mixed back into ``HSEMDataUpdateCoordinator`` in MRO
order, so ``self`` and every attribute reference are unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)

from custom_components.hsem.coordinator_helpers import (
    OPTIONS_UPDATE_DEBOUNCE_SECONDS,
    LoadForecastSignature,
)
from custom_components.hsem.coordinator_state import (
    CoordinatorSharedState,
)
from custom_components.hsem.coordinator_tracking import (
    init_financial_tracker,
    init_prediction_tracker,
)
from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.custom_sensors.state_collector import (  # noqa: F401 — kept for backward compat
    async_collect_all_states,
    build_battery_schedules,
    build_sensor_config,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.utils.datetime_utils import (
    now as hsem_now,
    utc_key,
)
from custom_components.hsem.utils.logger import (
    async_log,
)


class CoordinatorLifecycleMixin(CoordinatorSharedState):
    """Setup, teardown, options handling, timers, and plan persistence."""

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

        # Start the embedded OCPP 1.6 server(s) if enabled (issue #603).
        # One server per EV: the primary EV uses the primary server, and the
        # optional second EV gets its own dedicated server on a separate port.
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

        # Second OCPP server — only when the second EV is configured/enabled.
        if cfg.ev_second_planned_load_enabled and cfg.ocpp_second_enabled:
            try:
                self._ocpp_second_server = OCPPServer(
                    hass=self.hass,
                    host="0.0.0.0",
                    port=cfg.ocpp_second_port,
                    start_window_s=cfg.ocpp_start_window_s,
                    stop_window_s=cfg.ocpp_stop_window_s,
                )
                await self._ocpp_second_server.start()
                async_log(
                    "info",
                    "Second OCPP server started on port %d",
                    cfg.ocpp_second_port,
                )
            except Exception as e:
                async_log("error", "Failed to start second OCPP server: %s", e)
                self._ocpp_second_server = None

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

        # Dedicated live-power sampling tick (issue #797), independent of
        # the main interval timer: the rolling median window needs samples
        # fresher than the main timer's minutes-scale cadence can provide.
        from custom_components.hsem.coordinator_live_power import (
            LIVE_POWER_MONITOR_INTERVAL_SECONDS,
        )

        self._live_power_timer_unsub = async_track_time_interval(
            self.hass,
            self.async_monitor_live_power,  # type: ignore[arg-type]  # HA stub expects Callable[[datetime], ...]
            timedelta(seconds=LIVE_POWER_MONITOR_INTERVAL_SECONDS),
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
        live_power_timer_unsub = getattr(self, "_live_power_timer_unsub", None)
        if live_power_timer_unsub is not None:
            live_power_timer_unsub()
            self._live_power_timer_unsub = None
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

        # Stop the OCPP servers if they were started.
        ocpp = getattr(self, "_ocpp_server", None)
        if ocpp is not None:
            await ocpp.stop()
            self._ocpp_server = None
        ocpp_second = getattr(self, "_ocpp_second_server", None)
        if ocpp_second is not None:
            await ocpp_second.stop()
            self._ocpp_second_server = None

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
        self._last_plan_ev_effective_energy_kwh = self._ev_effective_energy_kwh(
            live.ev,
            float(self._cfg.ev_planned_load_battery_capacity_kwh),
        )
        self._last_plan_ev_second_effective_energy_kwh = self._ev_effective_energy_kwh(
            live.ev_second,
            float(self._cfg.ev_second_planned_load_battery_capacity_kwh),
        )
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

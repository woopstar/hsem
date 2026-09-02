"""Live-state collection and the guarded update cycle for the coordinator.

Extracted from ``coordinator.py`` to satisfy the repository's 30 KB /
1000-line file limit. This is a pure move: the methods keep their exact
behaviour and are mixed back into ``HSEMDataUpdateCoordinator`` in MRO
order, so ``self`` and every attribute reference are unchanged.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from datetime import datetime
from typing import Any

from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.hsem.const import EMA_ALPHA_NET_CONSUMPTION
from custom_components.hsem.coordinator_builder import (
    generate_recommendation_intervals,
)
from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.coordinator_helpers import (
    _StaleUpdateCycle,
    apply_load_forecast_hold,
    assess_load_forecast,
    ocpp_charge_target,
)
from custom_components.hsem.coordinator_persistence import persist_all_trackers
from custom_components.hsem.coordinator_state import (
    CoordinatorSharedState,
)
from custom_components.hsem.coordinator_tracking import (
    accumulate_forecast_actuals,
)
from custom_components.hsem.custom_sensors.hourly_data_populator.consumption import (
    populate_avg_house_consumption_from_snapshot,
)
from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    populate_price_and_solcast_from_snapshot,
)
from custom_components.hsem.custom_sensors.state_collector import (  # noqa: F401 — kept for backward compat
    async_collect_all_states,
    build_battery_schedules,
    build_sensor_config,
)
from custom_components.hsem.models.live_state import EVLiveState, LiveState
from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.savings_tracker import SavingsTracker
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.capacity_learner import CapacityLearner
from custom_components.hsem.utils.datetime_utils import (
    now as hsem_now,
    slot_contains,
    utc_now_iso,
)
from custom_components.hsem.utils.ev_delivered_energy import EVDeliveredEnergyTracker
from custom_components.hsem.utils.forecast_tracker import ForecastTracker
from custom_components.hsem.utils.logger import (
    async_log,
    set_hsem_verbose,
)
from custom_components.hsem.utils.misc import ema_filter
from custom_components.hsem.utils.phase_power import normalize_ev_phase_topology
from custom_components.hsem.utils.prediction_tracker import PredictionTracker
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector
from custom_components.hsem.utils.weekday_profile import weekday_profile

# A stale vehicle SoC must eventually shrink the remaining planner demand, but
# not wake the MILP for every small power sample. Credit accumulates against the
# last accepted plan and crosses both an energy and a cadence gate.
EV_DELIVERED_ENERGY_REPLAN_DELTA_KWH = 0.25
EV_DELIVERED_ENERGY_REPLAN_MIN_SECONDS = 60.0
EV_DELIVERED_ENERGY_MAX_GAP_SECONDS = 3600.0

# An EV that can no longer reach its margined deadline target at max
# charger power should trigger a prompt replan (issue #845) rather than
# waiting for an unrelated event — but not on every single cadence tick.
EV_DEADLINE_PACING_REPLAN_MIN_SECONDS = 120.0


class CoordinatorCycleMixin(CoordinatorSharedState):
    """Live-state collection and the guarded update cycle for the coordinator."""

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
        self._update_ev_delivered_energy_credit(live, cfg, now)

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

        # Adjust timer based on missing-entities, pending-consumption status,
        # or a physically charging EV. Missing inputs and load-forecast
        # recovery retain their one-minute retry cadence; a charging EV also
        # refreshes each minute so delivered-energy target crossings stop a
        # bounded whole-amp overshoot promptly instead of waiting for the
        # normal (several-minute) coordinator interval (issue #797).
        if live.missing_entities or not consumption_ok or live.any_ev_charging:
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

    def _update_ev_delivered_energy_credit(
        self,
        live: LiveState,
        cfg: SensorConfig,
        now: datetime,
    ) -> None:
        """Attach bounded effective SoC estimates to one live snapshot."""
        max_gap_seconds = min(
            max(float(cfg.update_interval) * 120.0, 300.0),
            EV_DELIVERED_ENERGY_MAX_GAP_SECONDS,
        )
        chargers = (
            (
                live.ev,
                cfg.ev,
                bool(cfg.ev_planned_load_enabled),
                live.ev_planned_load_target_soc_pct,
                float(cfg.ev_planned_load_battery_capacity_kwh),
                float(cfg.ev_planned_load_charger_power_kw) * 1000.0,
                float(cfg.ev_planned_load_charger_efficiency_pct),
                bool(cfg.ev.allow_charge_past_target_soc),
                "_ev_delivered_energy_tracker",
            ),
            (
                live.ev_second,
                cfg.ev_second,
                bool(cfg.ev_second_planned_load_enabled),
                live.ev_second_planned_load_target_soc_pct,
                float(cfg.ev_second_planned_load_battery_capacity_kwh),
                float(cfg.ev_second_planned_load_charger_power_kw) * 1000.0,
                float(cfg.ev_second_planned_load_charger_efficiency_pct),
                bool(cfg.ev_second.allow_charge_past_target_soc),
                "_ev_second_delivered_energy_tracker",
            ),
        )
        for (
            ev_live,
            ev_cfg,
            enabled,
            target_soc_pct,
            capacity_kwh,
            configured_max_power_w,
            efficiency_pct,
            allow_past_target,
            tracker_attribute,
        ) in chargers:
            tracker = getattr(self, tracker_attribute, None)
            if not isinstance(tracker, EVDeliveredEnergyTracker):
                tracker = EVDeliveredEnergyTracker()
                setattr(self, tracker_attribute, tracker)

            # A configured connection sensor is the session-identity boundary.
            # Without it, carrying an estimate across two vehicles is unsafe.
            identity_valid = (
                enabled
                and ev_cfg.connected_entity is not None
                and ev_live.is_connected is True
            )
            if not identity_valid:
                tracker.reset()
                ev_live.effective_soc_pct = None
                ev_live.delivered_energy_credit_kwh = 0.0
                continue

            # Permit normal metering tolerance above the configured nameplate,
            # while rejecting unit mistakes and runaway sensor values.
            telemetry_max_power_w = max(
                configured_max_power_w * 1.25,
                configured_max_power_w + 1000.0,
            )
            estimate = tracker.update(
                now=now,
                connected=True,
                charging=ev_live.is_charging,
                power_w=ev_live.power_w,
                reported_soc_pct=ev_live.soc_pct,
                target_soc_pct=target_soc_pct,
                battery_capacity_kwh=capacity_kwh,
                charger_efficiency_pct=efficiency_pct,
                max_power_w=telemetry_max_power_w,
                allow_charge_past_target=allow_past_target,
                max_gap_seconds=max_gap_seconds,
            )
            ev_live.effective_soc_pct = estimate.effective_soc_pct
            ev_live.delivered_energy_credit_kwh = estimate.credit_kwh

    @staticmethod
    def _ev_effective_energy_kwh(
        ev_live: EVLiveState,
        battery_capacity_kwh: float,
    ) -> float | None:
        """Return bounded effective battery energy when tracking is active."""
        soc_pct = ev_live.effective_soc_pct
        if (
            soc_pct is None
            or not math.isfinite(soc_pct)
            or not 0.0 <= soc_pct <= 100.0
            or not math.isfinite(battery_capacity_kwh)
            or battery_capacity_kwh <= 0.0
        ):
            return None
        return soc_pct / 100.0 * battery_capacity_kwh

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

        # Anchor the solar corrector's slot-distance math to this cycle's
        # physical time so DST folds and mid-cycle replans compute the true
        # elapsed-slot distance, not the list position (issue #815).
        getattr(self, "_solar_corrector", SolarForecastCorrector()).set_reference_time(
            now
        )

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
            if prediction_record_added:
                await persist_all_trackers(self, only=["_prediction_tracker"])

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
        ocpp_listening = False
        ocpp_last_requested_current_a: int | None = None
        ocpp = getattr(self, "_ocpp_server", None)
        if ocpp is not None:
            ocpp_chargers = ocpp.charger_sessions
            ocpp_sessions = list(self._ocpp_sessions)
            ocpp_listening = ocpp.is_listening
            ocpp_last_requested_current_a = ocpp.last_requested_current_a

        # Second EV's OCPP server state.
        ocpp_second_chargers: dict | None = None
        ocpp_second_sessions: list | None = None
        ocpp_second_listening = False
        ocpp_second_last_requested_current_a: int | None = None
        ocpp_second = getattr(self, "_ocpp_second_server", None)
        if ocpp_second is not None:
            ocpp_second_chargers = ocpp_second.charger_sessions
            ocpp_second_sessions = []
            ocpp_second_listening = ocpp_second.is_listening
            ocpp_second_last_requested_current_a = ocpp_second.last_requested_current_a

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
            ocpp_second_chargers=ocpp_second_chargers,
            ocpp_second_sessions=ocpp_second_sessions,
            ocpp_listening=ocpp_listening,
            ocpp_second_listening=ocpp_second_listening,
            ocpp_last_requested_current_a=ocpp_last_requested_current_a,
            ocpp_second_last_requested_current_a=(ocpp_second_last_requested_current_a),
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
            live_power_estimate = getattr(self, "_live_power_estimate_this_cycle", None)
            if live_power_estimate is not None:
                self._accept_live_power_plan_estimate(
                    live_power_estimate,
                    plan_now=now,
                    requested_slot=getattr(
                        self, "_live_power_replan_request_slot_this_cycle", None
                    ),
                )

        # Push the accepted EV targets only after the freshness gate passes.
        # Each EV gets its own OCPP server: the primary plan drives the
        # primary server, the second plan drives the second server. The
        # command read here (``ev_charger_calculated_power`` /
        # ``ev_second_charger_calculated_power``) is the same post-stability,
        # force-charge-aware ceiling already published on
        # sensor.hsem_ev_charger_current_limit (and its second-EV
        # counterpart) — OCPP must never compute a second, divergent target
        # from the raw plan.
        current_slot = next(
            (
                item
                for item in self._hourly_recommendations
                if slot_contains(item.start, item.end, now)
            ),
            None,
        )

        ocpp_server = getattr(self, "_ocpp_server", None)
        if ocpp_server is not None and self._cfg.ocpp_enabled:
            cpid = self._cfg.ocpp_cpid or "default"
            power_w = (
                current_slot.ev_charger_calculated_power
                if current_slot is not None
                else 0.0
            )
            topology = normalize_ev_phase_topology(
                self._cfg.ev_planned_load_charger_phase_topology
            )
            target_kw, max_current_a = ocpp_charge_target(power_w, topology)
            await ocpp_server.update_charge_target(
                cpid, target_kw, max_current_a=max_current_a, now=now
            )

        ocpp_second_server = getattr(self, "_ocpp_second_server", None)
        if (
            ocpp_second_server is not None
            and self._cfg.ocpp_enabled
            and self._cfg.ocpp_second_enabled
        ):
            second_cpid = self._cfg.ocpp_second_cpid or "default"
            second_power_w = (
                current_slot.ev_second_charger_calculated_power
                if current_slot is not None
                else 0.0
            )
            second_topology = normalize_ev_phase_topology(
                self._cfg.ev_second_planned_load_charger_phase_topology
            )
            second_target_kw, second_max_current_a = ocpp_charge_target(
                second_power_w, second_topology
            )
            await ocpp_second_server.update_charge_target(
                second_cpid,
                second_target_kw,
                max_current_a=second_max_current_a,
                now=now,
            )

        async_log("debug", "------ HSEM Coordinator: update cycle complete")

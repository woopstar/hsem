"""Planner invocation and replan-decision behaviour for the coordinator.

Extracted from ``coordinator.py`` to satisfy the repository's 30 KB /
1000-line file limit. This is a pure move: the methods keep their exact
behaviour and are mixed back into ``HSEMDataUpdateCoordinator`` in MRO
order, so ``self`` and every attribute reference are unchanged.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime

from custom_components.hsem.coordinator_builder import (
    build_planner_input,
)
from custom_components.hsem.coordinator_cycle import (
    EV_DELIVERED_ENERGY_REPLAN_DELTA_KWH,
    EV_DELIVERED_ENERGY_REPLAN_MIN_SECONDS,
)
from custom_components.hsem.coordinator_helpers import (
    LoadForecastSignature,
    _SimpleSlot,
    _StaleUpdateCycle,
    apply_current_ev_power_override,
    apply_force_charge_now,
    apply_load_forecast_hold,
    live_demand_contradicts_zero_profile,
    load_forecast_signatures_match,
    reset_force_charge_on_disconnect,
)
from custom_components.hsem.coordinator_persistence import persist_all_trackers
from custom_components.hsem.coordinator_state import (
    CoordinatorSharedState,
)
from custom_components.hsem.coordinator_tracking import (
    accumulate_daily_plan_actuals,
    accumulate_financials,
    accumulate_savings,
    register_forecasts_from_planner,
)
from custom_components.hsem.custom_sensors.state_collector import (  # noqa: F401 — kept for backward compat
    async_collect_all_states,
    build_sensor_config,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.charge_scheduler import apply_window_hysteresis
from custom_components.hsem.utils.capacity_learner import CapacityLearner
from custom_components.hsem.utils.datetime_utils import (
    as_tz,
    utc_key,
)
from custom_components.hsem.utils.logger import (
    async_log,
    set_hsem_verbose,
)
from custom_components.hsem.utils.misc import get_config_value
from custom_components.hsem.utils.units import usable_kwh_from_rated


class CoordinatorPlannerPhaseMixin(CoordinatorSharedState):
    """Planner invocation and replan-decision behaviour for the coordinator."""

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
        window hysteresis, EV charger command stability, and all post-plan
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

        # Seed the rolling live-power window from this cycle's immutable
        # snapshot (issue #797). The dedicated fast timer
        # (coordinator_live_power.py) keeps the window fresh between full
        # cycles; this seed also lets a slow-cadence cycle (no fast timer
        # sample in between) still see an up-to-date estimate.
        live_power_estimate = self._seed_live_power_window(now, cfg, live)
        live_power_replan_request_slot = self._actionable_live_power_replan_slot(
            now, live_power_estimate
        )
        # Stashed for the cycle's plan-acceptance hook (coordinator_cycle.py),
        # which runs after this method returns.
        self._live_power_estimate_this_cycle = live_power_estimate
        self._live_power_replan_request_slot_this_cycle = live_power_replan_request_slot

        # Determine whether a full re-plan is needed.
        should_replan = self._should_replan(
            live,
            now,
            load_forecast_signature=self._current_load_forecast_signature,
            live_power_replan_request_slot=live_power_replan_request_slot,
        )

        if should_replan:
            planner_input = build_planner_input(
                cfg=cfg,
                live=live,
                hourly_recommendations=self._hourly_recommendations,
                previous_winner_name=self._previous_planner_winner_name,
                previous_winner_score=self._previous_planner_winner_score,
                ev_session_kw=ev_session_kw if ev_session_kw else None,
                dynamic_discharge_floor_pct=_dynamic_floor_pct,
                capacity_learner=getattr(self, "_capacity_learner", CapacityLearner()),
                live_power_estimate=live_power_estimate,
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

        # 8c. Auto-disable force-charge-now on EV disconnect (issue #900).
        # Must run before the force-charge-now override below so a
        # disconnect and reset in the same cycle never leaves a stale
        # forced-charge slot.
        reset_force_charge_on_disconnect(
            hass=self.hass,
            config_entry=self._config_entry,
            was_connected=getattr(self, "_last_plan_ev_connected", None),
            is_connected=live.ev.is_connected,
            option_key="hsem_ev_force_charge_now",
            ev_label="EV1",
        )
        reset_force_charge_on_disconnect(
            hass=self.hass,
            config_entry=self._config_entry,
            was_connected=getattr(self, "_last_plan_ev_second_connected", None),
            is_connected=live.ev_second.is_connected,
            option_key="hsem_ev_second_force_charge_now",
            ev_label="EV2",
        )

        # 8d. Force-charge-now override.
        apply_force_charge_now(
            config_entry=self._config_entry,
            hourly_recommendations=self._hourly_recommendations,
            ev_plan=self._ev_charging_plan,
            ev_second_plan=self._ev_second_charging_plan,
            now=now,
            live=live,
        )

        # 8e. Load-forecast fail-closed hold.
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

        # 8f. EV charger command stability — damp integer-lattice churn and
        # suppress a slot-tail stop.  Runs last so it smooths the command that
        # every earlier override has already had its say on.
        self._apply_ev_command_stability(now, live, cfg)

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

        # Persist financial and savings tracker state (issues #599, #604,
        # #890) through the shared registry -- see coordinator_persistence.py.
        try:
            await persist_all_trackers(
                self, only=["_financial_tracker", "_savings_tracker"]
            )
        except Exception as e:
            async_log(
                "error",
                "Tracker persistence failed: %s",
                e,
            )

        return state, should_replan, planner_output

    def _ev_delivered_energy_requires_replan(
        self,
        live: LiveState,
        now: datetime,
    ) -> bool:
        """Return whether credited energy materially changed since acceptance.

        Two independent triggers:

        - **Target-crossing bypass** (issue #797): the whole-amp MILP lattice
          may deliberately plan up to one activation quantum beyond the
          exact target so it never promises an avoidable deadline miss (see
          the target-cap relaxation in ``planner/milp/_constraints.py``).
          As soon as delivered-energy crosses the accepted HSEM target, that
          bounded excess must stop immediately — neither the ordinary
          one-minute cadence gate nor the 0.25 kWh materiality threshold
          below applies to this safety transition.
        - **Materiality threshold**: outside a target crossing, a stale
          reported SoC only forces a replan once accumulated delivered
          energy diverges from the accepted baseline by a material amount,
          gated by the minimum cadence below.
        """
        last_plan_at = self._last_plan_slot_start
        elapsed_seconds: float | None = None
        if last_plan_at is not None:
            try:
                elapsed_seconds = (utc_key(now) - utc_key(last_plan_at)).total_seconds()
            except TypeError, ValueError:
                elapsed_seconds = None

        observations: list[tuple[str, float, float]] = []
        for label, ev_live, capacity_kwh, baseline_kwh, target_soc_pct in (
            (
                "EV",
                live.ev,
                float(self._cfg.ev_planned_load_battery_capacity_kwh),
                getattr(self, "_last_plan_ev_effective_energy_kwh", None),
                getattr(self, "_last_plan_ev_target_soc", None),
            ),
            (
                "EV2",
                live.ev_second,
                float(self._cfg.ev_second_planned_load_battery_capacity_kwh),
                getattr(self, "_last_plan_ev_second_effective_energy_kwh", None),
                getattr(self, "_last_plan_ev2_target_soc", None),
            ),
        ):
            current_kwh = self._ev_effective_energy_kwh(ev_live, capacity_kwh)
            if not ev_live.is_charging or current_kwh is None or baseline_kwh is None:
                continue
            try:
                baseline = float(baseline_kwh)
            except TypeError, ValueError:
                continue
            if not math.isfinite(baseline):
                continue

            target_kwh: float | None = None
            if target_soc_pct is not None:
                try:
                    target_pct = float(target_soc_pct)
                except TypeError, ValueError:
                    target_pct = None
                if (
                    target_pct is not None
                    and math.isfinite(target_pct)
                    and 0.0 <= target_pct <= 100.0
                ):
                    target_kwh = target_pct / 100.0 * capacity_kwh

            observations.append((label, current_kwh, baseline))

            if (
                target_kwh is not None
                and baseline + 1e-9 < target_kwh
                and current_kwh + 1e-9 >= target_kwh
            ):
                async_log(
                    "debug",
                    "[replan] %s effective battery energy crossed the accepted "
                    "target (%.3f → %.3f kWh, target %.3f kWh) — re-planning.",
                    label,
                    baseline,
                    current_kwh,
                    target_kwh,
                )
                return True

        if last_plan_at is not None and (
            elapsed_seconds is None
            or elapsed_seconds < EV_DELIVERED_ENERGY_REPLAN_MIN_SECONDS
        ):
            return False

        for label, current_kwh, baseline_kwh in observations:
            delta_kwh = current_kwh - baseline_kwh
            if abs(delta_kwh) + 1e-9 >= EV_DELIVERED_ENERGY_REPLAN_DELTA_KWH:
                async_log(
                    "debug",
                    "[replan] %s effective battery energy changed by %.3f kWh "
                    "since the accepted plan — re-planning.",
                    label,
                    delta_kwh,
                )
                return True
        return False

    # ------------------------------------------------------------------
    # DataUpdateCoordinator override
    # ------------------------------------------------------------------

    def _should_replan(
        self,
        live: LiveState,
        now: datetime,
        *,
        load_forecast_signature: LoadForecastSignature | None = None,
        live_power_replan_request_slot: datetime | None = None,
    ) -> bool:
        """Determine whether the planner should be re-run.

        Returns ``True`` when a material event occurred since the last plan:

        - EV connection state changed (plugged in or unplugged)
        - EV charging state changed (started or stopped)
        - EV SoC crossed the target threshold
        - Forced working mode changed
        - Crossed into a new recommendation slot
        - Import price changed significantly (new price period)
        - Sustained material rolling house/PV change in the current slot,
          within its bounded correction budget (issue #797)
        - An EV can no longer reach its margined deadline target at max
          charger power for the remaining time (issue #845)

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

        if live_power_replan_request_slot is not None:
            async_log(
                "debug",
                "[replan] Sustained live power changed — re-planning.",
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

        if self._ev_delivered_energy_requires_replan(live, now):
            return True

        if self._ev_deadline_pacing_requires_replan(live, now):
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

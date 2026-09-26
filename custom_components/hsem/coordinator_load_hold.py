"""Load-forecast safety hold for the non-planner path of the update cycle.

Extracted from ``coordinator_cycle.py`` to keep that module within the
repository's 30 KB / 1000-line limit. Mixed back into
``HSEMDataUpdateCoordinator`` in MRO order, so ``self`` and every attribute
reference are unchanged.

While the hold is active the home battery stays strictly idle, but each
managed EV on smart charging follows a grid-only EV-only fallback plan
(issue #1106, see :mod:`~planner.ev_fallback`). Force-charge-now is applied
on top of that (issue #1103), and command stability runs last.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.coordinator_helpers import (
    apply_force_charge_overrides,
    apply_load_forecast_hold,
    ev_site_power_budget_w,
    write_ev_slot_commands,
)
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.planner.ev_fallback import (
    EvFallbackResult,
    EvFallbackSlot,
    build_ev_only_fallback_plan,
    estimate_unpriced_tail,
    finalize_fallback_plan,
)
from custom_components.hsem.planner.ev_planner_models import (
    EVChargingPlan,
    EVPlannerInput,
)
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.datetime_utils import slot_contains, utc_key
from custom_components.hsem.utils.ev_accounting import normalized_baseline_includes_ev
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.units import slot_duration_hours

#: Plan-explanation constraint marking an active EV-only fallback.
EV_ONLY_FALLBACK_CONSTRAINT = "ev_only_fallback"


class CoordinatorLoadHoldMixin(CoordinatorSharedState):
    """Publish the strict storage hold while the load forecast is not ready."""

    def _apply_load_forecast_safety_hold(
        self,
        now: datetime,
        live: LiveState,
        load_forecast_ready: bool,
    ) -> HourlyRecommendation | None:
        """Hold the battery, plan EVs grid-only, then re-apply force charge.

        Returns the held current slot, or ``None`` when no hold applies
        (forecast ready, manual force mode, or no current slot).

        Order matters. The strict hold clears the current slot, the EV-only
        fallback (issue #1106) writes each managed EV's grid-only commands,
        force-charge-now overrides them (issue #1103), and command stability
        smooths and fuse-clamps the result last. The primary battery's
        charge and discharge stay zero throughout.
        """
        load_hold = apply_load_forecast_hold(
            self._hourly_recommendations,
            live,
            now,
            load_forecast_ready=load_forecast_ready,
        )
        if load_hold is None:
            return None
        reason = self._last_load_forecast_readiness_reason
        assert reason is not None

        fallback_plans = self._apply_ev_only_fallback(now, live, reason)
        ev_plan = fallback_plans[0] or self._ev_charging_plan
        ev_second_plan = fallback_plans[1] or self._ev_second_charging_plan

        apply_force_charge_overrides(
            hass=self.hass,
            config_entry=self._config_entry,
            hourly_recommendations=self._hourly_recommendations,
            ev_plan=ev_plan,
            ev_second_plan=ev_second_plan,
            now=now,
            live=live,
            was_connected=getattr(self, "_last_plan_ev_connected", None),
            was_second_connected=getattr(self, "_last_plan_ev_second_connected", None),
        )
        # Runs last on this path too, as in the planner phase: whole-amp
        # quantisation, deadband, and the live fuse clamp.
        self._apply_ev_command_stability(now, live, self._cfg)
        # The hold is a wait slot; it is relabelled only while the final,
        # stabilised command actually charges an EV.
        load_hold.recommendation = (
            Recommendations.EVSmartCharging.value
            if load_hold.ev_charger_calculated_power > 1e-9
            or load_hold.ev_second_charger_calculated_power > 1e-9
            else Recommendations.BatteriesWaitMode.value
        )
        if any(plan is not None for plan in fallback_plans):
            self._publish_fallback_plans(now, fallback_plans)

        # No plan is accepted during a hold, so advance the disconnect
        # baseline here (issue #900); recovery always forces a replan anyway.
        self._last_plan_ev_connected = live.ev.is_connected
        self._last_plan_ev_second_connected = live.ev_second.is_connected
        self._hourly_recommendation = load_hold

        summary = (
            f"Battery held because the house-load forecast is not ready ({reason})."
        )
        constraints = [f"load_forecast:{reason}"]
        if any(plan is not None for plan in fallback_plans):
            summary += " EV smart charging follows a grid-only fallback plan."
            constraints.append(EV_ONLY_FALLBACK_CONSTRAINT)
        self._plan_explanation = PlanExplanation(
            selected_strategy="safety_hold",
            winner_name="safety_hold",
            summary=summary,
            constraints=constraints,
        )
        return load_hold

    # ------------------------------------------------------------------
    # EV-only fallback (issue #1106)
    # ------------------------------------------------------------------

    def _fallback_slots(self) -> list[EvFallbackSlot]:
        """Return the horizon slots, chronologically, with import prices."""
        return [
            EvFallbackSlot(start=rec.start, end=rec.end, import_price=rec.import_price)
            for rec in sorted(self._hourly_recommendations, key=lambda r: r.start)
        ]

    def _fallback_input(
        self, now: datetime, live: LiveState, *, is_second: bool
    ) -> EVPlannerInput | None:
        """Return the EV planner input for one EV, or ``None`` if not configured.

        Mirrors ``coordinator_builder.build_planner_input`` for the EV fields:
        the bounded delivered-energy SoC is preferred over the raw reading, and
        an unknown SoC stays ``None`` so the EV planner refuses to plan on it
        (issue #988).
        """
        cfg = self._cfg
        if is_second:
            enabled = cfg.ev_second_planned_load_enabled
            ev_live, ev_cfg = live.ev_second, cfg.ev_second
            connected = live.ev_second_planned_load_connected
            smart = live.ev_second_planned_load_smart_charging_enabled
            raw_soc = live.ev_second_planned_load_current_soc_pct
            target = live.ev_second_planned_load_target_soc_pct
            deadline = live.ev_second_planned_load_deadline
            capacity = cfg.ev_second_planned_load_battery_capacity_kwh
            power = cfg.ev_second_planned_load_charger_power_kw
            efficiency = cfg.ev_second_planned_load_charger_efficiency_pct
            min_power = cfg.ev_second_planned_load_charger_min_power_w
        else:
            enabled = cfg.ev_planned_load_enabled
            ev_live, ev_cfg = live.ev, cfg.ev
            connected = live.ev_planned_load_connected
            smart = live.ev_planned_load_smart_charging_enabled
            raw_soc = live.ev_planned_load_current_soc_pct
            target = live.ev_planned_load_target_soc_pct
            deadline = live.ev_planned_load_deadline
            capacity = cfg.ev_planned_load_battery_capacity_kwh
            power = cfg.ev_planned_load_charger_power_kw
            efficiency = cfg.ev_planned_load_charger_efficiency_pct
            min_power = cfg.ev_planned_load_charger_min_power_w
        if not enabled:
            return None
        soc = convert_to_float(ev_live.effective_soc_pct)
        if soc is None:
            soc = convert_to_float(raw_soc)
        return EVPlannerInput(
            enabled=True,
            ev_connected=bool(connected),
            smart_charging_enabled=bool(smart),
            current_soc_pct=soc,
            target_soc_pct=convert_to_float(target) or 80.0,
            battery_capacity_kwh=convert_to_float(capacity) or 0.0,
            charger_power_kw=convert_to_float(power) or 0.0,
            charger_efficiency_pct=convert_to_float(efficiency) or 100.0,
            charger_min_power_w=convert_to_float(min_power) or 1380.0,
            deadline=deadline,
            base_load_includes_ev=normalized_baseline_includes_ev(
                raw_house_meter_includes_ev=bool(
                    cfg.house_power_includes_ev_charger_power
                ),
                ev_power_entity=ev_cfg.power_entity,
            ),
            now=now,
        )

    def _apply_ev_only_fallback(
        self,
        now: datetime,
        live: LiveState,
        reason: str,
    ) -> tuple[EVChargingPlan | None, EVChargingPlan | None]:
        """Build each configured EV's fallback plan and write its commands.

        Returns the ``(primary, second)`` fallback plans, ``None`` for an EV
        whose planned-load feature is off. The current slot is clamped to the
        live fuse budget, which the two EVs share. Primary-battery fields are
        never touched.
        """
        slots = self._fallback_slots()
        results: list[EvFallbackResult | None] = []
        for is_second in (False, True):
            inp = self._fallback_input(now, live, is_second=is_second)
            results.append(
                None
                if inp is None
                else build_ev_only_fallback_plan(
                    inp,
                    slots,
                    interval_minutes=self._cfg.recommendation_interval_minutes,
                    load_forecast_reason=reason,
                    is_second=is_second,
                )
            )
        primary, second = results
        if primary is None and second is None:
            return None, None

        budget_w = ev_site_power_budget_w(self._config_entry, live)
        by_start = {utc_key(rec.start): rec for rec in self._hourly_recommendations}
        for i, slot in enumerate(slots):
            primary_w = primary.slot_commands_w[i] if primary else 0.0
            second_w = second.slot_commands_w[i] if second else 0.0
            rec = by_start[utc_key(slot.start)]
            is_current = slot_contains(rec.start, rec.end, now)
            if is_current:
                primary_w = min(primary_w, budget_w)
                second_w = min(second_w, max(budget_w - primary_w, 0.0))
            elif primary_w <= 1e-9 and second_w <= 1e-9:
                continue
            remaining_hours = slot_duration_hours(
                max(now, rec.start) if is_current else rec.start, rec.end
            )
            if remaining_hours <= 1e-9:
                continue
            write_ev_slot_commands(
                rec,
                primary_w=primary_w,
                second_w=second_w,
                remaining_hours=remaining_hours,
                old_planned_ev_kwh=max(float(rec.ev_planned_load_kwh), 0.0),
                primary_base_load_includes_ev=bool(
                    primary and primary.plan.base_load_includes_ev
                ),
                second_base_load_includes_ev=bool(
                    second and second.plan.base_load_includes_ev
                ),
            )
        return (
            primary.plan if primary else None,
            second.plan if second else None,
        )

    def _publish_fallback_plans(
        self,
        now: datetime,
        plans: tuple[EVChargingPlan | None, EVChargingPlan | None],
    ) -> None:
        """Publish fallback plans rebuilt from the final slot commands."""
        slots = self._fallback_slots()
        prices = dict(
            zip(
                (utc_key(s.start) for s in slots),
                estimate_unpriced_tail(slots),
                strict=True,
            )
        )
        primary, second = plans
        if primary is not None:
            self._ev_charging_plan = finalize_fallback_plan(
                primary,
                self._hourly_recommendations,
                now,
                float(self._cfg.ev_planned_load_charger_efficiency_pct or 100.0),
                prices,
            )
        if second is not None:
            self._ev_second_charging_plan = finalize_fallback_plan(
                second,
                self._hourly_recommendations,
                now,
                float(self._cfg.ev_second_planned_load_charger_efficiency_pct or 100.0),
                prices,
                is_second=True,
            )

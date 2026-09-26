"""Load-forecast safety hold for the non-planner path of the update cycle.

Extracted from ``coordinator_cycle.py`` to keep that module within the
repository's 30 KB / 1000-line limit. Mixed back into
``HSEMDataUpdateCoordinator`` in MRO order, so ``self`` and every attribute
reference are unchanged.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.coordinator_helpers import (
    apply_force_charge_overrides,
    apply_load_forecast_hold,
)
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.plan_explanation import PlanExplanation


class CoordinatorLoadHoldMixin(CoordinatorSharedState):
    """Publish the strict storage hold while the load forecast is not ready."""

    def _apply_load_forecast_safety_hold(
        self,
        now: datetime,
        live: LiveState,
        load_forecast_ready: bool,
    ) -> HourlyRecommendation | None:
        """Hold the current slot and re-apply force-charge-now on top of it.

        Returns the held current slot, or ``None`` when no hold applies
        (forecast ready, manual force mode, or no current slot).

        Force charge is an explicit user override, so it is applied *after*
        the hold (issue #1103): the forced EV charges at its fuse-limited
        maximum while the home battery stays held.
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
        apply_force_charge_overrides(
            hass=self.hass,
            config_entry=self._config_entry,
            hourly_recommendations=self._hourly_recommendations,
            ev_plan=self._ev_charging_plan,
            ev_second_plan=self._ev_second_charging_plan,
            now=now,
            live=live,
            was_connected=getattr(self, "_last_plan_ev_connected", None),
            was_second_connected=getattr(self, "_last_plan_ev_second_connected", None),
        )
        # No plan is accepted during a hold, so advance the disconnect
        # baseline here (issue #900); recovery always forces a replan anyway.
        self._last_plan_ev_connected = live.ev.is_connected
        self._last_plan_ev_second_connected = live.ev_second.is_connected
        self._hourly_recommendation = load_hold
        self._plan_explanation = PlanExplanation(
            selected_strategy="safety_hold",
            winner_name="safety_hold",
            summary=(
                f"Battery held because the house-load forecast is not ready ({reason})."
            ),
            constraints=[f"load_forecast:{reason}"],
        )
        return load_hold

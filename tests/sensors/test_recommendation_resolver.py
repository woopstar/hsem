"""Tests for custom_sensors/recommendation_resolver.py.

All four priority branches of :func:`resolve_current_recommendation` are
tested with plain dataclasses — no Home Assistant required.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from custom_components.hsem.custom_sensors.recommendation_resolver import (
    resolve_current_recommendation,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import EVLiveState, LiveState
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rec(recommendation: str | None = None) -> HourlyRecommendation:
    """Return a minimal HourlyRecommendation with a given recommendation value."""
    from datetime import datetime

    now = datetime.now(tz=UTC)
    return HourlyRecommendation(
        avg_house_consumption_kwh=0.5,
        avg_house_consumption_1d_kwh=0.5,
        avg_house_consumption_3d_kwh=0.5,
        avg_house_consumption_7d_kwh=0.5,
        avg_house_consumption_14d_kwh=0.5,
        batteries_charged_kwh=0.0,
        batteries_discharged_kwh=0.0,
        end=now,
        estimated_battery_capacity_kwh=5.0,
        estimated_battery_soc_pct=50,
        estimated_cost_currency=0.1,
        estimated_net_consumption_kwh=0.3,
        export_price=0.5,
        grid_export_kwh=0.0,
        grid_import_kwh=0.0,
        import_price=0.8,
        recommendation=recommendation,
        solcast_pv_estimate_kwh=0.0,
        start=now,
    )


def _make_live(
    import_price: float = 0.5,
    ev_charging: bool = False,
    ev2_charging: bool = False,
    battery_kwh: float = 5.0,
) -> LiveState:
    live = LiveState()
    live.import_electricity_price = import_price
    live.ev = EVLiveState(is_charging=ev_charging)
    live.ev_second = EVLiveState(is_charging=ev2_charging)
    live.battery_current_capacity_kwh = battery_kwh
    return live


# ---------------------------------------------------------------------------
# Priority 1: Negative import price → ForceExport
# ---------------------------------------------------------------------------


class TestNegativeImportPrice:
    def test_negative_price_overrides_any_recommendation(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesDischargeMode.value)
        resolve_current_recommendation(rec, _make_live(import_price=-0.01), 0.0)
        assert rec.recommendation == Recommendations.ForceExport.value

    def test_zero_price_does_not_force_export(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        resolve_current_recommendation(rec, _make_live(import_price=0.0), 0.0)
        assert rec.recommendation == Recommendations.BatteriesWaitMode.value

    def test_positive_price_does_not_force_export(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        resolve_current_recommendation(rec, _make_live(import_price=0.5), 0.0)
        assert rec.recommendation == Recommendations.BatteriesWaitMode.value


# ---------------------------------------------------------------------------
# Priority 2: BatteriesChargeGrid must not be overridden
# ---------------------------------------------------------------------------


class TestGridChargePreserved:
    def test_grid_charge_not_overridden_by_ev(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesChargeGrid.value)
        live = _make_live(import_price=0.5, ev_charging=True)
        resolve_current_recommendation(rec, live, 0.0)
        assert rec.recommendation == Recommendations.BatteriesChargeGrid.value


# ---------------------------------------------------------------------------
# Priority 3: Active EV → EVSmartCharging
# ---------------------------------------------------------------------------


class TestEVSmartCharging:
    def test_ev1_charging_triggers_ev_mode(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesDischargeMode.value)
        live = _make_live(import_price=0.5, ev_charging=True)
        resolve_current_recommendation(rec, live, 0.0)
        assert rec.recommendation == Recommendations.EVSmartCharging.value

    def test_ev2_charging_triggers_ev_mode(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        live = _make_live(import_price=0.5, ev2_charging=True)
        resolve_current_recommendation(rec, live, 0.0)
        assert rec.recommendation == Recommendations.EVSmartCharging.value

    def test_no_ev_charging_no_override(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        live = _make_live(ev_charging=False, ev2_charging=False)
        resolve_current_recommendation(rec, live, 0.0)
        assert rec.recommendation == Recommendations.BatteriesWaitMode.value


# ---------------------------------------------------------------------------
# Priority 4: Battery above remaining schedule need → BatteriesDischargeMode
# ---------------------------------------------------------------------------


class TestBatteryAboveScheduleNeed:
    def test_battery_above_need_sets_discharge_mode(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        live = _make_live(battery_kwh=8.0)
        # remaining need = 5 kWh, battery = 8 kWh → discharge mode
        resolve_current_recommendation(
            rec, live, batteries_schedules_remaining_capacity_needed=5.0
        )
        assert rec.recommendation == Recommendations.BatteriesDischargeMode.value

    def test_battery_exactly_at_need_no_override(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        live = _make_live(battery_kwh=5.0)
        resolve_current_recommendation(
            rec, live, batteries_schedules_remaining_capacity_needed=5.0
        )
        # Not strictly greater, so no override
        assert rec.recommendation == Recommendations.BatteriesWaitMode.value

    def test_zero_remaining_need_no_discharge_override(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        live = _make_live(battery_kwh=10.0)
        resolve_current_recommendation(
            rec, live, batteries_schedules_remaining_capacity_needed=0.0
        )
        assert rec.recommendation == Recommendations.BatteriesWaitMode.value


# ---------------------------------------------------------------------------
# None rec safety
# ---------------------------------------------------------------------------


class TestNoneRec:
    def test_none_rec_does_not_raise(self):
        """resolve_current_recommendation should be a no-op when rec is None."""
        resolve_current_recommendation(None, _make_live(), 0.0)  # NOSONAR


# ---------------------------------------------------------------------------
# Regression: data.state must be synced after resolver overrides rec
#
# Repro: planner emits batteries_charge_solar; EV is charging at runtime.
# resolve_current_recommendation mutates hourly_rec → ev_smart_charging but
# data.state was left pointing at the original planner string, so the HA
# sensor state showed batteries_charge_solar instead of ev_smart_charging.
# ---------------------------------------------------------------------------


class TestDataStateSync:
    """Verify that data.state is updated to match rec after resolver override.

    The working_mode_sensor syncs data.state immediately after calling
    resolve_current_recommendation, so the HA state property always reflects
    the effective resolved recommendation — not the raw planner output.
    """

    def _make_coordinator_data(
        self,
        planner_recommendation: str,
        resolved_recommendation: str,
        live: LiveState,
    ) -> Any:
        """Return a CoordinatorData-like namespace simulating the sync behaviour.

        The ``resolved_recommendation`` parameter is passed by callers as
        documentation of the expected post-resolution state.  It is not used
        in the method body because the resolver under test computes it
        dynamically from the live state.
        """
        # Acknowledge the parameter explicitly so static-analysis tools do not
        # flag it as unused; its purpose is to document the expected outcome.
        del resolved_recommendation
        from dataclasses import dataclass

        @dataclass
        class _Data:
            state: str | None
            hourly_recommendation: HourlyRecommendation
            batteries_schedules_remaining_capacity_needed: float
            live: LiveState

        rec = _make_rec(recommendation=planner_recommendation)
        data = _Data(
            state=planner_recommendation,
            hourly_recommendation=rec,
            batteries_schedules_remaining_capacity_needed=0.0,
            live=live,
        )
        # Simulate what working_mode_sensor._async_apply_hardware_writes does.
        resolve_current_recommendation(
            data.hourly_recommendation,
            data.live,
            data.batteries_schedules_remaining_capacity_needed,
        )
        data.state = data.hourly_recommendation.recommendation
        return data, rec

    def test_ev_charging_syncs_state_from_batteries_charge_solar(self):
        """batteries_charge_solar planner output → ev_smart_charging in state."""
        live = _make_live(import_price=0.338, ev_charging=True)
        data, rec = self._make_coordinator_data(
            planner_recommendation=Recommendations.BatteriesChargeSolar.value,
            resolved_recommendation=Recommendations.EVSmartCharging.value,
            live=live,
        )
        assert rec.recommendation == Recommendations.EVSmartCharging.value
        assert data.state == Recommendations.EVSmartCharging.value

    def test_ev_charging_syncs_state_from_batteries_discharge_mode(self):
        """batteries_discharge_mode planner output → ev_smart_charging in state."""
        live = _make_live(import_price=0.5, ev_charging=True)
        data, rec = self._make_coordinator_data(
            planner_recommendation=Recommendations.BatteriesDischargeMode.value,
            resolved_recommendation=Recommendations.EVSmartCharging.value,
            live=live,
        )
        assert rec.recommendation == Recommendations.EVSmartCharging.value
        assert data.state == Recommendations.EVSmartCharging.value

    def test_grid_charge_preserved_in_state_even_when_ev_charging(self):
        """BatteriesChargeGrid must not be overridden even when EV is charging."""
        live = _make_live(import_price=0.5, ev_charging=True)
        data, rec = self._make_coordinator_data(
            planner_recommendation=Recommendations.BatteriesChargeGrid.value,
            resolved_recommendation=Recommendations.BatteriesChargeGrid.value,
            live=live,
        )
        assert rec.recommendation == Recommendations.BatteriesChargeGrid.value
        assert data.state == Recommendations.BatteriesChargeGrid.value

    def test_no_ev_charging_state_unchanged(self):
        """When EV is not charging, state remains the planner recommendation."""
        live = _make_live(import_price=0.5, ev_charging=False)
        data, rec = self._make_coordinator_data(
            planner_recommendation=Recommendations.BatteriesChargeSolar.value,
            resolved_recommendation=Recommendations.BatteriesChargeSolar.value,
            live=live,
        )
        assert rec.recommendation == Recommendations.BatteriesChargeSolar.value
        assert data.state == Recommendations.BatteriesChargeSolar.value


# ---------------------------------------------------------------------------
# EVSmartCharging label via resolver — live charging always overrides
# ---------------------------------------------------------------------------


class TestResolverEvSmartChargingLabel:
    """Verify that the resolver applies EVSmartCharging when live EV is charging,
    regardless of whether the slot carried planned EV load.

    The resolver operates on live hardware state and is intentionally not gated
    on ``ev_planned_load_kwh`` — an EV can start or stop charging at any moment,
    independently of the planner's forward plan.  These tests document that
    contract explicitly.
    """

    def test_ev_charging_overrides_wait_mode_no_planned_load(self):
        """Live EV charging overrides BatteriesWaitMode even when ev_planned_load_kwh=0."""
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        rec.ev_planned_load_kwh = 0.0
        live = _make_live(import_price=0.5, ev_charging=True)
        resolve_current_recommendation(rec, live, 0.0)
        assert rec.recommendation == Recommendations.EVSmartCharging.value

    def test_ev_charging_overrides_wait_mode_with_planned_load(self):
        """Live EV charging overrides BatteriesWaitMode when ev_planned_load_kwh > 0."""
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        rec.ev_planned_load_kwh = 3.5
        live = _make_live(import_price=0.5, ev_charging=True)
        resolve_current_recommendation(rec, live, 0.0)
        assert rec.recommendation == Recommendations.EVSmartCharging.value

    def test_no_live_ev_no_override_even_with_planned_load(self):
        """Without live EV charging the recommendation must not change to EVSmartCharging."""
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        rec.ev_planned_load_kwh = (
            3.5  # planner injected load but EV is not currently charging
        )
        live = _make_live(ev_charging=False, ev2_charging=False)
        resolve_current_recommendation(rec, live, 0.0)
        # Resolver branch 3 did not fire; later branches also don't match → stays as-is
        assert rec.recommendation == Recommendations.BatteriesWaitMode.value

    def test_ev2_live_charging_overrides_regardless_of_planned_load(self):
        """Second-EV live charging also triggers EVSmartCharging override."""
        rec = _make_rec(recommendation=Recommendations.BatteriesChargeSolar.value)
        rec.ev_planned_load_kwh = 0.0
        live = _make_live(import_price=0.5, ev2_charging=True)
        resolve_current_recommendation(rec, live, 0.0)
        assert rec.recommendation == Recommendations.EVSmartCharging.value

    def test_planned_load_without_live_charging_label_unchanged(self):
        """ev_planned_load_kwh > 0 alone does not trigger EVSmartCharging via resolver.

        The planner's EV label pass (in engine.py) handles the static planned label;
        the resolver only handles live hardware state.
        """
        rec = _make_rec(recommendation=Recommendations.BatteriesChargeSolar.value)
        rec.ev_planned_load_kwh = 5.0
        live = _make_live(ev_charging=False, ev2_charging=False, import_price=0.3)
        resolve_current_recommendation(rec, live, 0.0)
        # EV not live-charging → resolver branch 3 not taken → stays BatteriesChargeSolar
        assert rec.recommendation == Recommendations.BatteriesChargeSolar.value

    def test_grid_charge_still_not_overridden_when_ev_planned_load_present(self):
        """BatteriesChargeGrid must never be overridden, even with ev_planned_load_kwh > 0."""
        rec = _make_rec(recommendation=Recommendations.BatteriesChargeGrid.value)
        rec.ev_planned_load_kwh = 4.0
        live = _make_live(import_price=0.5, ev_charging=True)
        resolve_current_recommendation(rec, live, 0.0)
        assert rec.recommendation == Recommendations.BatteriesChargeGrid.value

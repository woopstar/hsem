"""Tests for custom_sensors/recommendation_resolver.py.

All four priority branches of :func:`resolve_current_recommendation` are
tested with plain dataclasses — no Home Assistant required.
"""

from __future__ import annotations

from datetime import UTC

from custom_components.hsem.custom_sensors.recommendation_resolver import (
    resolve_current_recommendation,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import EVLiveState, LiveState
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rec(recommendation=None) -> HourlyRecommendation:
    """Return a minimal HourlyRecommendation with a given recommendation value."""
    from datetime import datetime

    now = datetime.now(tz=UTC)
    return HourlyRecommendation(
        avg_house_consumption=0.5,
        avg_house_consumption_1d=0.5,
        avg_house_consumption_3d=0.5,
        avg_house_consumption_7d=0.5,
        avg_house_consumption_14d=0.5,
        batteries_charged=0.0,
        end=now,
        estimated_battery_capacity=5.0,
        estimated_battery_soc=50,
        estimated_cost=0.1,
        estimated_net_consumption=0.3,
        export_price=0.5,
        import_price=0.8,
        recommendation=recommendation,
        solcast_pv_estimate=0.0,
        start=now,
    )


def _make_live(
    import_price: float = 0.5,
    ev_charging: bool = False,
    ev2_charging: bool = False,
    battery_kwh: float = 5.0,
) -> LiveState:
    live = LiveState()
    live.energi_data_service_import_price = import_price
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
        resolve_current_recommendation(None, _make_live(), 0.0)

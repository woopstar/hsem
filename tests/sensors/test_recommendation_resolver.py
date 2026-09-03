"""Tests for custom_sensors/recommendation_resolver.py.

All priority branches of :func:`resolve_current_recommendation` are
tested with plain dataclasses — no Home Assistant required.
"""

from __future__ import annotations

from datetime import UTC

from custom_components.hsem.custom_sensors.recommendation_resolver import (
    resolve_current_recommendation,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import EVLiveState, LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
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
    ev_power_w: float | None = None,
    ev2_power_w: float | None = None,
    export_price: float = 0.5,
    export_price_available: bool = True,
) -> LiveState:
    live = LiveState()
    live.import_electricity_price = import_price
    live.export_electricity_price = export_price
    live.export_electricity_price_available = export_price_available
    live.ev = EVLiveState(is_charging=ev_charging, power_w=ev_power_w)
    live.ev_second = EVLiveState(is_charging=ev2_charging, power_w=ev2_power_w)
    live.battery_current_capacity_kwh = battery_kwh
    return live


def _make_cfg(
    batteries_enable_excess_export: bool = True,
    export_electricity_min_price: float = 0.0,
    batteries_export_min_price: float = 0.0,
) -> SensorConfig:
    cfg = SensorConfig()
    cfg.batteries_enable_excess_export = batteries_enable_excess_export
    cfg.export_electricity_min_price = export_electricity_min_price
    cfg.batteries_export_min_price = batteries_export_min_price
    return cfg


# ---------------------------------------------------------------------------
# Priority 1: Negative import price → ForceExport
# ---------------------------------------------------------------------------


class TestNegativeImportPrice:
    def test_negative_price_overrides_any_recommendation(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesDischargeMode.value)
        resolve_current_recommendation(rec, _make_live(import_price=-0.01), _make_cfg())
        assert rec.recommendation == Recommendations.ForceExport.value

    def test_zero_price_does_not_force_export(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        resolve_current_recommendation(rec, _make_live(import_price=0.0), _make_cfg())
        assert rec.recommendation == Recommendations.BatteriesWaitMode.value

    def test_positive_price_does_not_force_export(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        resolve_current_recommendation(rec, _make_live(import_price=0.5), _make_cfg())
        assert rec.recommendation == Recommendations.BatteriesWaitMode.value

    def test_negative_import_and_export_price_does_not_force_export(self):
        """Issue #732: negative export price must not be forced to export."""
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        live = _make_live(import_price=-0.7254, export_price=-0.708)
        resolve_current_recommendation(rec, live, _make_cfg())
        assert rec.recommendation == Recommendations.BatteriesWaitMode.value

    def test_negative_import_price_with_excess_export_disabled_no_override(self):
        """Issue #732: a disabled excess-export toggle must not be bypassed."""
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        live = _make_live(import_price=-0.05, export_price=0.5)
        cfg = _make_cfg(batteries_enable_excess_export=False)
        resolve_current_recommendation(rec, live, cfg)
        assert rec.recommendation == Recommendations.BatteriesWaitMode.value

    def test_negative_import_price_export_below_floor_no_override(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        live = _make_live(import_price=-0.05, export_price=0.1)
        cfg = _make_cfg(export_electricity_min_price=0.2)
        resolve_current_recommendation(rec, live, cfg)
        assert rec.recommendation == Recommendations.BatteriesWaitMode.value

    def test_negative_import_price_export_unavailable_no_override(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        live = _make_live(
            import_price=-0.05, export_price=0.5, export_price_available=False
        )
        resolve_current_recommendation(rec, live, _make_cfg())
        assert rec.recommendation == Recommendations.BatteriesWaitMode.value

    def test_negative_import_price_profitable_export_overrides(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        live = _make_live(import_price=-0.05, export_price=0.3)
        cfg = _make_cfg(export_electricity_min_price=0.2)
        resolve_current_recommendation(rec, live, cfg)
        assert rec.recommendation == Recommendations.ForceExport.value


# ---------------------------------------------------------------------------
# Priority 2: Grid charge → preserved
# ---------------------------------------------------------------------------


class TestGridChargePreserved:
    def test_grid_charge_not_overridden_by_ev(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesChargeGrid.value)
        live = _make_live(import_price=0.5, ev_charging=True)
        resolve_current_recommendation(rec, live, _make_cfg())
        assert rec.recommendation == Recommendations.BatteriesChargeGrid.value

    def test_grid_charge_not_overridden_by_negative_price(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesChargeGrid.value)
        live = _make_live(import_price=-0.05, export_price=0.5)
        # Negative price with profitable export is priority 1, so it DOES
        # override grid charge.
        resolve_current_recommendation(rec, live, _make_cfg())
        assert rec.recommendation == Recommendations.ForceExport.value

    def test_grid_charge_not_overridden_by_negative_price_unprofitable_export(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesChargeGrid.value)
        live = _make_live(import_price=-0.05, export_price=-0.03)
        resolve_current_recommendation(rec, live, _make_cfg())
        assert rec.recommendation == Recommendations.BatteriesChargeGrid.value


# ---------------------------------------------------------------------------
# Priority 3: Active EV → EVSmartCharging
# ---------------------------------------------------------------------------


class TestEVSmartCharging:
    def test_ev1_charging_triggers_ev_mode(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesDischargeMode.value)
        rec.ev_charger_calculated_power = 7500.0  # Planner allocated power
        live = _make_live(import_price=0.5, ev_charging=True)
        resolve_current_recommendation(rec, live, _make_cfg())
        assert rec.recommendation == Recommendations.EVSmartCharging.value

    def test_ev2_charging_triggers_ev_mode(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        rec.ev_second_charger_calculated_power = 11000.0  # Planner allocated power
        live = _make_live(import_price=0.5, ev2_charging=True)
        resolve_current_recommendation(rec, live, _make_cfg())
        assert rec.recommendation == Recommendations.EVSmartCharging.value

    def test_no_ev_charging_no_override(self):
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        live = _make_live(ev_charging=False, ev2_charging=False)
        resolve_current_recommendation(rec, live, _make_cfg())
        assert rec.recommendation == Recommendations.BatteriesWaitMode.value

    def test_ev1_charging_but_planner_zero_power_no_override(self):
        """EV is charging but planner set power to 0 → do NOT override."""
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        # Planner explicitly set power to 0 (stop charging command)
        rec.ev_charger_calculated_power = 0.0
        rec.ev_total_planned_load_kwh = 0.0
        live = _make_live(ev_charging=True)
        resolve_current_recommendation(rec, live, _make_cfg())
        # Should keep original WaitMode because planner said stop
        assert rec.recommendation == Recommendations.BatteriesWaitMode.value

    def test_ev1_charging_with_positive_power_overrides(self):
        """EV is charging AND planner allocated positive power → override."""
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        rec.ev_charger_calculated_power = 7500.0  # Planner allocated power
        live = _make_live(ev_charging=True)
        resolve_current_recommendation(rec, live, _make_cfg())
        assert rec.recommendation == Recommendations.EVSmartCharging.value

    def test_ev2_charging_with_positive_power_overrides(self):
        """Second EV charging AND planner allocated positive power → override."""
        rec = _make_rec(recommendation=Recommendations.BatteriesWaitMode.value)
        rec.ev_second_charger_calculated_power = 11000.0  # Planner allocated power
        live = _make_live(ev2_charging=True)
        resolve_current_recommendation(rec, live, _make_cfg())
        assert rec.recommendation == Recommendations.EVSmartCharging.value


# ---------------------------------------------------------------------------
# None rec safety
# ---------------------------------------------------------------------------


class TestNoneRec:
    def test_none_rec_does_not_raise(self):
        live = _make_live()
        resolve_current_recommendation(None, live, _make_cfg())  # type: ignore[arg-type]
        # No exception = pass

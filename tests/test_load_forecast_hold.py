"""Regression tests for the load-forecast fail-closed hold in the coordinator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.hsem.coordinator import (
    _apply_load_forecast_hold,
    _future_consumption_profile_is_nonzero,
    _live_demand_contradicts_zero_profile,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.utils.recommendations import Recommendations

_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _rec(
    *,
    start_minutes: int = 0,
    duration_minutes: int = 15,
    load_kwh: float = 1.0,
) -> HourlyRecommendation:
    """Build a current-slot recommendation with explicit load and timestamps."""
    start = _NOW + timedelta(minutes=start_minutes)
    return HourlyRecommendation(
        start=start,
        end=start + timedelta(minutes=duration_minutes),
        avg_house_consumption_kwh=load_kwh,
        avg_house_consumption_1d_kwh=load_kwh,
        avg_house_consumption_3d_kwh=load_kwh,
        avg_house_consumption_7d_kwh=load_kwh,
        avg_house_consumption_14d_kwh=load_kwh,
        batteries_charged_kwh=0.0,
        batteries_discharged_kwh=0.0,
        estimated_battery_capacity_kwh=0.0,
        estimated_battery_soc_pct=0.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=load_kwh,
        export_price=0.0,
        grid_export_kwh=0.0,
        grid_import_kwh=0.0,
        import_price=1.0,
        recommendation=Recommendations.BatteriesDischargeMode.value,
        solcast_pv_estimate_kwh=0.0,
    )


def _live(house_demand_w: float | None = None, force: str = "auto") -> LiveState:
    """Build a minimal live state snapshot."""
    live = LiveState()
    live.force_working_mode_state = force
    live.house_consumption_power_w = house_demand_w
    return live


def test_future_profile_is_nonzero_when_any_future_slot_has_load() -> None:
    """A future slot with positive load means the profile is not all-zero."""
    recs = [_rec(start_minutes=0, load_kwh=0.0), _rec(start_minutes=15, load_kwh=0.5)]
    assert _future_consumption_profile_is_nonzero(recs, _NOW) is True


def test_future_profile_is_zero_when_no_future_load() -> None:
    """A profile with only past slots is treated as zero (nothing future)."""
    recs = [_rec(start_minutes=-30, load_kwh=1.0)]
    assert _future_consumption_profile_is_nonzero(recs, _NOW) is False


def test_live_demand_contradicts_zero_profile() -> None:
    """Live positive demand disproves an all-zero future load profile."""
    recs = [_rec(start_minutes=15, load_kwh=0.0)]
    assert _live_demand_contradicts_zero_profile(recs, _live(500.0), _NOW) is True


def test_live_demand_below_threshold_does_not_contradict() -> None:
    """A near-zero live demand is consistent with a measured-zero night."""
    recs = [_rec(start_minutes=15, load_kwh=0.0)]
    assert _live_demand_contradicts_zero_profile(recs, _live(10.0), _NOW) is False


def test_nonzero_future_profile_never_contradicts() -> None:
    """Even strong live demand does not contradict a populated future profile."""
    recs = [_rec(start_minutes=15, load_kwh=2.0)]
    assert _live_demand_contradicts_zero_profile(recs, _live(5000.0), _NOW) is False


def test_hold_publishes_wait_mode_and_zero_motion() -> None:
    """Unavailable consumption holds the current slot with no storage motion."""
    recs = [_rec(start_minutes=0, load_kwh=0.0)]
    recs[0].batteries_charged_kwh = 1.0
    recs[0].batteries_discharged_kwh = 2.0

    held = _apply_load_forecast_hold(recs, _live(500.0), _NOW, consumption_ok=False)

    assert held is recs[0]
    assert held is not None
    assert held.recommendation == Recommendations.BatteriesWaitMode.value
    assert held.batteries_charged_kwh == 0.0
    assert held.batteries_discharged_kwh == 0.0


def test_hold_does_not_override_forced_mode() -> None:
    """A user-forced working mode always wins over the automatic hold."""
    recs = [_rec(start_minutes=0, load_kwh=0.0)]
    held = _apply_load_forecast_hold(
        recs, _live(500.0, force="batteries_charge_grid"), _NOW, consumption_ok=False
    )
    assert held is None
    assert recs[0].recommendation == Recommendations.BatteriesDischargeMode.value


def test_no_hold_when_consumption_ok_and_profile_populated() -> None:
    """Healthy consumption with future load does not trigger a hold."""
    recs = [_rec(start_minutes=15, load_kwh=1.0)]
    held = _apply_load_forecast_hold(recs, _live(500.0), _NOW, consumption_ok=True)
    assert held is None

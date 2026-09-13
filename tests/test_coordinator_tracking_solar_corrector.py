"""Regression test for the solar-corrector cross-restart double-count bug.

Issue #973: ``coordinator_tracking.py::accumulate_forecast_actuals`` gated
re-learning of a finalised forecast slot with an in-memory-only set
(``coordinator.py``'s ``_solar_corrector_processed``), never calling
``SolarForecastCorrector.mark_processed()``. That set resets on every Home
Assistant restart, so a restored ``ForecastTracker`` that still holds an
already-learned, finalised slot record would be fed into
``update_hour()``/``update_residual()`` a second time, double-counting the
sample in the per-hour PV correction factors.

The fix gates purely on the corrector's own persisted ``processed_through``
watermark, which survives restarts via ``SolarForecastCorrector.to_dict()``/
``load_from_dict()`` (wired through ``HSEMSolarConfidenceSensor``). This test
simulates a restart by persisting one corrector instance and restoring a
brand-new instance from that payload, then re-running
``accumulate_forecast_actuals`` against the *same* forecast tracker (whose
finalised record also survives a restart via its own persistence) and
asserting the restored corrector does not re-learn it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.hsem.coordinator_tracking import accumulate_forecast_actuals
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.utils.forecast_tracker import ForecastTracker
from custom_components.hsem.utils.prediction_tracker import PredictionTracker
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector


def _make_hourly_recommendation(
    start: datetime, end: datetime, **kwargs: Any
) -> HourlyRecommendation:
    """Return a minimal ``HourlyRecommendation`` covering ``start``-``end``."""
    defaults = {
        "avg_house_consumption_kwh": 1.0,
        "avg_house_consumption_1d_kwh": 1.0,
        "avg_house_consumption_3d_kwh": 1.0,
        "avg_house_consumption_7d_kwh": 1.0,
        "avg_house_consumption_14d_kwh": 1.0,
        "batteries_charged_kwh": 0.0,
        "batteries_discharged_kwh": 0.0,
        "estimated_battery_capacity_kwh": 5.0,
        "estimated_battery_soc_pct": 50,
        "estimated_cost_currency": 0.0,
        "estimated_net_consumption_kwh": 0.0,
        "ev_planned_load_kwh": 0.0,
        "export_price": 0.05,
        "grid_export_kwh": 0.0,
        "grid_import_kwh": 0.0,
        "import_price": 0.20,
        "recommendation": None,
        "solcast_pv_estimate_kwh": 0.5,
    }
    defaults.update(kwargs)
    return HourlyRecommendation(start=start, end=end, **defaults)  # type: ignore[arg-type]


def test_restored_solar_corrector_does_not_relearn_finalised_slot() -> None:
    """A restored corrector must skip a slot it already learned pre-restart."""
    slot0_start = datetime(2024, 6, 1, 9, 0, tzinfo=UTC)
    slot0_end = datetime(2024, 6, 1, 9, 15, tzinfo=UTC)
    current_start = datetime(2024, 6, 1, 10, 0, tzinfo=UTC)
    current_end = datetime(2024, 6, 1, 10, 15, tzinfo=UTC)
    now = datetime(2024, 6, 1, 10, 5, tzinfo=UTC)

    # This forecast tracker record already finished and was finalised in the
    # "previous session" -- it survives a restart via its own persistence
    # (ForecastTracker.to_persistence_dict / load_from_dict), independent of
    # the solar corrector's own persistence being exercised below.
    forecast_tracker = ForecastTracker(max_slots=96)
    rec0 = forecast_tracker.get_or_create_record(slot0_start, slot0_end)
    rec0.forecast_pv_kwh = 2.0
    rec0.actual_pv_kwh = 1.5
    rec0.finalise()

    hourly_recommendations = [_make_hourly_recommendation(current_start, current_end)]
    live = LiveState()
    prediction_tracker = PredictionTracker(max_records=10)
    solar_corrector = SolarForecastCorrector()

    # First cycle (pre-restart): the finalised slot0 record gets learned.
    accumulate_forecast_actuals(
        now=now,
        live=live,
        hourly_recommendations=hourly_recommendations,
        forecast_tracker=forecast_tracker,
        last_accumulation_ts=now - timedelta(seconds=300),
        solar_corrector=solar_corrector,
        prediction_tracker=prediction_tracker,
        last_planner_output=None,
        update_interval_minutes=5,
    )

    assert solar_corrector.hour_factors.get(9) == pytest.approx(0.75)
    assert len(solar_corrector._hour_history.get(9, [])) == 1
    assert solar_corrector.processed_through == slot0_start

    # Simulate a Home Assistant restart: persist the corrector's state and
    # restore it into a brand-new instance, exactly as
    # HSEMSolarConfidenceSensor.async_added_to_hass does.
    persisted = solar_corrector.to_dict()
    restored_corrector = SolarForecastCorrector()
    restored_corrector.load_from_dict(persisted, restored_at=now)

    assert restored_corrector.processed_through == slot0_start
    assert restored_corrector.hour_factors.get(9) == pytest.approx(0.75)

    # Next cycle after the "restart": the same forecast tracker still holds
    # the already-learned, finalised slot0 record. Without the fix this
    # would double-count it into update_hour()/update_residual() again.
    next_now = now + timedelta(minutes=5)
    accumulate_forecast_actuals(
        now=next_now,
        live=live,
        hourly_recommendations=hourly_recommendations,
        forecast_tracker=forecast_tracker,
        last_accumulation_ts=now,
        solar_corrector=restored_corrector,
        prediction_tracker=prediction_tracker,
        last_planner_output=None,
        update_interval_minutes=5,
    )

    # Still exactly one sample -- the restored corrector did not re-learn
    # the pre-restart slot a second time.
    assert len(restored_corrector._hour_history.get(9, [])) == 1
    assert restored_corrector.hour_factors.get(9) == pytest.approx(0.75)
    assert restored_corrector.processed_through == slot0_start

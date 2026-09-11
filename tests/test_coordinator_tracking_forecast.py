"""Regression tests for the ForecastTracker coordinator wiring (issue #972).

``ForecastTracker`` (``utils/forecast_tracker.py``) already had three
methods — ``reconcile_unfinalised_layout``, ``accumulate_power_interval``,
and the ``observed_at`` pathway of ``set_forecasts`` / ``freeze_forecasts``
— that were more correct than what ``coordinator_tracking.py`` actually
called, but nothing wired them in. This module exercises the coordinator
free functions directly (``accumulate_forecast_actuals`` and
``register_forecasts_from_planner``) against the three concrete failure
modes the issue described:

1. Cross-slot-boundary energy accumulation — a delayed cycle whose elapsed
   interval spans two slots must split the energy proportionally instead of
   crediting all of it to whichever slot merely contains ``now``.
2. Mid-cycle slot-layout change — an interval reconfiguration must discard
   the stale unfinalised record instead of ``get_or_create_record()``
   silently reusing it with the wrong ``end``.
3. Stale vs. fresh forecast baseline — the frozen baseline must reflect the
   freshest pre-start planner estimate, not the first one ever seen, and
   must not be overwritten once the slot has started.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.hsem.coordinator_tracking import (
    accumulate_forecast_actuals,
    register_forecasts_from_planner,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.utils.forecast_tracker import ForecastTracker
from custom_components.hsem.utils.prediction_tracker import PredictionTracker
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector


def _rec(start: datetime, end: datetime, **kwargs: float) -> HourlyRecommendation:
    """Build a minimal hourly recommendation for the given slot bounds."""
    defaults: dict = {
        "avg_house_consumption_kwh": 0.0,
        "avg_house_consumption_1d_kwh": 0.0,
        "avg_house_consumption_3d_kwh": 0.0,
        "avg_house_consumption_7d_kwh": 0.0,
        "avg_house_consumption_14d_kwh": 0.0,
        "batteries_charged_kwh": 0.0,
        "batteries_discharged_kwh": 0.0,
        "estimated_battery_capacity_kwh": 0.0,
        "estimated_battery_soc_pct": 50.0,
        "estimated_cost_currency": 0.0,
        "estimated_net_consumption_kwh": 0.0,
        "export_price": 0.0,
        "grid_export_kwh": 0.0,
        "grid_import_kwh": 0.0,
        "import_price": 0.0,
        "recommendation": None,
        "solcast_pv_estimate_kwh": 0.0,
    }
    defaults.update(kwargs)
    return HourlyRecommendation(start=start, end=end, **defaults)  # type: ignore[arg-type]


def _live(*, pv_w: float, load_w: float) -> LiveState:
    """Build a live snapshot with the given instantaneous power readings."""
    live = LiveState()
    live.solar_production_power_w = pv_w
    live.house_consumption_power_w = load_w
    return live


def _accumulate(
    tracker: ForecastTracker,
    *,
    now: datetime,
    live: LiveState,
    hourly_recommendations: list[HourlyRecommendation],
    last_accumulation_ts: datetime | None,
) -> datetime | None:
    """Call accumulate_forecast_actuals with fresh diagnostic-tracker collaborators."""
    new_ts, _ = accumulate_forecast_actuals(
        now=now,
        live=live,
        hourly_recommendations=hourly_recommendations,
        forecast_tracker=tracker,
        last_accumulation_ts=last_accumulation_ts,
        solar_corrector=SolarForecastCorrector(),
        prediction_tracker=PredictionTracker(),
        last_planner_output=None,
        update_interval_minutes=1,
    )
    return new_ts


class TestCrossSlotBoundaryAccumulation:
    """Gap 1 — accumulate_power_interval wired into accumulate_forecast_actuals."""

    def test_delayed_cycle_spanning_boundary_splits_energy(self) -> None:
        slot_a_start = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
        slot_a_end = datetime(2026, 6, 1, 10, 15, tzinfo=UTC)
        slot_b_start = slot_a_end
        slot_b_end = datetime(2026, 6, 1, 10, 30, tzinfo=UTC)

        tracker = ForecastTracker()
        planned = PlannerOutput(
            slots=[
                PlannedSlot(
                    start=slot_a_start,
                    end=slot_a_end,
                    solcast_pv_estimate_kwh=1.0,
                    avg_house_consumption_kwh=1.0,
                ),
                PlannedSlot(
                    start=slot_b_start,
                    end=slot_b_end,
                    solcast_pv_estimate_kwh=1.0,
                    avg_house_consumption_kwh=1.0,
                ),
            ]
        )
        # Register both slots well before either starts.
        register_forecasts_from_planner(
            planned, tracker, now=slot_a_start - timedelta(hours=1)
        )

        recommendations = [
            _rec(slot_a_start, slot_a_end),
            _rec(slot_b_start, slot_b_end),
        ]

        # First accumulation call only establishes the baseline timestamp,
        # 5 seconds before the A/B boundary.
        baseline_ts = slot_b_start - timedelta(seconds=5)
        ts = _accumulate(
            tracker,
            now=baseline_ts,
            live=_live(pv_w=3600.0, load_w=7200.0),
            hourly_recommendations=recommendations,
            last_accumulation_ts=None,
        )
        assert ts == baseline_ts

        # A delayed cycle lands 10 seconds after the A/B boundary — the
        # elapsed interval spans both slots.
        now = slot_b_start + timedelta(seconds=5)
        _accumulate(
            tracker,
            now=now,
            live=_live(pv_w=3600.0, load_w=7200.0),
            hourly_recommendations=recommendations,
            last_accumulation_ts=ts,
        )

        rec_a = tracker.find_record(slot_a_start)
        rec_b = tracker.find_record(slot_b_start)
        assert rec_a is not None
        assert rec_b is not None

        # Before the fix this entire 10s sample would have landed on slot B
        # (whichever slot contained `now`). Correct behaviour splits it
        # 5s/5s by physical overlap.
        assert rec_a.actual_pv_kwh > 0.0
        assert rec_b.actual_pv_kwh > 0.0
        assert rec_a.actual_pv_kwh == rec_b.actual_pv_kwh
        assert rec_a.actual_load_kwh == rec_b.actual_load_kwh


class TestMidCycleSlotLayoutChange:
    """Gap 2 — reconcile_unfinalised_layout wired into both coordinator entry points."""

    def test_interval_reconfiguration_discards_stale_record(self) -> None:
        hour_start = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
        hour_end = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)

        tracker = ForecastTracker()
        register_forecasts_from_planner(
            PlannerOutput(
                slots=[
                    PlannedSlot(
                        start=hour_start,
                        end=hour_end,
                        solcast_pv_estimate_kwh=4.0,
                        avg_house_consumption_kwh=1.0,
                    )
                ]
            ),
            tracker,
            now=hour_start - timedelta(hours=1),
        )

        hourly_60min = [_rec(hour_start, hour_end)]

        # Bootstrap the baseline, then accumulate one minute of energy into
        # the 60-minute record (well within the default gap tolerance).
        ts = _accumulate(
            tracker,
            now=hour_start,
            live=_live(pv_w=1000.0, load_w=500.0),
            hourly_recommendations=hourly_60min,
            last_accumulation_ts=None,
        )
        ts = _accumulate(
            tracker,
            now=hour_start + timedelta(minutes=1),
            live=_live(pv_w=1000.0, load_w=500.0),
            hourly_recommendations=hourly_60min,
            last_accumulation_ts=ts,
        )

        stale = tracker.find_record(hour_start)
        assert stale is not None
        assert stale.end == hour_end
        assert stale.actual_pv_kwh > 0.0

        # Mid-cycle replan switches to 15-minute slots for the same physical
        # start. Without reconciliation, get_or_create_record() would keep
        # matching (and silently reusing) the stale 60-minute record.
        quarter_start = hour_start
        quarter_end = hour_start + timedelta(minutes=15)
        next_quarter_start = quarter_end
        next_quarter_end = quarter_end + timedelta(minutes=15)
        hourly_15min = [
            _rec(quarter_start, quarter_end),
            _rec(next_quarter_start, next_quarter_end),
        ]

        _accumulate(
            tracker,
            now=hour_start + timedelta(minutes=2),
            live=_live(pv_w=1000.0, load_w=500.0),
            hourly_recommendations=hourly_15min,
            last_accumulation_ts=ts,
        )

        reconciled = tracker.find_record(quarter_start)
        assert reconciled is not None
        # The record now matches the new layout's end, not the stale one...
        assert reconciled.end == quarter_end
        # ...and does not carry over energy accumulated under the old,
        # incompatible 60-minute layout (no interval bridges the change).
        assert reconciled.actual_pv_kwh == 0.0
        assert reconciled.actual_load_kwh == 0.0


class TestStaleVsFreshForecastBaseline:
    """Gap 3 — observed_at / freeze_forecasts wired end-to-end."""

    def test_baseline_freezes_at_freshest_pre_start_estimate(self) -> None:
        slot_start = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        slot_end = datetime(2026, 6, 1, 12, 15, tzinfo=UTC)
        tracker = ForecastTracker()

        # Planner cycle far ahead of slot start: an early, stale estimate.
        register_forecasts_from_planner(
            PlannerOutput(
                slots=[
                    PlannedSlot(
                        start=slot_start,
                        end=slot_end,
                        solcast_pv_estimate_kwh=5.0,
                        avg_house_consumption_kwh=2.0,
                    )
                ]
            ),
            tracker,
            now=slot_start - timedelta(hours=2),
        )
        rec = tracker.find_record(slot_start)
        assert rec is not None
        assert rec.forecast_pv_kwh == 5.0
        assert rec.forecast_frozen is False

        # A later planner cycle, still before slot start, refines the
        # estimate — this must overwrite the stale one.
        register_forecasts_from_planner(
            PlannerOutput(
                slots=[
                    PlannedSlot(
                        start=slot_start,
                        end=slot_end,
                        solcast_pv_estimate_kwh=3.0,
                        avg_house_consumption_kwh=1.5,
                    )
                ]
            ),
            tracker,
            now=slot_start - timedelta(minutes=5),
        )
        assert rec.forecast_pv_kwh == 3.0
        assert rec.forecast_frozen is False

        # The slot physically starts; the coordinator's accumulation cycle
        # freezes whatever the freshest pre-start estimate was.
        hourly_recommendations = [_rec(slot_start, slot_end)]
        ts = _accumulate(
            tracker,
            now=slot_start - timedelta(seconds=30),
            live=_live(pv_w=0.0, load_w=0.0),
            hourly_recommendations=hourly_recommendations,
            last_accumulation_ts=None,
        )
        _accumulate(
            tracker,
            now=slot_start + timedelta(seconds=5),
            live=_live(pv_w=0.0, load_w=0.0),
            hourly_recommendations=hourly_recommendations,
            last_accumulation_ts=ts,
        )

        assert rec.forecast_frozen is True
        assert rec.forecast_pv_kwh == 3.0
        assert rec.forecast_load_kwh == 1.5

        # A post-start planner cycle must not clobber the frozen baseline.
        register_forecasts_from_planner(
            PlannerOutput(
                slots=[
                    PlannedSlot(
                        start=slot_start,
                        end=slot_end,
                        solcast_pv_estimate_kwh=99.0,
                        avg_house_consumption_kwh=99.0,
                    )
                ]
            ),
            tracker,
            now=slot_start + timedelta(seconds=10),
        )
        assert rec.forecast_pv_kwh == 3.0
        assert rec.forecast_load_kwh == 1.5

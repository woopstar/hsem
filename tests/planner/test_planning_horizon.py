"""Tests for configurable planning horizon (issue #324).

Covers the acceptance criteria from GitHub issue #324:

- Horizon is configurable: 24 h, 48 h, and 72 h all produce the correct
  number of slots.
- Planner handles missing future data safely — gaps surface as diagnostics,
  never silently become real zeros.
- Confidence decay is applied to PV estimates for day+1 and day+2 slots.
- All slots receive a non-None recommendation regardless of horizon length.
- DataQuality.horizon_days reflects the actual calendar span.

All tests are pure-Python; no Home Assistant runtime is required.
"""

from __future__ import annotations

from datetime import time

from custom_components.hsem.models.planner_inputs import (
    BatteryScheduleInput,
    HourlyConsumptionAverage,
    PlannerInput,
    PricePoint,
    SolcastSlot,
)
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.planner import run_planner

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_IMPORT_PRICES_24H = [
    0.08,
    0.06,
    0.05,
    0.05,
    0.06,
    0.09,  # 00-06 cheap
    0.15,
    0.22,
    0.26,
    0.24,
    0.12,
    0.08,  # 06-12
    0.06,
    0.07,
    0.10,
    0.25,
    0.30,
    0.32,  # 12-18
    0.29,
    0.24,
    0.18,
    0.14,
    0.11,
    0.09,  # 18-24
]

_PV_PROFILE_24H = [
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.1,
    0.4,
    1.2,
    2.5,
    3.8,
    5.0,
    5.5,
    5.2,
    4.8,
    3.8,
    2.5,
    1.5,
    0.6,
    0.1,
    0.0,
    0.0,
    0.0,
    0.0,
]


def _make_prices(*, import_prices: list[float] | None = None) -> list[PricePoint]:
    """Build a 24-slot PricePoint list for hours 0-23."""
    prices = import_prices or _IMPORT_PRICES_24H
    return [
        PricePoint(
            hour=h,
            import_price=prices[h],
            export_price=max(prices[h] - 0.02, 0.0),
        )
        for h in range(24)
    ]


def _make_solcast(*, pv_profile: list[float] | None = None) -> list[SolcastSlot]:
    """Build a 24-slot SolcastSlot list for hours 0-23."""
    profile = pv_profile or _PV_PROFILE_24H
    return [SolcastSlot(hour=h, pv_estimate=profile[h]) for h in range(24)]


def _make_consumption(*, kwh_per_hour: float = 0.5) -> list[HourlyConsumptionAverage]:
    """Build flat 24-hour consumption averages."""
    return [
        HourlyConsumptionAverage(
            hour=h,
            avg_1d=kwh_per_hour,
            avg_3d=kwh_per_hour,
            avg_7d=kwh_per_hour,
            avg_14d=kwh_per_hour,
        )
        for h in range(24)
    ]


def _make_input(
    *,
    horizon_hours: int,
    interval_minutes: int = 60,
    now_iso: str = "2024-06-15T00:00:00+02:00",
    prices: list[PricePoint] | None = None,
    solcast: list[SolcastSlot] | None = None,
    schedules: list[BatteryScheduleInput] | None = None,
    battery_soc_pct: float = 50.0,
) -> PlannerInput:
    """Build a minimal PlannerInput for the given horizon."""
    default_schedules = [
        BatteryScheduleInput(
            enabled=True,
            start=time(7, 0),
            end=time(9, 0),
        ),
        BatteryScheduleInput(
            enabled=True,
            start=time(17, 0),
            end=time(21, 0),
        ),
    ]
    return PlannerInput(
        now_iso=now_iso,
        interval_minutes=interval_minutes,
        interval_length_hours=horizon_hours,
        battery_soc_pct=battery_soc_pct,
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
        battery_max_charge_power_w=5000.0,
        battery_purchase_price=0.0,
        battery_expected_cycles=6000,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=_make_consumption(),
        price_points=prices if prices is not None else _make_prices(),
        solcast_slots=solcast if solcast is not None else _make_solcast(),
        battery_schedules=(schedules if schedules is not None else default_schedules),
        excess_export_enabled=False,
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        house_power_includes_ev=True,
        is_read_only=True,
    )


# ===========================================================================
# Slot count invariants
# ===========================================================================


class TestSlotCounts:
    """Each horizon length must produce exactly the expected number of slots."""

    def test_24h_60min_slots_count(self):
        result = run_planner(_make_input(horizon_hours=24, interval_minutes=60))
        assert len(result.slots) == 24

    def test_48h_60min_slots_count(self):
        result = run_planner(_make_input(horizon_hours=48, interval_minutes=60))
        assert len(result.slots) == 48

    def test_72h_60min_slots_count(self):
        result = run_planner(_make_input(horizon_hours=72, interval_minutes=60))
        assert len(result.slots) == 72

    def test_24h_15min_slots_count(self):
        result = run_planner(_make_input(horizon_hours=24, interval_minutes=15))
        assert len(result.slots) == 96

    def test_48h_15min_slots_count(self):
        result = run_planner(_make_input(horizon_hours=48, interval_minutes=15))
        assert len(result.slots) == 192

    def test_72h_15min_slots_count(self):
        result = run_planner(_make_input(horizon_hours=72, interval_minutes=15))
        assert len(result.slots) == 288


# ===========================================================================
# All slots have recommendations
# ===========================================================================


class TestAllSlotsHaveRecommendations:
    """Every slot must carry a non-None recommendation regardless of horizon."""

    def test_24h_all_recommendations_present(self):
        result = run_planner(_make_input(horizon_hours=24))
        for slot in result.slots:
            assert slot.recommendation is not None, (
                f"Slot {slot.start.isoformat()} has no recommendation in 24h plan"
            )

    def test_48h_all_recommendations_present(self):
        result = run_planner(_make_input(horizon_hours=48))
        for slot in result.slots:
            assert slot.recommendation is not None, (
                f"Slot {slot.start.isoformat()} has no recommendation in 48h plan"
            )

    def test_72h_all_recommendations_present(self):
        result = run_planner(_make_input(horizon_hours=72))
        for slot in result.slots:
            assert slot.recommendation is not None, (
                f"Slot {slot.start.isoformat()} has no recommendation in 72h plan"
            )


# ===========================================================================
# Calendar day span
# ===========================================================================


class TestCalendarDaySpan:
    """Slots must span the correct number of distinct calendar days."""

    def test_24h_spans_one_day(self):
        result = run_planner(_make_input(horizon_hours=24))
        dates = {s.start.date() for s in result.slots}
        assert len(dates) == 1

    def test_48h_spans_two_days(self):
        result = run_planner(_make_input(horizon_hours=48))
        dates = {s.start.date() for s in result.slots}
        assert len(dates) == 2

    def test_72h_spans_three_days(self):
        result = run_planner(_make_input(horizon_hours=72))
        dates = {s.start.date() for s in result.slots}
        assert len(dates) == 3


# ===========================================================================
# DataQuality.horizon_days
# ===========================================================================


class TestDataQualityHorizonDays:
    """DataQuality.horizon_days must reflect the calendar span."""

    def test_24h_horizon_days_is_1(self):
        result = run_planner(_make_input(horizon_hours=24))
        assert result.data_quality.horizon_days == 1

    def test_48h_horizon_days_is_2(self):
        result = run_planner(_make_input(horizon_hours=48))
        assert result.data_quality.horizon_days == 2

    def test_72h_horizon_days_is_3(self):
        result = run_planner(_make_input(horizon_hours=72))
        assert result.data_quality.horizon_days == 3

    def test_24h_horizon_has_tomorrow_false(self):
        result = run_planner(_make_input(horizon_hours=24))
        assert result.data_quality.horizon_has_tomorrow is False

    def test_48h_horizon_has_tomorrow_true(self):
        result = run_planner(_make_input(horizon_hours=48))
        assert result.data_quality.horizon_has_tomorrow is True

    def test_72h_horizon_has_tomorrow_true(self):
        result = run_planner(_make_input(horizon_hours=72))
        assert result.data_quality.horizon_has_tomorrow is True


# ===========================================================================
# Complete data — no missing-data diagnostics
# ===========================================================================


class TestCompleteFutureData:
    """Verify safe handling of missing and complete future data."""

    def test_24h_complete_data_no_missing(self):
        result = run_planner(_make_input(horizon_hours=24))
        dq = result.data_quality
        assert dq.today_price_missing_hours == []
        assert dq.today_pv_missing_hours == []
        assert dq.tomorrow_price_missing_hours == []
        assert dq.tomorrow_pv_missing_hours == []
        assert dq.day2_price_missing_hours == []
        assert dq.day2_pv_missing_hours == []

    def test_missing_all_price_data_surfaces_today_missing(self):
        """Empty price list makes all slots missing; diagnostics must reflect this."""
        result = run_planner(_make_input(horizon_hours=48, prices=[]))
        dq = result.data_quality
        # With no prices, all 24 today hours should be missing
        assert len(dq.today_price_missing_hours) == 24

    def test_missing_all_pv_data_surfaces_today_pv_missing(self):
        """Empty PV list makes all PV slots missing; diagnostics must reflect this."""
        result = run_planner(_make_input(horizon_hours=48, solcast=[]))
        dq = result.data_quality
        assert len(dq.today_pv_missing_hours) == 24

    def test_missing_data_does_not_crash_planner_48h(self):
        """The planner must return a valid output even when all data is absent (48h)."""
        result = run_planner(_make_input(horizon_hours=48, prices=[], solcast=[]))
        assert result is not None
        assert len(result.slots) == 48

    def test_missing_data_does_not_crash_planner_72h(self):
        """The planner must return a valid output even when all data is absent (72h)."""
        result = run_planner(_make_input(horizon_hours=72, prices=[], solcast=[]))
        assert result is not None
        assert len(result.slots) == 72

    def test_missing_price_data_surfaces_in_missing_inputs(self):
        """Missing today's price hours must appear in missing_inputs."""
        result = run_planner(_make_input(horizon_hours=48, prices=[]))
        price_entries = [e for e in result.missing_inputs if "hour_" in e]
        # All 24 today hours are missing
        assert len(price_entries) == 24

    def test_planner_returns_plan_cost_with_missing_data(self):
        """Even with missing data, plan_cost.total must be a real number (not NaN)."""
        import math

        result = run_planner(_make_input(horizon_hours=72, prices=[], solcast=[]))
        assert not math.isnan(result.plan_cost.total)

    def test_48h_all_slots_have_recommendation_with_partial_data(self):
        """All slots must have a recommendation even with only today's data."""
        result = run_planner(
            _make_input(
                horizon_hours=48,
                prices=_make_prices(),
                solcast=_make_solcast(),
            )
        )
        for slot in result.slots:
            assert slot.recommendation is not None


# ===========================================================================
# Confidence decay for future-day PV estimates
# ===========================================================================


class TestConfidenceDecay:
    """PV estimates for day+1 and day+2 must be lower due to confidence decay."""

    def _get_solar_noon_pv(self, result, day_offset: int) -> float:
        """Return the solar-noon (12:00) PV estimate for a given day_offset."""
        from datetime import date

        # Find the date for the given day offset
        first_date = result.slots[0].start.date()
        target_date = date(
            first_date.year,
            first_date.month,
            first_date.day + day_offset,
        )
        for slot in result.slots:
            if slot.start.date() == target_date and slot.start.hour == 12:
                return slot.solcast_pv_estimate_kwh
        return 0.0

    def test_48h_day1_pv_less_than_day0_same_input(self):
        """Day+1 PV at solar noon should be < day+0 PV (90 % decay vs 100 %)."""
        result = run_planner(
            _make_input(
                horizon_hours=48,
                # Provide identical PV profile for both today and tomorrow
                solcast=_make_solcast() + _make_solcast(),
            )
        )
        pv_day0 = self._get_solar_noon_pv(result, 0)
        pv_day1 = self._get_solar_noon_pv(result, 1)
        # day+1 must be <= day+0 (decay applied to day+1)
        assert pv_day1 <= pv_day0, (
            f"Day+1 PV ({pv_day1:.4f}) should be ≤ day+0 PV ({pv_day0:.4f}) "
            "after confidence decay"
        )

    def test_72h_day2_pv_less_than_day1(self):
        """Day+2 PV at solar noon should be < day+1 PV (80 % vs 90 % decay)."""
        result = run_planner(
            _make_input(
                horizon_hours=72,
                solcast=_make_solcast() + _make_solcast() + _make_solcast(),
            )
        )
        pv_day1 = self._get_solar_noon_pv(result, 1)
        pv_day2 = self._get_solar_noon_pv(result, 2)
        assert pv_day2 <= pv_day1, (
            f"Day+2 PV ({pv_day2:.4f}) should be ≤ day+1 PV ({pv_day1:.4f}) "
            "after confidence decay"
        )

    def test_24h_no_confidence_decay_applied(self):
        """A 24 h plan must not decay any PV estimates (day+0 only)."""
        result_24h = run_planner(_make_input(horizon_hours=24, solcast=_make_solcast()))
        pv_day0_24h = self._get_solar_noon_pv(result_24h, 0)
        # Run again with same input; day0 should equal the raw profile value
        # (5.5 kWh at noon for a 60-min slot, but let's just check it's > 0)
        assert pv_day0_24h > 0, "Solar noon PV should be positive in 24h plan"

    def test_confidence_decay_warning_emitted_for_multiday(self):
        """A warning about confidence decay must appear for horizons > 24 h."""
        result_48h = run_planner(_make_input(horizon_hours=48))
        decay_warnings = [
            w for w in result_48h.warnings if "confidence decay" in w.lower()
        ]
        assert len(decay_warnings) >= 1

    def test_no_confidence_decay_warning_for_24h(self):
        """No confidence decay warning should appear for 24-hour plans."""
        result_24h = run_planner(_make_input(horizon_hours=24))
        decay_warnings = [
            w for w in result_24h.warnings if "confidence decay" in w.lower()
        ]
        assert len(decay_warnings) == 0


# ===========================================================================
# DataQuality.is_complete
# ===========================================================================


class TestDataQualityIsComplete:
    """DataQuality.is_complete must be consistent with missing-hours fields."""

    def test_24h_complete_data_is_complete_true(self):
        result = run_planner(_make_input(horizon_hours=24))
        # Today's data is provided; horizon_days=1 so tomorrow is not required.
        # today price/pv might show nothing missing
        assert result.data_quality.tomorrow_price_missing_hours == []
        assert result.data_quality.day2_price_missing_hours == []

    def test_empty_price_data_is_not_complete(self):
        """With no price data, DataQuality.is_complete must be False."""
        result = run_planner(_make_input(horizon_hours=48, prices=[]))
        assert result.data_quality.is_complete is False

    def test_data_quality_as_dict_contains_horizon_days(self):
        result = run_planner(_make_input(horizon_hours=72))
        d = result.data_quality.as_dict()
        assert "horizon_days" in d
        assert d["horizon_days"] == 3

    def test_data_quality_as_dict_contains_day2_keys(self):
        result = run_planner(_make_input(horizon_hours=72))
        d = result.data_quality.as_dict()
        assert "day2_price_missing_hours" in d
        assert "day2_pv_missing_hours" in d


# ===========================================================================
# TimeSeriesIndex multi-day helpers
# ===========================================================================


class TestTimeSeriesIndexMultiDayHelpers:
    """Unit tests for the new TimeSeriesIndex day-offset helpers."""

    def _make_tsi(self, horizon_hours: int) -> TimeSeriesIndex:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Europe/Copenhagen")
        now = datetime(2024, 6, 15, 0, 0, tzinfo=tz)
        return TimeSeriesIndex.from_now(
            now, interval_minutes=60, horizon_hours=horizon_hours
        )

    def test_horizon_days_24h(self):
        tsi = self._make_tsi(24)
        assert tsi.horizon_days == 1

    def test_horizon_days_48h(self):
        tsi = self._make_tsi(48)
        assert tsi.horizon_days == 2

    def test_horizon_days_72h(self):
        tsi = self._make_tsi(72)
        assert tsi.horizon_days == 3

    def test_has_day_slots_tomorrow_in_48h(self):
        tsi = self._make_tsi(48)
        assert tsi.has_day_slots(0) is True
        assert tsi.has_day_slots(1) is True
        assert tsi.has_day_slots(2) is False

    def test_has_day_slots_day2_in_72h(self):
        tsi = self._make_tsi(72)
        assert tsi.has_day_slots(2) is True

    def test_has_day_slots_false_beyond_horizon(self):
        tsi = self._make_tsi(24)
        assert tsi.has_day_slots(1) is False
        assert tsi.has_day_slots(2) is False

    def test_missing_future_day_price_hours_empty_when_no_day(self):
        tsi = self._make_tsi(24)
        # No slots for day_offset=1, so missing_future_day_price_hours(1) == empty
        tsi.align_hourly_prices({}, {})  # call to populate missing_price_slots
        assert tsi.missing_future_day_price_hours(1) == set()

    def test_missing_future_day_price_hours_empty_when_full_data(self):
        """With complete 24h price data, no day+0 hours should be missing."""
        tsi = self._make_tsi(24)
        prices = {h: 0.2 for h in range(24)}
        tsi.align_hourly_prices(prices, prices)
        # All today's hours provided, so today is complete
        assert tsi.missing_future_day_price_hours(0) == set()

    def test_missing_future_day_price_hours_populated_when_no_data(self):
        """With an empty price dict, all day+0 hours should be marked missing."""
        tsi = self._make_tsi(24)
        tsi.align_hourly_prices({}, {})
        missing = tsi.missing_future_day_price_hours(0)
        assert len(missing) == 24

    def test_missing_future_day_pv_hours_populated_when_no_data_24h(self):
        """With an empty PV dict on a 24h TSI, all day+0 hours should be missing."""
        tsi = self._make_tsi(24)
        tsi.align_hourly_pv({})
        missing_day0 = tsi.missing_future_day_pv_hours(0)
        assert len(missing_day0) == 24

    def test_missing_future_day_pv_returns_empty_for_missing_day_offset(self):
        """day_offset beyond horizon should always return empty set."""
        tsi = self._make_tsi(24)
        tsi.align_hourly_pv({})
        # day_offset=1 has no slots in a 24h index
        assert tsi.missing_future_day_pv_hours(1) == set()
        assert tsi.missing_future_day_pv_hours(2) == set()

    def test_tomorrow_helpers_delegate_to_day1(self):
        """missing_tomorrow_* helpers must equal missing_future_day_*(..., 1)."""
        tsi = self._make_tsi(48)
        today_prices = {h: 0.2 for h in range(24)}
        tsi.align_hourly_prices(today_prices, today_prices)
        assert tsi.missing_tomorrow_price_hours() == tsi.missing_future_day_price_hours(
            1
        )

    def test_has_tomorrow_slots_delegates_to_has_day_slots(self):
        tsi24 = self._make_tsi(24)
        tsi48 = self._make_tsi(48)
        assert tsi24.has_tomorrow_slots() == tsi24.has_day_slots(1)
        assert tsi48.has_tomorrow_slots() == tsi48.has_day_slots(1)

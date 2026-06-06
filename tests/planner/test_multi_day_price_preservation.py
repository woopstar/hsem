"""Regression tests for multi-day price, PV, and consumption preservation.

Issue C1: coordinator's `_build_planner_input` used a ``seen_hours: set[int]``
dedup key keyed only on ``rec.start.hour``.  For a 48-hour horizon this caused
all tomorrow's slots to be silently dropped; the planner only ever saw today's
24 data points and used them cyclically for tomorrow — making multi-day arbitrage
decisions against a fictitious price curve.

Fix: coordinator now uses ``(day_offset, hour)`` dedup keys and populates
``PricePoint``, ``SolcastSlot``, and ``HourlyConsumptionAverage`` with a
``day_offset`` field.  ``slot_population.py`` detects multi-day inputs and
builds ``(day_offset, hour)``-keyed dicts.  ``TimeSeriesIndex.align_hourly_*``
methods accept both key formats.

Acceptance criteria tested here:
1. ``PricePoint``, ``SolcastSlot``, ``HourlyConsumptionAverage`` carry
   ``day_offset`` and default to 0 for backward compatibility.
2. When prices differ between today and tomorrow the planner receives and uses
   the correct per-day values in each slot (not today's value for all slots).
3. When only 24 single-day entries (no ``day_offset``) are provided the planner
   still produces correct output (backward compatibility path).
4. ``TimeSeriesIndex.align_hourly_prices/pv/load`` works with both key formats.
"""

from __future__ import annotations

import math
from datetime import time
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.battery_schedule_input import BatteryScheduleInput
from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.planner import run_planner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TZ = ZoneInfo("Europe/Copenhagen")

_TODAY_PRICES = [0.10 + (h % 4) * 0.02 for h in range(24)]
# Tomorrow's prices are distinctly different (shifted +0.15) so we can assert
# they are NOT mixed up with today's.
_TOMORROW_PRICES = [p + 0.15 for p in _TODAY_PRICES]


def _make_48h_input_with_day_offsets(
    *,
    today_prices: list[float] | None = None,
    tomorrow_prices: list[float] | None = None,
    pv_today: float = 0.0,
    pv_tomorrow: float = 0.0,
    load_today: float = 0.5,
    load_tomorrow: float = 0.5,
) -> PlannerInput:
    """Build a 48-hour ``PlannerInput`` with explicit ``day_offset`` fields."""
    t = today_prices or _TODAY_PRICES
    tm = tomorrow_prices or _TOMORROW_PRICES

    price_points: list[PricePoint] = [
        PricePoint(
            hour=h,
            import_price=t[h],
            export_price=max(t[h] - 0.02, 0.0),
            day_offset=0,
        )
        for h in range(24)
    ] + [
        PricePoint(
            hour=h,
            import_price=tm[h],
            export_price=max(tm[h] - 0.02, 0.0),
            day_offset=1,
        )
        for h in range(24)
    ]

    solcast_slots: list[SolcastSlot] = [
        SolcastSlot(hour=h, pv_estimate=pv_today, day_offset=0) for h in range(24)
    ] + [SolcastSlot(hour=h, pv_estimate=pv_tomorrow, day_offset=1) for h in range(24)]

    consumption_averages: list[HourlyConsumptionAverage] = [
        HourlyConsumptionAverage(
            hour=h,
            avg_1d=load_today,
            avg_3d=load_today,
            avg_7d=load_today,
            avg_14d=load_today,
            day_offset=0,
        )
        for h in range(24)
    ] + [
        HourlyConsumptionAverage(
            hour=h,
            avg_1d=load_tomorrow,
            avg_3d=load_tomorrow,
            avg_7d=load_tomorrow,
            avg_14d=load_tomorrow,
            day_offset=1,
        )
        for h in range(24)
    ]

    return PlannerInput(
        now_iso="2024-06-15T00:00:00+02:00",
        interval_minutes=60,
        interval_length_hours=48,
        battery_soc_pct=50.0,
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_charge_power_w=5000.0,
        battery_purchase_price=0.0,
        battery_expected_cycles=6000,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=consumption_averages,
        price_points=price_points,
        solcast_slots=solcast_slots,
        battery_schedules=[
            BatteryScheduleInput(
                enabled=True,
                start=time(7, 0),
                end=time(9, 0),
            )
        ],
        excess_export_enabled=False,
        excess_export_discharge_buffer_pct=10.0,
        excess_export_price_threshold=0.10,
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        house_power_includes_ev=True,
        is_read_only=True,
    )


# ---------------------------------------------------------------------------
# Unit tests for dataclass defaults
# ---------------------------------------------------------------------------


class TestDataclassDefaults:
    """PricePoint, SolcastSlot, and HourlyConsumptionAverage default day_offset=0."""

    def test_price_point_default_day_offset(self):
        pp = PricePoint(hour=12, import_price=0.10, export_price=0.08)
        assert pp.day_offset == 0

    def test_solcast_slot_default_day_offset(self):
        sc = SolcastSlot(hour=12, pv_estimate=1.5)
        assert sc.day_offset == 0

    def test_hourly_consumption_average_default_day_offset(self):
        ca = HourlyConsumptionAverage(hour=12, avg_1d=0.5)
        assert ca.day_offset == 0

    def test_price_point_explicit_day_offset(self):
        pp = PricePoint(hour=0, import_price=0.20, export_price=0.18, day_offset=1)
        assert pp.day_offset == 1


# ---------------------------------------------------------------------------
# TimeSeriesIndex alignment with (day_offset, hour) keyed dicts
# ---------------------------------------------------------------------------


class TestTimeSeriesIndexMultiDayAlignment:
    """TimeSeriesIndex.align_hourly_* must accept (day_offset, hour) keyed dicts."""

    def _make_tsi(self) -> TimeSeriesIndex:
        from datetime import datetime

        now = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)
        return TimeSeriesIndex.from_now(now, interval_minutes=60, horizon_hours=48)

    def test_align_prices_day_hour_keys_distinct_days(self):
        """Slots on day 0 get today's price; slots on day 1 get tomorrow's price."""
        tsi = self._make_tsi()
        today_import = 0.10
        tomorrow_import = 0.30  # Distinctly higher — must not bleed into day-0 slots.
        imp = {(0, h): today_import for h in range(24)}
        imp.update({(1, h): tomorrow_import for h in range(24)})
        exp = {(0, h): 0.08 for h in range(24)}
        exp.update({(1, h): 0.28 for h in range(24)})

        aligned_imp, _ = tsi.align_hourly_prices(imp, exp)

        # First 24 slots (day_offset=0) must carry today's price.
        for i in range(24):
            assert aligned_imp[i] == pytest.approx(today_import), (
                f"slot {i} (day 0): expected {today_import}, got {aligned_imp[i]}"
            )
        # Next 24 slots (day_offset=1) must carry tomorrow's price.
        for i in range(24, 48):
            assert aligned_imp[i] == pytest.approx(tomorrow_import), (
                f"slot {i} (day 1): expected {tomorrow_import}, got {aligned_imp[i]}"
            )

    def test_align_pv_day_hour_keys_distinct_days(self):
        tsi = self._make_tsi()
        pv = {(0, h): 1.0 for h in range(24)}
        pv.update({(1, h): 3.0 for h in range(24)})

        aligned = tsi.align_hourly_pv(pv)

        # 60-min slots: slot_fraction = 1.0, so value passes through unchanged.
        for i in range(24):
            assert aligned[i] == pytest.approx(1.0), f"slot {i} (day 0)"
        for i in range(24, 48):
            assert aligned[i] == pytest.approx(3.0), f"slot {i} (day 1)"

    def test_align_load_day_hour_keys_distinct_days(self):
        tsi = self._make_tsi()
        load = {(0, h): 0.5 for h in range(24)}
        load.update({(1, h): 2.0 for h in range(24)})

        aligned = tsi.align_hourly_load(load)

        for i in range(24):
            assert aligned[i] == pytest.approx(0.5), f"slot {i} (day 0)"
        for i in range(24, 48):
            assert aligned[i] == pytest.approx(2.0), f"slot {i} (day 1)"

    def test_align_prices_hour_only_keys_backward_compat(self):
        """Hour-only (legacy) keys still work — cyclical lookup, no day separation."""
        tsi = self._make_tsi()
        imp = dict.fromkeys(range(24), 0.10)
        exp = dict.fromkeys(range(24), 0.08)

        aligned_imp, _ = tsi.align_hourly_prices(imp, exp)

        # All 48 slots receive the same cyclical value.
        for i, val in enumerate(aligned_imp):
            assert not math.isnan(val), f"slot {i} should not be NaN"
            assert val == pytest.approx(0.10)

    def test_missing_day_slots_reported(self):
        """When (day_offset=1, hour=X) is absent, those keys enter missing_slots."""
        tsi = self._make_tsi()
        # Only provide day-0 data.
        imp = {(0, h): 0.10 for h in range(24)}
        exp = {(0, h): 0.08 for h in range(24)}

        tsi.align_hourly_prices(imp, exp)

        # Day-1 slots should all be marked missing.
        missing_day1 = {k for k in tsi.missing_price_slots if k.day_offset == 1}
        assert len(missing_day1) == 24, (
            f"Expected 24 missing day-1 price slots, got {len(missing_day1)}"
        )


# ---------------------------------------------------------------------------
# Planner integration: slot prices must differ between day 0 and day 1
# ---------------------------------------------------------------------------


class TestPlannerMultiDayPriceIsolation:
    """The planner must use per-day prices, not the same value for all 48 slots."""

    def test_day0_slots_carry_today_prices_not_tomorrow(self):
        """Day-0 planner slots must reflect today's import price, not tomorrow's."""
        inp = _make_48h_input_with_day_offsets(
            today_prices=_TODAY_PRICES,
            tomorrow_prices=_TOMORROW_PRICES,
        )
        result = run_planner(inp)

        day0_date = result.slots[0].start.date()
        day0_slots = [s for s in result.slots if s.start.date() == day0_date]

        for slot in day0_slots:
            h = slot.start.hour
            # Day-0 price must match today's price, not tomorrow's.
            assert slot.price.import_price == pytest.approx(_TODAY_PRICES[h]), (
                f"Day-0 slot at {slot.start.isoformat()}: "
                f"import_price={slot.price.import_price} "
                f"expected={_TODAY_PRICES[h]}"
            )

    def test_day1_slots_carry_tomorrow_prices_not_today(self):
        """Day-1 planner slots must reflect tomorrow's import price, not today's."""
        inp = _make_48h_input_with_day_offsets(
            today_prices=_TODAY_PRICES,
            tomorrow_prices=_TOMORROW_PRICES,
        )
        result = run_planner(inp)

        day0_date = result.slots[0].start.date()
        day1_slots = [s for s in result.slots if s.start.date() != day0_date]
        assert day1_slots, "No day-1 slots found — horizon too short?"

        for slot in day1_slots:
            h = slot.start.hour
            # Day-1 price must match tomorrow's price, not today's.
            assert slot.price.import_price == pytest.approx(_TOMORROW_PRICES[h]), (
                f"Day-1 slot at {slot.start.isoformat()}: "
                f"import_price={slot.price.import_price} "
                f"expected={_TOMORROW_PRICES[h]}"
            )

    def test_prices_not_identical_across_days(self):
        """When today and tomorrow have different prices the planner must see them."""
        inp = _make_48h_input_with_day_offsets(
            today_prices=_TODAY_PRICES,
            tomorrow_prices=_TOMORROW_PRICES,
        )
        result = run_planner(inp)

        day0_date = result.slots[0].start.date()
        day0_import = {
            s.start.hour: s.price.import_price
            for s in result.slots
            if s.start.date() == day0_date
        }
        day1_import = {
            s.start.hour: s.price.import_price
            for s in result.slots
            if s.start.date() != day0_date
        }

        # Every hour must differ between day 0 and day 1.
        for h in range(24):
            assert day0_import.get(h) != pytest.approx(day1_import.get(h)), (
                f"Hour {h}: day-0 and day-1 prices are identical "
                f"({day0_import.get(h)}) — dedup bug still present"
            )

    def test_pv_not_identical_across_days(self):
        """When today and tomorrow have different PV the planner must see them."""
        inp = _make_48h_input_with_day_offsets(pv_today=0.5, pv_tomorrow=2.0)
        result = run_planner(inp)

        day0_date = result.slots[0].start.date()
        day0_pv = [
            s.solcast_pv_estimate_kwh
            for s in result.slots
            if s.start.date() == day0_date
        ]
        day1_pv = [
            s.solcast_pv_estimate_kwh
            for s in result.slots
            if s.start.date() != day0_date
        ]

        avg_day0 = sum(day0_pv) / len(day0_pv)
        avg_day1 = sum(day1_pv) / len(day1_pv)

        assert avg_day1 > avg_day0 + 0.5, (
            f"Day-1 PV average ({avg_day1:.3f}) should be higher than day-0 "
            f"({avg_day0:.3f}) — PV dedup bug may still be present"
        )

    def test_backward_compat_24h_single_day_still_works(self):
        """A legacy 24-entry (no day_offset) input must still produce correct output."""
        # Classic 24-entry input without day_offset (same as test_48h_second_day.py).
        prices = [
            PricePoint(hour=h, import_price=0.10, export_price=0.08) for h in range(24)
        ]
        solar = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
        consumption = [
            HourlyConsumptionAverage(
                hour=h, avg_1d=0.5, avg_3d=0.5, avg_7d=0.5, avg_14d=0.5
            )
            for h in range(24)
        ]

        inp = PlannerInput(
            now_iso="2024-06-15T00:00:00+02:00",
            interval_minutes=60,
            interval_length_hours=48,
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_charge_power_w=5000.0,
            battery_purchase_price=0.0,
            battery_expected_cycles=6000,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=consumption,
            price_points=prices,
            solcast_slots=solar,
            battery_schedules=[],
            excess_export_enabled=False,
            excess_export_discharge_buffer_pct=10.0,
            excess_export_price_threshold=0.10,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
            house_power_includes_ev=True,
            is_read_only=True,
        )

        result = run_planner(inp)
        assert len(result.slots) == 48
        for slot in result.slots:
            assert slot.recommendation is not None
            # With legacy data the cyclical fallback should give a valid price.
            assert not math.isnan(slot.price.import_price)

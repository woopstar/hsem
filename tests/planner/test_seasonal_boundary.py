"""Regression tests: seasonal fill must use each slot's own calendar month.

Before the fix, ``apply_optimization_strategy`` computed ``current_month =
now.month`` once and applied it to every slot in the horizon.  A 48-hour plan
starting on Aug 31 (summer) would classify Sep 1 slots (winter) as summer,
assigning ``BatteriesDischargeMode`` instead of ``BatteriesWaitMode``.

After the fix, each slot's month is derived from ``rec.start`` (via
``as_tz(rec.start, now.tzinfo).month``), so the seasonal boundary is
respected mid-horizon.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.hsem.models.battery_schedule_input import BatteryScheduleInput
from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.discharge_scheduler import (
    apply_optimization_strategy,
)
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TZ = ZoneInfo("Europe/Copenhagen")
# September is winter for this test; August is summer.
_MONTHS_WINTER = [1, 2, 3, 4, 9, 10, 11, 12]


def _make_slot(start: datetime, net_consumption: float = 0.5) -> PlannedSlot:
    """Return a minimal :class:`PlannedSlot` for unit-testing the seasonal fill."""
    return PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=0.20, export_price=0.05),
        estimated_net_consumption_kwh=net_consumption,
    )


def _make_boundary_input() -> PlannerInput:
    """48-hour plan starting Aug 31 (summer) crossing into Sep 1 (winter).

    Flat prices (import > export), no PV, disabled schedules — the seasonal
    fill is the only recommendation source.
    """
    prices = [
        PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(24)
    ]
    solar = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
    consumption = [
        HourlyConsumptionAverage(
            hour=h, avg_1d=0.5, avg_3d=0.5, avg_7d=0.5, avg_14d=0.5
        )
        for h in range(24)
    ]
    disabled_schedules = [
        BatteryScheduleInput(enabled=False, start=time(7, 0), end=time(9, 0)),
        BatteryScheduleInput(enabled=False, start=time(17, 0), end=time(21, 0)),
    ]

    return PlannerInput(
        now_iso="2024-08-31T00:00:00+02:00",
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
        battery_schedules=disabled_schedules,
        excess_export_enabled=False,
        excess_export_discharge_buffer_pct=10.0,
        excess_export_price_threshold=0.10,
        months_winter=_MONTHS_WINTER,
        house_power_includes_ev=True,
        is_read_only=True,
    )


# ===========================================================================
# Unit tests — apply_optimization_strategy directly
# ===========================================================================


class TestSeasonalBoundaryUnit:
    """``apply_optimization_strategy`` must use each slot's own calendar month."""

    def test_aug31_summer_sep1_winter(self):
        """Slots on Aug 31 (summer) → Discharge; Sep 1 (winter) → Wait.

        ``now`` is Aug 31 (month 8, summer).  The old code used
        ``now.month`` for every slot, so Sep 1 slots (month 9, winter)
        would incorrectly get ``BatteriesDischargeMode``.
        """
        now = datetime(2024, 8, 31, 0, 0, tzinfo=_TZ)

        aug31_slots = [_make_slot(now + timedelta(hours=h)) for h in range(24)]
        sep1_slots = [_make_slot(now + timedelta(hours=24 + h)) for h in range(24)]
        slots = aug31_slots + sep1_slots

        apply_optimization_strategy(
            slots=slots,
            now=now,
            current_capacity=5.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=_MONTHS_WINTER,
        )

        for slot in aug31_slots:
            assert (
                slot.recommendation == Recommendations.BatteriesDischargeMode.value
            ), (
                f"Aug 31 slot {slot.start.isoformat()} should be "
                f"BatteriesDischargeMode (summer), got {slot.recommendation}"
            )

        for slot in sep1_slots:
            assert slot.recommendation == Recommendations.BatteriesWaitMode.value, (
                f"Sep 1 slot {slot.start.isoformat()} should be "
                f"BatteriesWaitMode (winter), got {slot.recommendation}"
            )

    def test_single_day_no_regression(self):
        """A single-day plan (no boundary crossing) must behave as before.

        All slots on Aug 31 (summer) → ``BatteriesDischargeMode``.
        """
        now = datetime(2024, 8, 31, 0, 0, tzinfo=_TZ)
        slots = [_make_slot(now + timedelta(hours=h)) for h in range(24)]

        apply_optimization_strategy(
            slots=slots,
            now=now,
            current_capacity=5.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=_MONTHS_WINTER,
        )

        for slot in slots:
            assert (
                slot.recommendation == Recommendations.BatteriesDischargeMode.value
            ), (
                f"Aug 31 slot {slot.start.isoformat()} should be "
                f"BatteriesDischargeMode (summer), got {slot.recommendation}"
            )

    def test_winter_to_summer_boundary(self):
        """Slots crossing from winter (Sep) to summer (Oct is not in this
        test, but we verify the reverse: Sep winter → Oct would be summer).

        Here we test Sep 30 (winter) → Oct 1 (winter by default, but we
        override to make Oct summer) to verify the boundary works in both
        directions.
        """
        # Make October summer: months_winter = [1,2,3,4,9,11,12]
        months_winter = [1, 2, 3, 4, 9, 11, 12]
        now = datetime(2024, 9, 30, 0, 0, tzinfo=_TZ)

        sep30_slots = [_make_slot(now + timedelta(hours=h)) for h in range(24)]
        oct1_slots = [_make_slot(now + timedelta(hours=24 + h)) for h in range(24)]
        slots = sep30_slots + oct1_slots

        apply_optimization_strategy(
            slots=slots,
            now=now,
            current_capacity=5.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=months_winter,
        )

        for slot in sep30_slots:
            assert slot.recommendation == Recommendations.BatteriesWaitMode.value, (
                f"Sep 30 slot {slot.start.isoformat()} should be "
                f"BatteriesWaitMode (winter), got {slot.recommendation}"
            )

        for slot in oct1_slots:
            assert (
                slot.recommendation == Recommendations.BatteriesDischargeMode.value
            ), (
                f"Oct 1 slot {slot.start.isoformat()} should be "
                f"BatteriesDischargeMode (summer), got {slot.recommendation}"
            )


# ===========================================================================
# Integration tests — full run_planner pipeline
# ===========================================================================


class TestSeasonalBoundaryIntegration:
    """The full planner pipeline must respect the seasonal boundary."""

    def test_48h_boundary_produces_wait_mode_on_winter_day(self):
        """A 48-hour plan crossing Aug 31 → Sep 1 must produce
        ``BatteriesWaitMode`` slots on the winter day (Sep 1).

        Before the fix, all slots were classified as summer (Aug), so
        Sep 1 slots would all be ``BatteriesDischargeMode``.
        """
        result = run_planner(_make_boundary_input())
        assert len(result.slots) == 48

        sep1_slots = [s for s in result.slots if s.start.month == 9]
        assert sep1_slots, "Expected Sep 1 slots in the 48-hour horizon"

        wait_slots = [
            s
            for s in sep1_slots
            if s.recommendation == Recommendations.BatteriesWaitMode.value
        ]
        assert wait_slots, (
            "Sep 1 (winter) slots must include BatteriesWaitMode from the "
            "seasonal fill.  Before the fix, all slots used now.month "
            f"(Aug = summer) and got BatteriesDischargeMode.  "
            f"Sep 1 recommendations: "
            f"{[(s.start.hour, s.recommendation) for s in sep1_slots]}"
        )

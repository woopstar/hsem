"""Tests for the cross-occurrence battery capacity cap in charge_scheduler.py.

Bug A fix: _apply_grid_charge must return the energy it assigned so that
total_charged is correctly accumulated across discharge-window occurrences.
Without this fix, the capacity cap (usable_kwh - current_kwh) is never
exhausted by grid charging, causing far more charge slots than the battery
can physically hold.

Acceptance criteria verified here
-----------------------------------
- Total batteries_charged_kwh across all slots never exceeds
  usable_kwh - current_kwh after apply_charge_schedules completes.

All tests are synchronous with no Home Assistant imports.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.hsem.models.planner_inputs import BatteryScheduleInput
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.planner.charge_scheduler import apply_charge_schedules
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2024, 6, 15, 8, 0, tzinfo=_TZ)


def _slot(
    *,
    hour: int,
    minute: int = 0,
    import_price: float = 0.30,
    export_price: float = 0.05,
    net_consumption: float = 0.0,
    recommendation: str | None = None,
) -> PlannedSlot:
    """Build a minimal PlannedSlot for charge-scheduler tests."""
    start = datetime(2024, 6, 15, hour, minute, tzinfo=_TZ)
    return PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=import_price, export_price=export_price),
        estimated_net_consumption_kwh=net_consumption,
        recommendation=recommendation,
    )


class TestGridChargeCapacityCap:
    """apply_charge_schedules must not assign more charge energy than the
    battery can hold, even when multiple discharge-window occurrences exist."""

    def test_grid_charge_does_not_exceed_battery_capacity(self):
        """With two discharge-window occurrences each needing 8 kWh and a
        battery headroom of 8 kWh (usable_kwh=10, current_kwh=2), the total
        charged energy across all slots must not exceed 8.0 kWh."""
        slots: list[PlannedSlot] = []

        for h in range(8, 10):
            slots.append(_slot(hour=h, import_price=0.10, net_consumption=0.0))

        for h in range(10, 12):
            slots.append(
                _slot(
                    hour=h,
                    import_price=1.50,
                    net_consumption=4.0,
                    recommendation=Recommendations.BatteriesDischargeMode.value,
                )
            )

        for h in range(12, 14):
            slots.append(_slot(hour=h, import_price=0.10, net_consumption=0.0))

        for h in range(14, 16):
            slots.append(
                _slot(
                    hour=h,
                    import_price=1.50,
                    net_consumption=4.0,
                    recommendation=Recommendations.BatteriesDischargeMode.value,
                )
            )

        sched = BatteryScheduleInput(
            enabled=True,
            start=datetime(2024, 6, 15, 10, 0, tzinfo=_TZ).time(),
            end=datetime(2024, 6, 15, 12, 0, tzinfo=_TZ).time(),
        )
        sched._occurrences = [
            (
                datetime(2024, 6, 15, 10, 0, tzinfo=_TZ),
                datetime(2024, 6, 15, 12, 0, tzinfo=_TZ),
                8.0,
                1.50,
            ),
            (
                datetime(2024, 6, 15, 14, 0, tzinfo=_TZ),
                datetime(2024, 6, 15, 16, 0, tzinfo=_TZ),
                8.0,
                1.50,
            ),
        ]

        apply_charge_schedules(
            slots=slots,
            battery_schedules=[sched],
            now=_NOW,
            max_charge_per_interval=5.0,
            current_kwh=2.0,
            usable_kwh=10.0,
            cycle_cost_per_kwh=0.0,
            recommended_threshold=0.0,
        )

        total_charged = sum(s.batteries_charged_kwh for s in slots)
        headroom = 10.0 - 2.0
        assert total_charged >= 0.0
        assert total_charged - 1e-9 < headroom

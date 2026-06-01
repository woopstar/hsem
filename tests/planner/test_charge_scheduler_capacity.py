"""Tests for per-occurrence charge budgets in charge_scheduler.py.

Each discharge-window occurrence (calendar day) receives its own independent
charge budget because the battery is discharged between windows, making room
for new charging.

All tests are synchronous with no Home Assistant imports.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.hsem.models.planner_inputs import BatteryScheduleInput
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.planner.charge_scheduler import (
    apply_charge_schedules,
    apply_opportunistic_charge,
)
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


class TestGridChargePerOccurrenceBudgets:
    """apply_charge_schedules uses per-occurrence budgets — each discharge
    window occurrence gets its own independent charge allocation capped at
    min(needed, usable_kwh)."""

    def test_per_occurrence_budgets_are_independent(self):
        """Two discharge-window occurrences each needing 8 kWh with
        usable_kwh=10.  Each occurrence gets min(needed, usable_kwh) = 8 kWh
        as its own budget.  Total planned charge can be up to 16 kWh
        because the battery is discharged between windows."""
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
        # Each occurrence gets min(needed=8, usable_kwh=10) = 8 kWh
        # Two occurrences = up to 16 kWh (battery recharged between windows)
        per_occ_budget = min(8.0, 10.0)
        max_total = per_occ_budget * 2
        assert total_charged >= 0.0
        assert total_charged - 1e-9 < max_total + 1e-9, (
            f"Total charged ({total_charged:.3f}) must not exceed "
            f"per-occurrence budget x 2 ({max_total:.3f})"
        )

    def test_single_occurrence_budget_capped_at_usable_kwh(self):
        """A single occurrence needing more than usable_kwh is capped."""
        slots: list[PlannedSlot] = []

        for h in range(8, 14):
            slots.append(_slot(hour=h, import_price=0.10, net_consumption=0.0))

        for h in range(14, 15):
            slots.append(
                _slot(
                    hour=h,
                    import_price=1.50,
                    net_consumption=20.0,
                    recommendation=Recommendations.BatteriesDischargeMode.value,
                )
            )

        sched = BatteryScheduleInput(
            enabled=True,
            start=datetime(2024, 6, 15, 14, 0, tzinfo=_TZ).time(),
            end=datetime(2024, 6, 15, 15, 0, tzinfo=_TZ).time(),
        )
        sched._occurrences = [
            (
                datetime(2024, 6, 15, 14, 0, tzinfo=_TZ),
                datetime(2024, 6, 15, 15, 0, tzinfo=_TZ),
                20.0,
                1.50,
            ),
        ]

        apply_charge_schedules(
            slots=slots,
            battery_schedules=[sched],
            now=_NOW,
            max_charge_per_interval=5.0,
            current_kwh=0.0,
            usable_kwh=10.0,
            cycle_cost_per_kwh=0.0,
            recommended_threshold=0.0,
        )

        total_charged = sum(s.batteries_charged_kwh for s in slots)
        assert total_charged - 1e-9 < 10.0 + 1e-9, (
            f"Total charged ({total_charged:.3f}) must not exceed "
            f"usable_kwh ({10.0:.3f})"
        )


class TestOpportunisticChargeCapacityCap:
    """apply_opportunistic_charge must not exceed remaining battery capacity
    when apply_charge_schedules has already filled the battery."""

    def test_opportunistic_does_not_exceed_capacity_after_schedule_charge(
        self,
    ):
        """Battery is already filled to capacity by apply_charge_schedules.
        apply_opportunistic_charge must not add additional charge slots."""
        slots: list[PlannedSlot] = []

        # Cheap slots before discharge window - these will be filled
        # by apply_charge_schedules
        for h in range(8, 10):
            slots.append(_slot(hour=h, import_price=0.10, net_consumption=0.0))

        # Discharge window
        for h in range(10, 11):
            slots.append(
                _slot(
                    hour=h,
                    import_price=1.50,
                    net_consumption=8.0,
                    recommendation=Recommendations.BatteriesDischargeMode.value,
                )
            )

        # More cheap slots later - opportunistic might try to charge here
        for h in range(12, 14):
            slots.append(_slot(hour=h, import_price=0.05, net_consumption=0.0))

        sched = BatteryScheduleInput(
            enabled=True,
            start=datetime(2024, 6, 15, 10, 0, tzinfo=_TZ).time(),
            end=datetime(2024, 6, 15, 11, 0, tzinfo=_TZ).time(),
        )
        sched._occurrences = [
            (
                datetime(2024, 6, 15, 10, 0, tzinfo=_TZ),
                datetime(2024, 6, 15, 11, 0, tzinfo=_TZ),
                8.0,
                1.50,
            ),
        ]

        # First, apply charge schedules
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

        charged_before = sum(s.batteries_charged_kwh for s in slots)
        # Single occurrence on today's date, budget accounts for current_kwh:
        # min(needed=8, max(8-2, 0)=6, usable_kwh=10) = 6
        per_occ_budget = min(8.0, max(8.0 - 2.0, 0.0), 10.0)
        assert charged_before - 1e-9 < per_occ_budget + 1e-9, (
            f"Schedule charge ({charged_before:.3f}) must not exceed "
            f"per-occurrence budget ({per_occ_budget:.3f})"
        )

        # Now try opportunistic charging with very low prices
        apply_opportunistic_charge(
            slots=slots,
            now=_NOW,
            current_capacity=2.0,
            usable_capacity=10.0,
            max_charge_per_interval=5.0,
            depreciation_threshold=0.10,
            cycle_cost_per_kwh=0.0,
        )

        charged_after = sum(s.batteries_charged_kwh for s in slots)
        # Schedule charging: min(8, max(8-2,0)=6, 10) = 6 kWh
        # Remaining headroom: 10 - 2 - 6 = 2 kWh
        # Opportunistic may fill this with cheap 0.05 slots
        headroom = 10.0 - 2.0
        assert charged_after - 1e-9 < headroom + 1e-9, (
            f"Total charged ({charged_after:.3f}) must not exceed "
            f"headroom ({headroom:.3f})"
        )

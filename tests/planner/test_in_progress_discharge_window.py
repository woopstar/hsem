"""Regression tests for in-progress discharge-window scheduling (issue #1074)."""

from datetime import UTC, datetime, time, timedelta

import pytest

from custom_components.hsem.models.battery_schedule_input import BatteryScheduleInput
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.candidate_selector import (
    replacement_price_from_next_discharge,
)
from custom_components.hsem.planner.discharge_scheduler import apply_discharge_schedules
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.time_windows import window_containing_or_next

_BASE = datetime(2026, 9, 18, tzinfo=UTC)
_INTERVAL = timedelta(minutes=15)


def _slots(start: datetime, count: int) -> list[PlannedSlot]:
    """Build quarter-hour slots with tonight's peak priced above later windows."""
    slots: list[PlannedSlot] = []
    for index in range(count):
        slot_start = start + index * _INTERVAL
        is_tonight_peak = (
            slot_start.date() == _BASE.date() and 19 <= slot_start.hour < 21
        )
        import_price = 1.881 if is_tonight_peak else 0.3555
        slots.append(
            PlannedSlot(
                start=slot_start,
                end=slot_start + _INTERVAL,
                price=SlotPrice(import_price=import_price, export_price=0.0),
                estimated_net_consumption_kwh=0.25,
            )
        )
    return slots


def _schedule(start: time, end: time) -> BatteryScheduleInput:
    """Return one enabled discharge schedule."""
    return BatteryScheduleInput(enabled=True, start=start, end=end)


def test_replacement_price_stable_after_discharge_window_starts() -> None:
    """Tonight's expensive window remains first across its start boundary."""
    before_now = _BASE.replace(hour=16, minute=46)
    during_now = _BASE.replace(hour=18)
    before_slots = _slots(_BASE, 48 * 4)
    during_slots = _slots(_BASE, 48 * 4)
    before_schedule = _schedule(time(17), time(23, 30))
    during_schedule = _schedule(time(17), time(23, 30))

    apply_discharge_schedules(before_slots, [before_schedule], before_now)
    apply_discharge_schedules(during_slots, [during_schedule], during_now)

    before_price = replacement_price_from_next_discharge(
        before_slots, before_now, top_n=8, interval_minutes=15
    )
    during_price = replacement_price_from_next_discharge(
        during_slots, during_now, top_n=8, interval_minutes=15
    )

    assert before_price == pytest.approx(1.881)
    assert during_price == pytest.approx(before_price, abs=1e-6)


def test_in_progress_occurrence_contains_only_remaining_slots() -> None:
    """Elapsed slots are excluded from labels and the occurrence's needed energy."""
    now = _BASE.replace(hour=18)
    slots = _slots(_BASE, 48 * 4)
    schedule = _schedule(time(17), time(23, 30))

    apply_discharge_schedules(slots, [schedule], now)

    occurrence_start, occurrence_end, needed, _average_price = schedule._occurrences[0]
    assert occurrence_start == _BASE.replace(hour=17)
    assert occurrence_end == _BASE.replace(hour=23, minute=30)
    assert needed == pytest.approx(5.5)
    assert schedule._needed_capacity >= needed

    elapsed = next(slot for slot in slots if slot.start == _BASE.replace(hour=17))
    remaining = next(slot for slot in slots if slot.start == now)
    assert elapsed.recommendation is None
    assert remaining.recommendation == Recommendations.BatteriesDischargeMode.value


def test_cross_midnight_window_containing_now_is_retained() -> None:
    """After midnight, resolve the occurrence that started the previous day."""
    now = (_BASE + timedelta(days=1)).replace(hour=0, minute=30)
    slots = _slots(_BASE.replace(hour=22), 30)
    schedule = _schedule(time(23), time(2))

    start, end = window_containing_or_next(now, schedule.start, schedule.end)
    assert start == _BASE.replace(hour=23)
    assert end == (_BASE + timedelta(days=1)).replace(hour=2)

    apply_discharge_schedules(slots, [schedule], now)

    occurrence_start, occurrence_end, needed, _average_price = schedule._occurrences[0]
    assert occurrence_start == start
    assert occurrence_end == end
    assert needed == pytest.approx(1.5)

    elapsed = next(
        slot
        for slot in slots
        if slot.start == (_BASE + timedelta(days=1)).replace(hour=0)
    )
    remaining = next(slot for slot in slots if slot.start == now)
    assert elapsed.recommendation is None
    assert remaining.recommendation == Recommendations.BatteriesDischargeMode.value


def test_cross_midnight_window_end_is_exclusive() -> None:
    """At the end boundary, the helper advances to the next occurrence."""
    now = (_BASE + timedelta(days=1)).replace(hour=2)

    start, end = window_containing_or_next(now, time(23), time(2))

    assert start == (_BASE + timedelta(days=1)).replace(hour=23)
    assert end == (_BASE + timedelta(days=2)).replace(hour=2)

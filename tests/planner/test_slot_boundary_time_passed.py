"""Regression tests for issue #1174: ``now`` exactly on a slot boundary.

A slot is the half-open interval ``[start, end)``.  The MILP
(``future_slot_indices``), ``simulate_soc`` and the plan consistency gate
all treat a slot with ``end == now`` as past, but ``mark_time_passed`` used
a strict ``end < now``.  On a boundary the slot that had just ended was
therefore left out of the solve (0 kWh) yet kept a label from the
heuristic passes, and ``check_plan_self_consistency`` reported it as an
HSEM bug.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.slot_population import mark_time_passed
from custom_components.hsem.utils.recommendations import Recommendations
from tests.planner.fixtures import make_summer_day_input, make_winter_day_input

_TZ = ZoneInfo("Europe/Copenhagen")
_TIME_PASSED = Recommendations.TimePassed.value


def _run(
    make: Callable[[], PlannerInput], now: datetime, interval: int
) -> tuple[list[PlannedSlot], str | None, list[str]]:
    inp = dataclasses.replace(
        make(),
        now_iso=now.isoformat(),
        time_zone="Europe/Copenhagen",
        interval_minutes=interval,
    )
    out = run_planner(inp)
    return out.slots, out.current_recommendation, out.warnings


class TestMarkTimePassed:
    """``mark_time_passed`` uses the half-open slot rule."""

    def _slots(self) -> list[PlannedSlot]:
        start = datetime(2026, 6, 1, 11, 0, tzinfo=_TZ)
        step = timedelta(minutes=15)
        return [
            PlannedSlot(start=start + i * step, end=start + (i + 1) * step)
            for i in range(8)
        ]

    @pytest.mark.parametrize(
        ("offset", "expected"),
        [
            pytest.param(timedelta(seconds=-1), 3, id="just_before"),
            pytest.param(timedelta(0), 4, id="on_boundary"),
            pytest.param(timedelta(seconds=1), 4, id="just_after"),
        ],
    )
    def test_slot_ending_at_now_is_passed(
        self, offset: timedelta, expected: int
    ) -> None:
        slots = self._slots()
        now = datetime(2026, 6, 1, 12, 0, tzinfo=_TZ) + offset

        mark_time_passed(slots, now)

        passed = [s.recommendation == _TIME_PASSED for s in slots]
        assert passed == [i < expected for i in range(len(slots))]

    def test_live_slot_is_not_marked(self) -> None:
        slots = self._slots()

        mark_time_passed(slots, datetime(2026, 6, 1, 12, 0, tzinfo=_TZ))

        live = slots[4]
        assert live.start == datetime(2026, 6, 1, 12, 0, tzinfo=_TZ)
        assert live.recommendation is None


@pytest.mark.parametrize("make", [make_winter_day_input, make_summer_day_input])
@pytest.mark.parametrize("interval", [15, 60])
@pytest.mark.parametrize("hour", [9, 10, 12, 13, 14, 20])
class TestRunPlannerOnBoundary:
    """A full plan with ``now`` on a boundary is self-consistent."""

    def test_no_self_consistency_warning(
        self, make: Callable[[], PlannerInput], interval: int, hour: int
    ) -> None:
        now = datetime(2026, 6, 1, hour, 0, tzinfo=_TZ)

        _, _, warnings = _run(make, now, interval)

        assert [w for w in warnings if "self-consistency" in w] == []

    def test_boundary_slot_passed_and_next_slot_live(
        self, make: Callable[[], PlannerInput], interval: int, hour: int
    ) -> None:
        now = datetime(2026, 6, 1, hour, 0, tzinfo=_TZ)
        now_utc = now.astimezone(UTC)

        slots, current, _ = _run(make, now, interval)

        (ended,) = [s for s in slots if s.end.astimezone(UTC) == now_utc]
        (live,) = [s for s in slots if s.start.astimezone(UTC) == now_utc]
        assert ended.recommendation == _TIME_PASSED
        assert live.recommendation != _TIME_PASSED
        assert current == live.recommendation


@pytest.mark.parametrize("offset", [timedelta(seconds=-1), timedelta(seconds=1)])
def test_one_second_off_boundary_is_unchanged(offset: timedelta) -> None:
    """Either side of a boundary the live slot is the one containing now."""
    now = datetime(2026, 6, 1, 12, 0, tzinfo=_TZ) + offset
    now_utc = now.astimezone(UTC)

    slots, current, warnings = _run(make_winter_day_input, now, 15)

    (live,) = [
        s for s in slots if s.start.astimezone(UTC) <= now_utc < s.end.astimezone(UTC)
    ]
    assert current == live.recommendation
    assert live.recommendation != _TIME_PASSED
    assert [w for w in warnings if "self-consistency" in w] == []
    assert all(
        s.recommendation == _TIME_PASSED
        for s in slots
        if s.end.astimezone(UTC) <= now_utc
    )

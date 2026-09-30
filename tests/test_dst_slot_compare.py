"""Regression tests for issue #1167: past/live/future checks in the DST fall-back hour.

Since #1160 the slot grid holds both occurrences of the repeated hour, with
fixed-offset boundaries (``02:00+02:00`` … and again ``02:00+01:00`` …).
Call sites used to decide past/live/future with
``as_tz(slot.x, now.tzinfo) <op> now``.  When ``now`` carries the HA
``ZoneInfo`` (the coordinator's ``now``), both operands then share one
``ZoneInfo`` and Python compares their wall-clock fields, ignoring
``fold``, so during the second occurrence a slot from the first one looked
live or future.

Every test below runs with ``now`` at 02:30 local on 2026-10-25 in
``Europe/Copenhagen`` on both folds, and states the expectation in UTC:

  - fold=0 → 00:30Z (first occurrence, UTC+2)
  - fold=1 → 01:30Z (second occurrence, UTC+1)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.coordinator_tracking import (
    _accumulate_plan_for_slots,
    _last_completed_slot_end,
)
from custom_components.hsem.models.daily_plan_vs_actual_tracker import (
    DailyPlanVsActualTracker,
)
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.engine_core import _build_ev_configs_for_milp
from custom_components.hsem.planner.slot_population import mark_time_passed
from custom_components.hsem.planner.window_hysteresis import apply_window_hysteresis
from custom_components.hsem.utils.datetime_utils import (
    future_slot_indices,
    physical_slot_grid,
    slot_is_future,
)
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations
from tests.planner.fixtures import make_flat_price_input

_TZ = ZoneInfo("Europe/Copenhagen")
_DAY = datetime(2026, 10, 25, 12, 0, tzinfo=_TZ)
_FOLDS = [
    pytest.param(0, datetime(2026, 10, 25, 0, 30, tzinfo=UTC), id="fold0"),
    pytest.param(1, datetime(2026, 10, 25, 1, 30, tzinfo=UTC), id="fold1"),
]


def _now(fold: int) -> datetime:
    """Return 02:30 local on the fall-back day, in the HA ``ZoneInfo``."""
    return datetime(2026, 10, 25, 2, 30, tzinfo=_TZ, fold=fold)


def _grid_slots() -> list[PlannedSlot]:
    """Return the 100 fixed-offset 15-min slots of 2026-10-25."""
    return [
        PlannedSlot(
            start=start,
            end=end,
            price=SlotPrice(import_price=1.0 + i / 1000.0, export_price=0.1),
        )
        for i, (start, end) in enumerate(physical_slot_grid(_DAY, 15, 24))
    ]


def _live_index(slots: list[PlannedSlot], now_utc: datetime) -> int:
    """Return the index of the slot physically containing *now_utc*."""
    (idx,) = [
        i
        for i, s in enumerate(slots)
        if s.start.astimezone(UTC) <= now_utc < s.end.astimezone(UTC)
    ]
    return idx


def test_fold_fixture_is_the_repeated_hour() -> None:
    """Both folds read 02:30 locally but are one real hour apart."""
    assert _now(0).astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert _now(1).astimezone(UTC) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


class TestSlotIsFuture:
    """``slot_is_future`` compares by UTC instant."""

    @pytest.mark.parametrize(("fold", "now_utc"), _FOLDS)
    def test_matches_utc_comparison_on_every_slot(
        self, fold: int, now_utc: datetime
    ) -> None:
        """Each grid slot is future exactly when its UTC end is after now."""
        now = _now(fold)
        for start, end in physical_slot_grid(_DAY, 15, 24):
            assert slot_is_future(end, now) is (end.astimezone(UTC) > now_utc), start

    def test_first_occurrence_slot_is_past_during_second(self) -> None:
        """The issue's example: 02:30-02:45+02:00 has ended at 02:30 fold=1."""
        end = datetime(2026, 10, 25, 2, 45, tzinfo=_TZ).astimezone(UTC)
        assert slot_is_future(end, _now(0)) is True
        assert slot_is_future(end, _now(1)) is False

    def test_future_slot_indices_uses_the_same_rule(self) -> None:
        """``future_slot_indices`` agrees with ``slot_is_future``."""
        ends = [end for _, end in physical_slot_grid(_DAY, 15, 24)]
        now = _now(1)
        assert future_slot_indices(ends, now) == [
            i for i, end in enumerate(ends) if slot_is_future(end, now)
        ]


class TestEvMilpFilter:
    """The EV MILP's LP index must match ``future_slot_indices`` (#1167)."""

    @pytest.mark.parametrize(("fold", "now_utc"), _FOLDS)
    def test_deadline_slot_counts_physical_future_slots(
        self, fold: int, now_utc: datetime
    ) -> None:
        """The deadline maps to the LP row of the last slot ending by 03:00Z."""
        slots = _grid_slots()
        deadline = datetime(2026, 10, 25, 4, 0, tzinfo=_TZ)  # 03:00Z
        inp = PlannerInput(
            now_iso=_now(fold).isoformat(),
            interval_minutes=15,
            interval_length_hours=24,
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=20.0,
            ev_planned_load_target_soc_pct=80.0,
            ev_planned_load_battery_capacity_kwh=50.0,
            ev_planned_load_charger_power_kw=7.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=deadline,
        )

        configs = _build_ev_configs_for_milp(inp, slots, _now(fold))

        assert configs is not None
        lp_slots = [s for s in slots if s.end.astimezone(UTC) > now_utc]
        expected = (
            sum(
                1 for s in lp_slots if s.end.astimezone(UTC) <= deadline.astimezone(UTC)
            )
            - 1
        )
        # 00:30Z → ten slots end by 03:00Z; 01:30Z → six.
        assert expected == (9 if fold == 0 else 5)
        assert configs[0].deadline_slot == expected


class TestLiveSlotDetection:
    """Live-slot checks pick the physically current slot on both folds."""

    @pytest.mark.parametrize(("fold", "now_utc"), _FOLDS)
    def test_window_hysteresis_current_slot(self, fold: int, now_utc: datetime) -> None:
        """``apply_window_hysteresis`` runs on the coordinator's ZoneInfo now."""
        slots = _grid_slots()
        for i, s in enumerate(slots):
            s.recommendation = f"rec-{i}"
        live = _live_index(slots, now_utc)

        rec, start = apply_window_hysteresis(
            slots,
            _now(fold),
            window_hysteresis_minutes=0,
            previous_current_recommendation=None,
            previous_current_slot_start=None,
        )

        assert rec == f"rec-{live}"
        assert start is not None
        assert start.astimezone(UTC) == now_utc

    @pytest.mark.parametrize(("fold", "now_utc"), _FOLDS)
    def test_engine_core_current_recommendation(
        self, fold: int, now_utc: datetime
    ) -> None:
        """``run_planner``'s ``current_recommendation`` is the live slot's."""
        inp = make_flat_price_input(now_iso=_now(fold).isoformat(), interval_minutes=15)

        out = run_planner(inp)

        live = [
            s
            for s in out.slots
            if s.start.astimezone(UTC) <= now_utc < s.end.astimezone(UTC)
        ]
        assert len(live) == 1
        assert out.current_recommendation == live[0].recommendation
        assert out.current_recommendation != Recommendations.TimePassed.value

    @pytest.mark.parametrize(("fold", "now_utc"), _FOLDS)
    def test_plan_accumulation_marks_the_live_slot(
        self, fold: int, now_utc: datetime
    ) -> None:
        """Daily plan accumulation uses the physically live slot as its marker."""
        slots = _grid_slots()
        tracker = DailyPlanVsActualTracker()

        marker = _accumulate_plan_for_slots(tracker, slots, _now(fold), None)

        assert marker is not None
        assert marker.astimezone(UTC) == now_utc

    @pytest.mark.parametrize(("fold", "now_utc"), _FOLDS)
    def test_last_completed_slot_end(self, fold: int, now_utc: datetime) -> None:
        """The latest completed slot ends at *now* (now is on a boundary)."""
        end = _last_completed_slot_end(_grid_slots(), _now(fold))

        assert end is not None
        assert end.astimezone(UTC) == now_utc


class TestPastSlots:
    """Past-slot marking follows physical time."""

    @pytest.mark.parametrize(("fold", "now_utc"), _FOLDS)
    def test_mark_time_passed(self, fold: int, now_utc: datetime) -> None:
        """Exactly the slots ending by *now* in UTC are ``TimePassed``."""
        slots = _grid_slots()

        mark_time_passed(slots, _now(fold))

        passed = [s.recommendation == Recommendations.TimePassed.value for s in slots]
        assert passed == [s.end.astimezone(UTC) <= now_utc for s in slots]
        # The grid starts at 22:00Z: 10 slots end by 00:30Z, 14 by 01:30Z
        # (a slot ending exactly at now has passed, issue #1174), so fold=1
        # also marks the first occurrence of 02:00-03:00.
        assert sum(passed) == (10 if fold == 0 else 14)


_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "custom_components"
_WALL_CLOCK_COMPARE = re.compile(r"as_tz\([^()]*\)\s*(<=|>=|<|>)|(<=|>=|<|>)\s*as_tz\(")


def test_no_as_tz_ordering_comparisons_remain() -> None:
    """Guard: slot ordering must not go through ``as_tz`` (issue #1167).

    ``as_tz`` is for reading wall-clock fields (``.date()``, ``.hour``).
    Ordering its result against ``now`` compares by wall clock.
    """
    offenders = [
        f"{path.relative_to(_SOURCE_ROOT)}:{lineno}: {line.strip()}"
        for path in sorted(_SOURCE_ROOT.rglob("*.py"))
        for lineno, line in enumerate(path.read_text().splitlines(), start=1)
        if _WALL_CLOCK_COMPARE.search(line)
    ]
    assert offenders == []


def test_guard_pattern_catches_the_old_forms() -> None:
    """The guard regex matches the patterns #1167 removed."""
    for line in (
        "if as_tz(s.start, now.tzinfo) <= now < as_tz(s.end, now.tzinfo):",
        "fut = [s for s in slots if as_tz(s.end, now.tzinfo) > now]",
        "if now < as_tz(s.start, tz) <= cutoff",
    ):
        assert _WALL_CLOCK_COMPARE.search(line), line
    assert not _WALL_CLOCK_COMPARE.search("day = as_tz(s.start, tz).date()")

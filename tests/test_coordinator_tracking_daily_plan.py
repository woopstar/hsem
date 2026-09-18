"""Tests for daily plan-vs-actual accumulation in ``coordinator_tracking``.

Covers the lazy tracker initialisation (history path, history load, midnight
timer), plan accumulation for the current and missed slots, actual
accumulation from cumulative meters and SoC deltas, and the midnight
persist-and-reset handler (issue #540).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.hsem.coordinator_tracking import (
    _accumulate_plan_for_slots,
    _async_handle_midnight,
    _init_daily_tracker,
    accumulate_daily_plan_actuals,
)
from custom_components.hsem.models.daily_metrics import DailyMetrics
from custom_components.hsem.models.daily_plan_vs_actual_tracker import (
    DailyPlanVsActualTracker,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.utils.prices import SlotPrice

_MODULE = "custom_components.hsem.coordinator_tracking"
_SLOT = timedelta(minutes=15)
_T0 = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)


def _make_hass(config_dir: Path) -> HomeAssistant:
    """Build a minimal fake HomeAssistant exposing only ``config.config_dir``."""
    return cast(
        HomeAssistant,
        SimpleNamespace(config=SimpleNamespace(config_dir=str(config_dir))),
    )


def _slot(start: datetime, **kwargs: Any) -> PlannedSlot:
    """Build a 15-minute planned slot starting at *start*."""
    return PlannedSlot(start=start, end=start + _SLOT, **kwargs)


def _tracker(today: datetime = _T0) -> DailyPlanVsActualTracker:
    """Return a tracker already dated to *today* and marked initialised."""
    tracker = DailyPlanVsActualTracker(today=today.date().isoformat())
    tracker._initialized = True  # type: ignore[attr-defined]  # skip lazy init
    return tracker


# ---------------------------------------------------------------------------
# Lazy initialisation
# ---------------------------------------------------------------------------


class TestInitDailyTracker:
    """The tracker is wired to storage and a midnight timer exactly once."""

    @pytest.mark.asyncio
    async def test_first_access_loads_history_and_registers_midnight_timer(
        self, tmp_path: Path
    ) -> None:
        """History is read from ``.storage`` and a 00:00:00 timer is set up."""
        history_path = tmp_path / ".storage" / "hsem_daily_history.json"
        history_path.parent.mkdir(parents=True)
        history_path.write_text(
            json.dumps(
                {
                    "days": [
                        {
                            "date": _T0.date().isoformat(),
                            "actual": {"grid_import_kwh": 3.0},
                            "plan": {"grid_import_kwh": 2.0},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        tracker = DailyPlanVsActualTracker(today=_T0.date().isoformat())
        hass = _make_hass(tmp_path)
        unsub = MagicMock()
        track_time_change = MagicMock(return_value=unsub)

        with patch(f"{_MODULE}.async_track_time_change", track_time_change):
            await _init_daily_tracker(tracker, hass)
            await _init_daily_tracker(tracker, hass)

        assert tracker.history_file == str(history_path)
        assert [record.date for record in tracker.history] == [_T0.date().isoformat()]
        track_time_change.assert_called_once()
        args, kwargs = track_time_change.call_args
        assert args[0] is hass
        assert kwargs == {"hour": 0, "minute": 0, "second": 0}
        assert tracker._midnight_unsub is unsub  # type: ignore[attr-defined]
        assert tracker._initialized is True  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_registered_midnight_action_saves_and_resets(
        self, tmp_path: Path
    ) -> None:
        """The registered action schedules the async midnight handler."""
        tracker = _dirty_tracker("")
        create_task = MagicMock()
        hass = cast(
            HomeAssistant,
            SimpleNamespace(
                async_create_task=create_task,
                config=SimpleNamespace(config_dir=str(tmp_path)),
            ),
        )
        track_time_change = MagicMock(return_value=MagicMock())

        with patch(f"{_MODULE}.async_track_time_change", track_time_change):
            await _init_daily_tracker(tracker, hass)

        midnight_action = track_time_change.call_args.args[1]
        midnight_action(_T0)
        create_task.assert_called_once()
        await create_task.call_args.args[0]

        saved = json.loads(Path(tracker.history_file).read_text(encoding="utf-8"))
        assert saved["days"][0]["date"] == "2026-05-31"
        assert saved["days"][0]["actual"]["grid_import_kwh"] == pytest.approx(5.0)
        _assert_reset_for_today(tracker)

    @pytest.mark.asyncio
    async def test_failure_is_logged_and_does_not_retry(self) -> None:
        """A broken ``hass`` logs an error but never crashes the coordinator."""
        tracker = DailyPlanVsActualTracker()
        broken_hass = cast(HomeAssistant, SimpleNamespace())
        log = MagicMock()

        with patch(f"{_MODULE}.async_log", log):
            await _init_daily_tracker(tracker, broken_hass)
            await _init_daily_tracker(tracker, broken_hass)

        log.assert_called_once()
        assert log.call_args.args[0] == "error"
        assert tracker._initialized is True  # type: ignore[attr-defined]
        assert tracker.history_file == ""


# ---------------------------------------------------------------------------
# accumulate_daily_plan_actuals
# ---------------------------------------------------------------------------


class TestAccumulateDailyPlanActuals:
    """One coordinator cycle accumulates plan and actual values."""

    @pytest.mark.asyncio
    async def test_current_slot_plan_is_accumulated_once(self, tmp_path: Path) -> None:
        """The in-progress slot's full plan is counted on first sight only."""
        tracker = _tracker()
        output = PlannerOutput(
            slots=[
                _slot(
                    _T0,
                    price=SlotPrice(2.0, 0.5),
                    grid_import_kwh=1.0,
                    grid_export_kwh=0.4,
                    batteries_charged_kwh=0.6,
                    batteries_discharged_kwh=0.2,
                    solcast_pv_estimate_kwh=1.5,
                )
            ]
        )
        hass = _make_hass(tmp_path)
        now = _T0 + timedelta(minutes=5)

        marker = await accumulate_daily_plan_actuals(
            now=now,
            live=LiveState(),
            output=output,
            daily_tracker=tracker,
            daily_plan_last_accumulated=None,
            hass=hass,
        )
        marker_again = await accumulate_daily_plan_actuals(
            now=now + timedelta(minutes=5),
            live=LiveState(),
            output=output,
            daily_tracker=tracker,
            daily_plan_last_accumulated=marker,
            hass=hass,
        )

        assert marker == _T0
        assert marker_again == _T0
        assert tracker.plan.grid_import_kwh == pytest.approx(1.0)
        assert tracker.plan.grid_import_cost == pytest.approx(2.0)
        assert tracker.plan.grid_export_kwh == pytest.approx(0.4)
        assert tracker.plan.grid_export_rev == pytest.approx(0.2)
        assert tracker.plan.battery_cycled_kwh == pytest.approx(0.8)
        assert tracker.plan.pv_produced_kwh == pytest.approx(1.5)

    @pytest.mark.asyncio
    async def test_actuals_come_from_meter_and_soc_deltas(self, tmp_path: Path) -> None:
        """Meter deltas are priced and SoC swings count as cycled energy."""
        tracker = _tracker()
        hass = _make_hass(tmp_path)
        output = PlannerOutput()

        def live(import_kwh: float, export_kwh: float, soc: float) -> LiveState:
            state = LiveState()
            state.grid_import_energy_kwh = import_kwh
            state.grid_export_energy_kwh = export_kwh
            state.pv_energy_kwh = import_kwh * 2
            state.huawei_batteries_soc_pct = soc
            state.huawei_batteries_rated_capacity_wh = 10_000.0
            state.import_electricity_price = 2.0
            state.export_electricity_price = 0.5
            return state

        for minutes, reading in (
            (0, live(10.0, 4.0, 50.0)),
            (5, live(11.0, 5.0, 60.0)),
        ):
            await accumulate_daily_plan_actuals(
                now=_T0 + timedelta(minutes=minutes),
                live=reading,
                output=output,
                daily_tracker=tracker,
                daily_plan_last_accumulated=None,
                hass=hass,
            )

        assert tracker.actual.grid_import_kwh == pytest.approx(1.0)
        assert tracker.actual.grid_import_cost == pytest.approx(2.0)
        assert tracker.actual.grid_export_kwh == pytest.approx(1.0)
        assert tracker.actual.grid_export_rev == pytest.approx(0.5)
        assert tracker.actual.pv_produced_kwh == pytest.approx(2.0)
        # 10 percentage points of a 10 kWh battery.
        assert tracker.actual.battery_cycled_kwh == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_day_rollover_persists_yesterday_before_accumulating(
        self, tmp_path: Path
    ) -> None:
        """A new calendar day saves yesterday's record and restarts counters."""
        tracker = _tracker(today=_T0 - timedelta(days=1))
        tracker.history_file = str(tmp_path / "daily.json")
        tracker.plan = DailyMetrics(grid_import_kwh=7.0)

        await accumulate_daily_plan_actuals(
            now=_T0,
            live=LiveState(),
            output=PlannerOutput(),
            daily_tracker=tracker,
            daily_plan_last_accumulated=None,
            hass=_make_hass(tmp_path),
        )

        assert tracker.today == _T0.date().isoformat()
        assert tracker.plan.grid_import_kwh == pytest.approx(0.0)
        saved = json.loads(Path(tracker.history_file).read_text(encoding="utf-8"))
        assert saved["days"][0]["date"] == (_T0 - timedelta(days=1)).date().isoformat()
        assert saved["days"][0]["plan"]["grid_import_kwh"] == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# _accumulate_plan_for_slots
# ---------------------------------------------------------------------------


class TestAccumulatePlanForSlots:
    """Plan accumulation for the current slot and the missed-slot safety net."""

    def test_first_cycle_skips_past_slots_and_marks_last_completed_end(self) -> None:
        """On startup, zeroed past slots are not counted as plan."""
        tracker = _tracker()
        slots = [
            _slot(_T0, grid_import_kwh=1.0),
            _slot(_T0 + _SLOT, grid_import_kwh=2.0),
        ]
        now = _T0 + 2 * _SLOT + timedelta(minutes=1)

        marker = _accumulate_plan_for_slots(tracker, slots, now, None)

        assert marker == _T0 + 2 * _SLOT
        assert tracker.plan.grid_import_kwh == pytest.approx(0.0)

    def test_missed_slots_after_the_marker_are_counted_once(self) -> None:
        """Slots completed since the last marker are added; older ones are not."""
        tracker = _tracker()
        slots = [
            _slot(_T0, grid_import_kwh=1.0),
            _slot(_T0 + _SLOT, grid_import_kwh=2.0),
            _slot(_T0 + 2 * _SLOT, grid_import_kwh=4.0),
        ]
        now = _T0 + 3 * _SLOT + timedelta(minutes=1)

        marker = _accumulate_plan_for_slots(tracker, slots, now, _T0)

        assert marker == _T0 + 3 * _SLOT
        assert tracker.plan.grid_import_kwh == pytest.approx(6.0)

    def test_marker_is_kept_when_no_slot_has_completed(self) -> None:
        """Only future slots → nothing added and the marker is unchanged."""
        tracker = _tracker()
        slots = [_slot(_T0 + _SLOT, grid_import_kwh=1.0)]

        marker = _accumulate_plan_for_slots(tracker, slots, _T0, _T0 - _SLOT)

        assert marker == _T0 - _SLOT
        assert tracker.plan.grid_import_kwh == pytest.approx(0.0)

    def test_slots_without_bounds_or_price_are_tolerated(self) -> None:
        """Objects lacking start/end are skipped; missing prices cost nothing."""
        tracker = _tracker()
        unbounded = SimpleNamespace(grid_import_kwh=9.0)
        unpriced = SimpleNamespace(
            start=_T0,
            end=_T0 + _SLOT,
            grid_import_kwh=1.0,
            grid_export_kwh=None,
            batteries_charged_kwh=None,
            batteries_discharged_kwh=None,
            solcast_pv_estimate_kwh=None,
        )

        marker = _accumulate_plan_for_slots(
            tracker, [unbounded, unpriced], _T0 + timedelta(minutes=1), None
        )

        assert marker == _T0
        assert tracker.plan.grid_import_kwh == pytest.approx(1.0)
        assert tracker.plan.grid_import_cost == pytest.approx(0.0)
        assert tracker.plan.battery_cycled_kwh == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _async_handle_midnight
# ---------------------------------------------------------------------------


def _dirty_tracker(history_file: str) -> DailyPlanVsActualTracker:
    """Return a tracker with non-zero accumulators and meter baselines."""
    tracker = DailyPlanVsActualTracker(today="2026-05-31", history_file=history_file)
    tracker.actual = DailyMetrics(grid_import_kwh=5.0)
    tracker.plan = DailyMetrics(grid_import_kwh=4.0)
    tracker.last_soc_pct = 55.0
    tracker._last_import_energy_kwh = 12.0
    tracker._last_export_energy_kwh = 3.0
    tracker._last_pv_energy_kwh = 8.0
    return tracker


def _assert_reset_for_today(tracker: DailyPlanVsActualTracker) -> None:
    """Assert every accumulator and baseline was reset for the new day."""
    assert tracker.today == date.today().isoformat()
    assert tracker.actual == DailyMetrics()
    assert tracker.plan == DailyMetrics()
    assert tracker.last_soc_pct is None
    assert tracker._last_import_energy_kwh is None
    assert tracker._last_export_energy_kwh is None
    assert tracker._last_pv_energy_kwh is None


class TestMidnightHandler:
    """At midnight the day's record is persisted and accumulators reset."""

    @pytest.mark.asyncio
    async def test_saves_record_and_resets(self, tmp_path: Path) -> None:
        """A successful save is logged at info level."""
        history_path = tmp_path / "daily.json"
        tracker = _dirty_tracker(str(history_path))
        log = MagicMock()

        with patch(f"{_MODULE}.async_log", log):
            await _async_handle_midnight(tracker, _make_hass(tmp_path))

        saved = json.loads(history_path.read_text(encoding="utf-8"))
        assert saved["days"][0]["date"] == "2026-05-31"
        assert saved["days"][0]["actual"]["grid_import_kwh"] == pytest.approx(5.0)
        assert log.call_args.args[0] == "info"
        _assert_reset_for_today(tracker)

    @pytest.mark.asyncio
    async def test_failed_save_warns_but_still_resets(self, tmp_path: Path) -> None:
        """A failed save is logged as a warning; the new day still starts."""
        tracker = _dirty_tracker(str(tmp_path / "daily.json"))
        tracker._save_record_to_history = AsyncMock(return_value=False)  # type: ignore[method-assign]  # force failure
        log = MagicMock()

        with patch(f"{_MODULE}.async_log", log):
            await _async_handle_midnight(tracker, _make_hass(tmp_path))

        assert log.call_args.args[0] == "warning"
        _assert_reset_for_today(tracker)

    @pytest.mark.asyncio
    async def test_without_history_file_nothing_happens(self, tmp_path: Path) -> None:
        """An uninitialised tracker is left untouched."""
        tracker = _dirty_tracker("")

        await _async_handle_midnight(tracker, _make_hass(tmp_path))

        assert tracker.today == "2026-05-31"
        assert tracker.actual.grid_import_kwh == pytest.approx(5.0)

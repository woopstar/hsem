"""Tests for EV SoC economics (issue #903).

All tests are pure-Python and carry no Home Assistant dependencies beyond
the ``STATE_UNAVAILABLE`` string constant already used throughout the
planner layer.

Test classes
------------
TestNextTimeOfDay              — next_time_of_day() helper
TestComputeEvSocEconomicsGuards — guard-clause short-circuits
TestComputeEvSocEconomics       — end-to-end cost/feasibility computation
TestDeadlineColumns             — configured deadlines and target deadline (#1344)
TestDeadlineHelpers             — effective_target_deadline() / deadline_columns()
TestChargingCost                — baseline solve and EV charging cost (#1348)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from homeassistant.const import STATE_UNAVAILABLE

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner.ev_soc_economics import (
    EVSoCEconomicsResult,
    compute_ev_soc_economics,
    deadline_columns,
    effective_target_deadline,
    next_time_of_day,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_planner_input(
    now_iso: str = "2024-06-15T14:00:00+00:00",
    *,
    ev_enabled: bool = True,
    ev_connected: bool = True,
    smart_charging: bool = True,
    current_soc: float = 50.0,
    capacity_kwh: float = 77.0,
    charger_kw: float = 11.0,
    efficiency: float = 100.0,
    second_ev_enabled: bool = False,
    second_ev_connected: bool = True,
    second_smart_charging: bool = True,
    second_current_soc: float = 50.0,
    second_capacity_kwh: float = 60.0,
    second_charger_kw: float = 7.4,
    second_efficiency: float = 100.0,
) -> PlannerInput:
    """Build a minimal, fully-populated PlannerInput with EV settings."""
    prices = [
        PricePoint(hour=h, import_price=0.10, export_price=0.05) for h in range(24)
    ]
    for h in range(6):
        prices[h] = PricePoint(hour=h, import_price=0.05, export_price=0.02)
    for h in range(16, 21):
        prices[h] = PricePoint(hour=h, import_price=0.30, export_price=0.15)

    pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
    for h in range(10, 16):
        pv[h] = SolcastSlot(hour=h, pv_estimate=3.5)

    averages = [
        HourlyConsumptionAverage(
            hour=h, avg_1d=1.0, avg_3d=1.0, avg_7d=1.0, avg_14d=1.0
        )
        for h in range(24)
    ]

    return PlannerInput(
        now_iso=now_iso,
        interval_minutes=60,
        interval_length_hours=24,
        battery_soc_pct=50.0,
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=90.0,
        battery_max_charge_power_w=5000.0,
        battery_max_discharge_power_w=5000.0,
        battery_charge_efficiency_pct=95.0,
        battery_discharge_efficiency_pct=95.0,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=averages,
        price_points=prices,
        solcast_slots=pv,
        ev_planned_load_enabled=ev_enabled,
        ev_planned_load_connected=ev_connected,
        ev_planned_load_smart_charging_enabled=smart_charging,
        ev_planned_load_current_soc_pct=current_soc,
        ev_planned_load_target_soc_pct=80.0,
        ev_planned_load_battery_capacity_kwh=capacity_kwh,
        ev_planned_load_charger_power_kw=charger_kw,
        ev_planned_load_charger_efficiency_pct=efficiency,
        ev_second_planned_load_enabled=second_ev_enabled,
        ev_second_planned_load_connected=second_ev_connected,
        ev_second_planned_load_smart_charging_enabled=second_smart_charging,
        ev_second_planned_load_current_soc_pct=second_current_soc,
        ev_second_planned_load_target_soc_pct=80.0,
        ev_second_planned_load_battery_capacity_kwh=second_capacity_kwh,
        ev_second_planned_load_charger_power_kw=second_charger_kw,
        ev_second_planned_load_charger_efficiency_pct=second_efficiency,
    )


# ---------------------------------------------------------------------------
# TestNextTimeOfDay
# ---------------------------------------------------------------------------


class TestNextTimeOfDay:
    """Unit tests for next_time_of_day()."""

    def test_time_later_today(self):
        """When the target time is still ahead today, return today's date."""
        now = datetime(2024, 6, 15, 6, 0, tzinfo=UTC)
        result = next_time_of_day(now, 8)
        assert result == datetime(2024, 6, 15, 8, 0, tzinfo=UTC)

    def test_time_already_passed_today(self):
        """When the target time has passed today, return tomorrow's date."""
        now = datetime(2024, 6, 15, 9, 0, tzinfo=UTC)
        result = next_time_of_day(now, 8)
        assert result == datetime(2024, 6, 16, 8, 0, tzinfo=UTC)

    def test_time_exactly_now(self):
        """When now lands exactly on the target time, it is not 'already passed'."""
        now = datetime(2024, 6, 15, 8, 0, tzinfo=UTC)
        result = next_time_of_day(now, 8)
        assert result == now

    def test_with_minute(self):
        """Minute argument is honoured."""
        now = datetime(2024, 6, 15, 6, 0, tzinfo=UTC)
        result = next_time_of_day(now, 17, minute=30)
        assert result == datetime(2024, 6, 15, 17, 30, tzinfo=UTC)

    def test_preserves_timezone(self):
        """Returned datetime carries the same timezone as now."""
        from datetime import timezone

        tz = timezone(timedelta(hours=2))
        now = datetime(2024, 6, 15, 6, 0, tzinfo=tz)
        result = next_time_of_day(now, 8)
        assert result.tzinfo == tz


# ---------------------------------------------------------------------------
# TestComputeEvSocEconomicsGuards
# ---------------------------------------------------------------------------


class TestComputeEvSocEconomicsGuards:
    """Guard-clause short-circuits never call run_planner()."""

    def _now(self) -> datetime:
        return datetime(2024, 6, 15, 14, 0, tzinfo=UTC)

    def test_not_enabled(self):
        """Disabled EV integration short-circuits to smart_charging_disabled."""
        base_input = _make_planner_input(ev_enabled=False)
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner"
        ) as mock_run:
            result = compute_ev_soc_economics(
                base_input,
                is_second=False,
                current_soc_pct=50.0,
                capacity_kwh=77.0,
                max_charge_kw=11.0,
                now=self._now(),
            )
        assert result.state == "smart_charging_disabled"
        assert result.points == []
        mock_run.assert_not_called()

    def test_not_connected(self):
        """Disconnected EV short-circuits to not_connected."""
        base_input = _make_planner_input(ev_connected=False)
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner"
        ) as mock_run:
            result = compute_ev_soc_economics(
                base_input,
                is_second=False,
                current_soc_pct=50.0,
                capacity_kwh=77.0,
                max_charge_kw=11.0,
                now=self._now(),
            )
        assert result.state == "not_connected"
        assert result.points == []
        mock_run.assert_not_called()

    def test_smart_charging_disabled(self):
        """Smart charging toggle off short-circuits to smart_charging_disabled."""
        base_input = _make_planner_input(smart_charging=False)
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner"
        ) as mock_run:
            result = compute_ev_soc_economics(
                base_input,
                is_second=False,
                current_soc_pct=50.0,
                capacity_kwh=77.0,
                max_charge_kw=11.0,
                now=self._now(),
            )
        assert result.state == "smart_charging_disabled"
        assert result.points == []
        mock_run.assert_not_called()

    def test_zero_capacity(self):
        """Zero battery capacity short-circuits to STATE_UNAVAILABLE."""
        base_input = _make_planner_input()
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner"
        ) as mock_run:
            result = compute_ev_soc_economics(
                base_input,
                is_second=False,
                current_soc_pct=50.0,
                capacity_kwh=0.0,
                max_charge_kw=11.0,
                now=self._now(),
            )
        assert result.state == STATE_UNAVAILABLE
        assert result.points == []
        mock_run.assert_not_called()

    def test_zero_charger_power(self):
        """Zero charger power short-circuits to STATE_UNAVAILABLE."""
        base_input = _make_planner_input()
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner"
        ) as mock_run:
            result = compute_ev_soc_economics(
                base_input,
                is_second=False,
                current_soc_pct=50.0,
                capacity_kwh=77.0,
                max_charge_kw=0.0,
                now=self._now(),
            )
        assert result.state == STATE_UNAVAILABLE
        assert result.points == []
        mock_run.assert_not_called()

    def test_unavailable_soc_short_circuits(self):
        """SoC None (unavailable sensor) short-circuits — issue #988.

        The economics table is a what-if diagnostic; computing it from a
        fabricated 0 % would show made-up prices for an empty battery.
        """
        base_input = _make_planner_input()
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner"
        ) as mock_run:
            result = compute_ev_soc_economics(
                base_input,
                is_second=False,
                current_soc_pct=None,
                capacity_kwh=77.0,
                max_charge_kw=11.0,
                now=self._now(),
            )
        assert result.state == STATE_UNAVAILABLE
        assert result.points == []
        assert result.current_soc_pct is None
        assert result.as_attributes()["current_soc_pct"] is None
        mock_run.assert_not_called()

    def test_second_ev_guards_read_second_fields(self):
        """is_second=True reads ev_second_* guard fields, not primary."""
        base_input = _make_planner_input(
            ev_enabled=True,  # primary enabled...
            second_ev_enabled=False,  # ...but second disabled.
        )
        result = compute_ev_soc_economics(
            base_input,
            is_second=True,
            current_soc_pct=50.0,
            capacity_kwh=60.0,
            max_charge_kw=7.4,
            now=self._now(),
        )
        assert result.state == "smart_charging_disabled"


# ---------------------------------------------------------------------------
# TestComputeEvSocEconomics
# ---------------------------------------------------------------------------


class TestComputeEvSocEconomics:
    """End-to-end cost/feasibility computation."""

    def test_ready_state_and_full_point_grid(self):
        """Default targets × 2 deadlines produce 10 points, state ready."""
        base_input = _make_planner_input(current_soc=50.0)
        result = compute_ev_soc_economics(
            base_input,
            is_second=False,
            current_soc_pct=50.0,
            capacity_kwh=77.0,
            max_charge_kw=11.0,
            now=datetime(2024, 6, 15, 14, 0, tzinfo=UTC),
        )
        assert result.state == "ready"
        assert len(result.points) == 10
        labels = {p.deadline_label for p in result.points}
        assert labels == {"08:00", "17:00"}
        targets_per_label = {
            label: sorted(
                p.target_soc_pct for p in result.points if p.deadline_label == label
            )
            for label in labels
        }
        for targets in targets_per_label.values():
            assert targets == [50.0, 60.0, 70.0, 80.0, 100.0]

    def test_targets_below_current_soc_are_removed(self):
        """Targets below the car's actual SoC are absent from the result."""
        base_input = _make_planner_input(current_soc=83.0)
        call_count = 0
        from custom_components.hsem.planner import ev_soc_economics as mod

        real_run_planner = mod.run_planner

        def _counting_run_planner(inp):
            nonlocal call_count
            call_count += 1
            return real_run_planner(inp)

        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner",
            side_effect=_counting_run_planner,
        ):
            result = compute_ev_soc_economics(
                base_input,
                is_second=False,
                current_soc_pct=83.0,
                capacity_kwh=77.0,
                max_charge_kw=11.0,
                now=datetime(2024, 6, 15, 14, 0, tzinfo=UTC),
                soc_targets=(50.0, 60.0, 70.0, 80.0, 100.0),
            )

        # One baseline solve (#1348) plus one per retained row.
        assert call_count == 3
        assert [point.target_soc_pct for point in result.points] == [100.0, 100.0]

    def test_target_equal_to_current_soc_remains_without_own_solve(self):
        """A target equal to actual SoC is the zero-cost baseline row.

        The only solve is the baseline (#1348); the row reuses it.
        """
        base_input = _make_planner_input(current_soc=80.0)
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner"
        ) as mock_run:
            mock_run.return_value.plan_cost.total_cost = 12.5
            result = compute_ev_soc_economics(
                base_input,
                is_second=False,
                current_soc_pct=80.0,
                capacity_kwh=77.0,
                max_charge_kw=11.0,
                now=datetime(2024, 6, 15, 14, 0, tzinfo=UTC),
                soc_targets=(70.0, 80.0),
            )

        assert len(result.points) == 2
        assert all(
            point.target_soc_pct == pytest.approx(80.0) for point in result.points
        )
        assert all(point.total_cost == pytest.approx(0.0) for point in result.points)
        assert all(
            point.plan_total_cost == pytest.approx(12.5) for point in result.points
        )
        mock_run.assert_called_once()

    def test_monotonic_cost_per_deadline_column(self):
        """Cost is monotonically non-decreasing within a deadline column."""
        base_input = _make_planner_input(current_soc=20.0)
        result = compute_ev_soc_economics(
            base_input,
            is_second=False,
            current_soc_pct=20.0,
            capacity_kwh=77.0,
            max_charge_kw=11.0,
            now=datetime(2024, 6, 15, 6, 0, tzinfo=UTC),
            soc_targets=(40.0, 60.0, 80.0),
        )
        for label in ("08:00", "17:00"):
            column = [p for p in result.points if p.deadline_label == label]
            column.sort(key=lambda p: p.target_soc_pct)
            costs = [p.total_cost for p in column]
            for earlier, later in zip(costs, costs[1:]):
                assert later >= earlier - 1e-9

    def test_feasibility_independent_of_price(self):
        """A target unreachable at rated charger power is marked infeasible."""
        base_input = _make_planner_input(current_soc=10.0)
        # now is 1 minute before the 08:00 deadline, so almost no energy can
        # be delivered before it — regardless of price.
        now = datetime(2024, 6, 15, 7, 59, tzinfo=UTC)
        result = compute_ev_soc_economics(
            base_input,
            is_second=False,
            current_soc_pct=10.0,
            capacity_kwh=77.0,
            max_charge_kw=11.0,
            now=now,
            soc_targets=(60.0,),
        )
        eight_am_point = next(p for p in result.points if p.deadline_label == "08:00")
        assert eight_am_point.feasible is False

        # The 17:00 deadline gives ~9 hours — comfortably feasible for the
        # same target with an 11 kW charger.
        five_pm_point = next(p for p in result.points if p.deadline_label == "17:00")
        assert five_pm_point.feasible is True

    def test_delta_from_previous_and_per_10pct(self):
        """Delta fields are None for the first target, computed afterward."""
        base_input = _make_planner_input(current_soc=20.0)
        result = compute_ev_soc_economics(
            base_input,
            is_second=False,
            current_soc_pct=20.0,
            capacity_kwh=77.0,
            max_charge_kw=11.0,
            now=datetime(2024, 6, 15, 6, 0, tzinfo=UTC),
            soc_targets=(40.0, 60.0),
        )
        column = sorted(
            (p for p in result.points if p.deadline_label == "08:00"),
            key=lambda p: p.target_soc_pct,
        )
        assert column[0].delta_from_previous is None
        assert column[0].delta_per_10pct is None
        delta_from_previous = column[1].delta_from_previous
        assert delta_from_previous is not None
        assert delta_from_previous == pytest.approx(
            column[1].total_cost - column[0].total_cost
        )
        assert column[1].delta_per_10pct == pytest.approx(
            delta_from_previous / (60.0 - 40.0) * 10.0
        )

    def test_second_ev_overrides_second_fields_only(self):
        """is_second=True clones and overrides ev_second_* fields, not primary."""
        base_input = _make_planner_input(
            second_ev_enabled=True,
            second_current_soc=30.0,
            second_capacity_kwh=60.0,
            second_charger_kw=7.4,
        )
        original_primary_target = base_input.ev_planned_load_target_soc_pct
        original_primary_deadline = base_input.ev_planned_load_deadline

        result = compute_ev_soc_economics(
            base_input,
            is_second=True,
            current_soc_pct=30.0,
            capacity_kwh=60.0,
            max_charge_kw=7.4,
            now=datetime(2024, 6, 15, 6, 0, tzinfo=UTC),
            soc_targets=(60.0,),
        )

        assert result.state == "ready"
        assert len(result.points) == 2
        # base_input itself must never be mutated by the clone-and-override.
        assert base_input.ev_planned_load_target_soc_pct == original_primary_target
        assert base_input.ev_planned_load_deadline == original_primary_deadline
        assert base_input.ev_second_planned_load_target_soc_pct == 80.0


# ---------------------------------------------------------------------------
# TestDeadlineColumns — configured deadlines and the target deadline (#1344)
# ---------------------------------------------------------------------------


def _fake_run_planner(
    cost_by_deadline: dict[datetime, float] | None = None,
) -> tuple[Callable[[PlannerInput], MagicMock], list[PlannerInput]]:
    """Return a run_planner stand-in that records each clone it is given.

    The cost of a clone is its target SoC, plus an optional per-deadline
    offset, so tests can tell columns apart without a real solve.
    """
    seen: list[PlannerInput] = []

    def _run(clone: PlannerInput) -> MagicMock:
        seen.append(clone)
        deadline = (
            clone.ev_planned_load_deadline or clone.ev_second_planned_load_deadline
        )
        offset = (cost_by_deadline or {}).get(deadline, 0.0) if deadline else 0.0
        target = (
            clone.ev_planned_load_target_soc_pct
            or clone.ev_second_planned_load_target_soc_pct
        )
        output = MagicMock()
        output.plan_cost.total_cost = float(target) / 10.0 + offset
        return output

    return _run, seen


def _compute(
    base_input: PlannerInput, now: datetime, **kwargs: Any
) -> EVSoCEconomicsResult:
    """Run compute_ev_soc_economics for the primary EV at 50 % SoC."""
    return compute_ev_soc_economics(
        base_input,
        is_second=kwargs.pop("is_second", False),
        current_soc_pct=50.0,
        capacity_kwh=77.0,
        max_charge_kw=11.0,
        now=now,
        **kwargs,
    )


class TestDeadlineColumns:
    """Configured deadlines and the always-present target deadline (#1344)."""

    _NOW = datetime(2024, 6, 15, 14, 0, tzinfo=UTC)

    def test_default_deadlines_unchanged_without_target(self):
        """No deadlines argument and no target deadline: the 08:00/17:00 table."""
        base_input = _make_planner_input()
        run, _ = _fake_run_planner()
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner",
            side_effect=run,
        ):
            result = _compute(base_input, self._NOW, soc_targets=(60.0,))

        assert [(p.deadline_label, p.deadline) for p in result.points] == [
            ("17:00", datetime(2024, 6, 15, 17, 0, tzinfo=UTC)),
            ("08:00", datetime(2024, 6, 16, 8, 0, tzinfo=UTC)),
        ]
        assert not any(p.is_target_deadline for p in result.points)
        assert result.target_deadline is None
        assert result.configured_deadlines == ((8, 0), (17, 0))

    def test_custom_minute_deadline(self):
        """A configured 06:30 resolves to the next 06:30 and solves with it."""
        base_input = _make_planner_input()
        run, seen = _fake_run_planner()
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner",
            side_effect=run,
        ):
            result = _compute(
                base_input, self._NOW, soc_targets=(60.0,), deadlines=((6, 30),)
            )

        expected = datetime(2024, 6, 16, 6, 30, tzinfo=UTC)
        assert [(p.deadline_label, p.deadline) for p in result.points] == [
            ("06:30", expected)
        ]
        # seen[0] is the baseline solve (#1348), which keeps the base deadline.
        assert [clone.ev_planned_load_deadline for clone in seen[1:]] == [expected]

    def test_no_configured_deadlines_and_no_target_is_an_empty_table(self):
        """An empty list with no target deadline yields no points and no solve."""
        base_input = _make_planner_input()
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner"
        ) as mock_run:
            result = _compute(base_input, self._NOW, deadlines=())

        assert result.state == "ready"
        assert result.points == []
        mock_run.assert_not_called()

    def test_target_deadline_added_as_its_own_column(self):
        """The EV's deadline gets an exact-time column, flagged and ordered."""
        base_input = _make_planner_input()
        target = datetime(2024, 6, 16, 7, 15, tzinfo=UTC)
        base_input.ev_planned_load_deadline = target
        run, seen = _fake_run_planner({target: 1.0})
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner",
            side_effect=run,
        ):
            result = _compute(base_input, self._NOW, soc_targets=(60.0,))

        assert [
            (p.deadline_label, p.deadline, p.is_target_deadline) for p in result.points
        ] == [
            ("17:00", datetime(2024, 6, 15, 17, 0, tzinfo=UTC), False),
            ("07:15", target, True),
            ("08:00", datetime(2024, 6, 16, 8, 0, tzinfo=UTC), False),
        ]
        assert result.target_deadline == target
        target_point = result.points[1]
        assert target_point.plan_total_cost == pytest.approx(7.0)
        # The baseline keeps the base deadline (the target), so it also gets
        # the +1.0 offset: 50 / 10 + 1.0 = 6.0, and the charging cost is 1.0.
        assert target_point.total_cost == pytest.approx(1.0)
        assert target in [clone.ev_planned_load_deadline for clone in seen]
        # The base input keeps its own deadline: only clones are overridden.
        assert base_input.ev_planned_load_deadline == target

    def test_target_deadline_merges_with_equal_configured_deadline(self):
        """A target at a configured time of day is one column, not two."""
        base_input = _make_planner_input()
        target = datetime(2024, 6, 16, 8, 0, tzinfo=UTC)
        base_input.ev_planned_load_deadline = target
        run, _ = _fake_run_planner()
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner",
            side_effect=run,
        ):
            result = _compute(base_input, self._NOW, soc_targets=(60.0,))

        assert [
            (p.deadline_label, p.deadline, p.is_target_deadline) for p in result.points
        ] == [
            ("17:00", datetime(2024, 6, 15, 17, 0, tzinfo=UTC), False),
            ("08:00", target, True),
        ]

    def test_target_deadline_alone_when_nothing_configured(self):
        """With no configured deadlines, the target deadline is the only column."""
        base_input = _make_planner_input()
        target = datetime(2024, 6, 16, 6, 45, tzinfo=UTC)
        base_input.ev_planned_load_deadline = target
        run, _ = _fake_run_planner()
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner",
            side_effect=run,
        ):
            result = _compute(
                base_input, self._NOW, soc_targets=(60.0, 80.0), deadlines=()
            )

        assert {p.deadline_label for p in result.points} == {"06:45"}
        assert all(p.is_target_deadline for p in result.points)
        assert len(result.points) == 2

    def test_passed_target_deadline_gets_no_column(self):
        """A target deadline at or before now is left out entirely."""
        base_input = _make_planner_input()
        base_input.ev_planned_load_deadline = self._NOW
        run, _ = _fake_run_planner()
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner",
            side_effect=run,
        ):
            result = _compute(base_input, self._NOW, soc_targets=(60.0,))

        assert result.target_deadline is None
        assert {p.deadline_label for p in result.points} == {"08:00", "17:00"}
        assert not any(p.is_target_deadline for p in result.points)

    def test_target_deadline_reported_when_not_connected(self):
        """Guard states still carry the target deadline for the sensor."""
        base_input = _make_planner_input(ev_connected=False)
        target = datetime(2024, 6, 16, 7, 0, tzinfo=UTC)
        base_input.ev_planned_load_deadline = target

        result = _compute(base_input, self._NOW, deadlines=((9, 0),))

        assert result.state == "not_connected"
        assert result.points == []
        assert result.target_deadline == target
        assert result.configured_deadlines == ((9, 0),)

    def test_second_ev_uses_its_own_target_deadline(self):
        """is_second reads ev_second_planned_load_deadline, not the primary one."""
        base_input = _make_planner_input(second_ev_enabled=True)
        base_input.ev_planned_load_deadline = datetime(2024, 6, 16, 5, 0, tzinfo=UTC)
        second_target = datetime(2024, 6, 16, 9, 30, tzinfo=UTC)
        base_input.ev_second_planned_load_deadline = second_target
        run, seen = _fake_run_planner()
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner",
            side_effect=run,
        ):
            result = _compute(
                base_input,
                self._NOW,
                is_second=True,
                soc_targets=(60.0,),
                deadlines=((18, 0),),
            )

        assert result.target_deadline == second_target
        assert [(p.deadline_label, p.is_target_deadline) for p in result.points] == [
            ("18:00", False),
            ("09:30", True),
        ]
        # seen[0] is the baseline solve (#1348), which keeps the base deadline.
        assert [c.ev_second_planned_load_deadline for c in seen[1:]] == [
            datetime(2024, 6, 15, 18, 0, tzinfo=UTC),
            second_target,
        ]

    def test_target_label_is_local_time(self):
        """The target column's label is its wall-clock time in now's zone."""
        zone = ZoneInfo("Europe/Copenhagen")
        now = datetime(2024, 6, 15, 14, 0, tzinfo=zone)
        base_input = _make_planner_input()
        # 05:20 UTC is 07:20 in Copenhagen summer time.
        base_input.ev_planned_load_deadline = datetime(2024, 6, 16, 5, 20, tzinfo=UTC)
        run, _ = _fake_run_planner()
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner",
            side_effect=run,
        ):
            result = _compute(base_input, now, soc_targets=(60.0,), deadlines=())

        assert [p.deadline_label for p in result.points] == ["07:20"]

    def test_attributes_expose_target_deadline_fields(self):
        """as_attributes carries target_deadline, configured_deadlines and flags."""
        base_input = _make_planner_input()
        target = datetime(2024, 6, 16, 7, 15, tzinfo=UTC)
        base_input.ev_planned_load_deadline = target
        run, _ = _fake_run_planner()
        with patch(
            "custom_components.hsem.planner.ev_soc_economics.run_planner",
            side_effect=run,
        ):
            attrs = _compute(
                base_input, self._NOW, soc_targets=(60.0,), deadlines=((17, 0),)
            ).as_attributes()

        assert attrs["target_deadline"] == target.isoformat()
        assert attrs["configured_deadlines"] == ["17:00"]
        assert [p["is_target_deadline"] for p in attrs["points"]] == [False, True]

    def test_attributes_target_deadline_null_when_unset(self):
        """No target deadline serialises as None."""
        attrs = _compute(
            _make_planner_input(ev_connected=False), self._NOW
        ).as_attributes()
        assert attrs["target_deadline"] is None
        assert attrs["configured_deadlines"] == ["08:00", "17:00"]


class TestDeadlineHelpers:
    """effective_target_deadline() and deadline_columns() directly."""

    _NOW = datetime(2024, 6, 15, 14, 0, tzinfo=UTC)

    def test_effective_target_deadline(self):
        """Only a deadline strictly after now is kept."""
        later = self._NOW + timedelta(minutes=1)
        assert effective_target_deadline(None, self._NOW) is None
        assert effective_target_deadline(self._NOW, self._NOW) is None
        assert effective_target_deadline(later, self._NOW) == later

    def test_duplicate_configured_deadlines_collapse(self):
        """The same configured time twice is one column."""
        columns = deadline_columns(self._NOW, ((8, 0), (8, 0)), None)
        assert [label for label, _, _ in columns] == ["08:00"]


# ---------------------------------------------------------------------------
# TestChargingCost — the baseline solve and the EV charging cost (#1348)
# ---------------------------------------------------------------------------


def _plan_cost_by_target(
    costs: dict[float, float],
) -> tuple[Callable[[PlannerInput], MagicMock], list[PlannerInput]]:
    """Return a run_planner stand-in whose plan cost depends on the target only.

    ``costs`` maps the primary EV's target SoC to the whole-plan cost; the
    second EV's target is read when the primary target is not in ``costs``.
    """
    seen: list[PlannerInput] = []

    def _run(clone: PlannerInput) -> MagicMock:
        seen.append(clone)
        target = clone.ev_planned_load_target_soc_pct
        if target not in costs:
            target = clone.ev_second_planned_load_target_soc_pct
        output = MagicMock()
        output.plan_cost.total_cost = costs[target]
        return output

    return _run, seen


class TestChargingCost:
    """Each row's total_cost is its plan cost minus the EV's baseline (#1348)."""

    _NOW = datetime(2024, 6, 15, 14, 0, tzinfo=UTC)
    _PATCH = "custom_components.hsem.planner.ev_soc_economics.run_planner"

    def _run(
        self,
        base_input: PlannerInput,
        run: Callable[[PlannerInput], MagicMock],
        **kw: Any,
    ) -> EVSoCEconomicsResult:
        with patch(self._PATCH, side_effect=run):
            return compute_ev_soc_economics(
                base_input,
                is_second=kw.pop("is_second", False),
                current_soc_pct=kw.pop("current_soc_pct", 72.0),
                capacity_kwh=77.0,
                max_charge_kw=11.0,
                now=self._NOW,
                **kw,
            )

    def test_total_cost_is_plan_cost_minus_baseline(self):
        """72 % SoC: baseline 133.38 (house only), 80 % plan 151.6 -> 18.22."""
        run, _ = _plan_cost_by_target({72.0: 133.38, 80.0: 151.6, 100.0: 196.41})
        result = self._run(
            _make_planner_input(current_soc=72.0), run, deadlines=((8, 0),)
        )

        assert result.baseline_plan_cost == pytest.approx(133.38)
        assert [p.target_soc_pct for p in result.points] == [80.0, 100.0]
        assert result.points[0].total_cost == pytest.approx(18.22)
        assert result.points[0].plan_total_cost == pytest.approx(151.6)
        assert result.points[1].total_cost == pytest.approx(63.03)
        assert result.points[1].plan_total_cost == pytest.approx(196.41)

    def test_deltas_unchanged_by_the_baseline(self):
        """The baseline cancels out of the step between two rows."""
        run, _ = _plan_cost_by_target({72.0: 133.38, 80.0: 151.6, 100.0: 196.41})
        result = self._run(
            _make_planner_input(current_soc=72.0), run, deadlines=((8, 0),)
        )

        upper = result.points[1]
        assert upper.delta_from_previous == pytest.approx(196.41 - 151.6)
        assert upper.delta_per_10pct == pytest.approx((196.41 - 151.6) / 2.0)
        assert result.points[0].delta_from_previous is None

    def test_one_baseline_solve_per_ev(self):
        """Several columns share a single baseline solve."""
        run, seen = _plan_cost_by_target({72.0: 1.0, 80.0: 2.0, 100.0: 3.0})
        result = self._run(
            _make_planner_input(current_soc=72.0),
            run,
            deadlines=((8, 0), (12, 0), (17, 0)),
        )

        baselines = [c for c in seen if c.ev_planned_load_target_soc_pct == 72.0]
        assert len(baselines) == 1
        assert len(seen) == 1 + len(result.points) == 1 + 2 * 3

    def test_baseline_clone_overrides_only_the_target(self):
        """The baseline keeps every other input, including the deadline."""
        base_input = _make_planner_input(current_soc=72.0)
        base_input.ev_planned_load_deadline = datetime(2024, 6, 16, 7, 0, tzinfo=UTC)
        run, seen = _plan_cost_by_target({72.0: 1.0, 80.0: 2.0, 100.0: 3.0})
        self._run(base_input, run, deadlines=())

        baseline = seen[0]
        assert baseline is not base_input
        assert baseline == replace(base_input, ev_planned_load_target_soc_pct=72.0)
        # The base input itself is never mutated.
        assert base_input.ev_planned_load_target_soc_pct == pytest.approx(80.0)

    def test_second_ev_baseline_overrides_second_target(self):
        """is_second sets ev_second_planned_load_target_soc_pct for the baseline."""
        base_input = _make_planner_input(
            second_ev_enabled=True, second_current_soc=30.0
        )
        run, seen = _plan_cost_by_target({30.0: 10.0, 50.0: 12.0, 100.0: 20.0})
        result = self._run(
            base_input,
            run,
            is_second=True,
            current_soc_pct=30.0,
            soc_targets=(50.0, 100.0),
            deadlines=((8, 0),),
        )

        baseline = seen[0]
        assert baseline.ev_second_planned_load_target_soc_pct == pytest.approx(30.0)
        assert baseline.ev_planned_load_target_soc_pct == pytest.approx(80.0)
        assert result.baseline_plan_cost == pytest.approx(10.0)
        assert [p.total_cost for p in result.points] == pytest.approx([2.0, 10.0])

    def test_no_baseline_when_ev_short_circuits(self):
        """A guard state solves nothing, baseline included."""
        with patch(self._PATCH) as mock_run:
            result = compute_ev_soc_economics(
                _make_planner_input(ev_connected=False),
                is_second=False,
                current_soc_pct=72.0,
                capacity_kwh=77.0,
                max_charge_kw=11.0,
                now=self._NOW,
            )

        mock_run.assert_not_called()
        assert result.baseline_plan_cost is None
        assert result.as_attributes()["baseline_plan_cost"] is None

    def test_no_baseline_when_no_row_is_kept(self):
        """Every target below the SoC: no rows, so no baseline solve either."""
        with patch(self._PATCH) as mock_run:
            result = compute_ev_soc_economics(
                _make_planner_input(current_soc=95.0),
                is_second=False,
                current_soc_pct=95.0,
                capacity_kwh=77.0,
                max_charge_kw=11.0,
                now=self._NOW,
                soc_targets=(50.0, 80.0),
            )

        mock_run.assert_not_called()
        assert result.state == "ready"
        assert result.points == []
        assert result.baseline_plan_cost is None

    def test_negative_charging_cost_is_not_clamped(self):
        """A plan that spends less money than the baseline shows a negative cost."""
        run, _ = _plan_cost_by_target({72.0: 10.0, 80.0: 9.5, 100.0: 12.0})
        result = self._run(
            _make_planner_input(current_soc=72.0), run, deadlines=((8, 0),)
        )

        assert result.points[0].total_cost == pytest.approx(-0.5)

    def test_attributes_expose_plan_and_baseline_costs(self):
        """as_attributes carries plan_total_cost per point and the baseline."""
        run, _ = _plan_cost_by_target({72.0: 133.38171, 80.0: 151.6, 100.0: 196.41})
        attrs = self._run(
            _make_planner_input(current_soc=72.0), run, deadlines=((8, 0),)
        ).as_attributes()

        assert attrs["baseline_plan_cost"] == pytest.approx(133.3817)
        assert attrs["points"][0]["total_cost"] == pytest.approx(18.2183)
        assert attrs["points"][0]["plan_total_cost"] == pytest.approx(151.6)

"""Tests for EV SoC economics (issue #903).

All tests are pure-Python and carry no Home Assistant dependencies beyond
the ``STATE_UNAVAILABLE`` string constant already used throughout the
planner layer.

Test classes
------------
TestNextTimeOfDay              — next_time_of_day() helper
TestComputeEvSocEconomicsGuards — guard-clause short-circuits
TestComputeEvSocEconomics       — end-to-end cost/feasibility computation
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from homeassistant.const import STATE_UNAVAILABLE

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner.ev_soc_economics import (
    compute_ev_soc_economics,
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

    def test_targets_at_or_below_current_soc_cost_zero_no_solve(self):
        """Targets <= current SoC cost 0.0 and skip run_planner() entirely."""
        base_input = _make_planner_input(current_soc=80.0)
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
                current_soc_pct=80.0,
                capacity_kwh=77.0,
                max_charge_kw=11.0,
                now=datetime(2024, 6, 15, 14, 0, tzinfo=UTC),
                soc_targets=(50.0, 60.0, 70.0, 80.0, 100.0),
            )

        # Only the 100% target (per deadline) is above current SoC (80%).
        assert call_count == 2
        already_met = [p for p in result.points if p.target_soc_pct <= 80.0]
        assert already_met
        for p in already_met:
            assert p.total_cost == pytest.approx(0.0)

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

"""Closed-loop replay of the dynamic floor's reserve profile (issue #1188).

A value derived from a plan and fed back into the next plan's constraints
needs more than a single-cycle test (issue #1140 found an oscillation only in
a replay).  Here the real ``run_planner`` replans every hour.  The floor of
replan k comes from replan k's own floor-free reference solve, the live slot
of each published plan is executed as planned, and the next replan starts
from the resulting SoC.

Fixture: the #1125 shape — 10 kWh battery, 5 % hardware floor, a 0.15 night
(no affordable refill), 0.25 peaks, PV surplus from 08:00.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from custom_components.hsem import coordinator_builder
from custom_components.hsem.coordinator_dynamic_floor import (
    compute_dynamic_floor_from_plan,
    floor_required_at_slot_end,
)
from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.utils.dynamic_floor import DynamicDischargeFloor

# Each fixture is 24 replans of two solves; allow for a loaded CI runner.
pytestmark = [pytest.mark.slow, pytest.mark.timeout(180)]

_TZ = timezone(timedelta(hours=3))
_DAY0 = datetime(2026, 9, 28, tzinfo=_TZ)
_START = _DAY0.replace(hour=21)
_REFILL = _DAY0 + timedelta(days=1, hours=8)
_RATED_KWH = 10.0
_HARDWARE_FLOOR_PCT = 5.0
_PEAK_PRICE = 0.25
_LOAD = [0.5] * 6 + [0.7] * 3 + [0.5] * 8 + [0.8] * 5 + [0.7] * 2
_PV = [0.0] * 7 + [0.2, 0.8, 1.6, 2.4, 2.8, 3.0, 2.8, 2.3, 1.6, 0.8, 0.2] + [0.0] * 6
_FIRST_DAY = [0.12] * 6 + [0.22] * 3 + [0.12] * 8 + [0.21] * 4 + [0.19] * 3
_LATER_DAY = [0.19] * 2 + [0.15] * 4 + [0.25] * 3 + [0.12] * 7 + [0.25] * 5 + [0.15] * 3


def _planner_input(now: datetime, soc_pct: float) -> PlannerInput:
    """Return the planner input for a replan at *now* with the given SoC."""
    day = (now.date() - _DAY0.date()).days
    return PlannerInput(
        now_iso=now.isoformat(),
        interval_minutes=60,
        interval_length_hours=48,
        battery_soc_pct=soc_pct,
        battery_rated_capacity_kwh=_RATED_KWH,
        battery_end_of_discharge_soc_pct=_HARDWARE_FLOOR_PCT,
        battery_max_charge_power_w=5000.0,
        battery_max_discharge_power_w=5000.0,
        battery_purchase_price=3000.0,
        battery_expected_cycles=6000,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=[
            HourlyConsumptionAverage(hour=h, avg_1d=v, avg_3d=v, avg_7d=v, avg_14d=v)
            for h, v in enumerate(_LOAD)
        ],
        price_points=[
            PricePoint(
                hour=hour,
                import_price=price,
                export_price=max(price - 0.10, 0.0),
                day_offset=offset,
            )
            for offset in (0, 1)
            for hour, price in enumerate(
                _FIRST_DAY if day + offset == 0 else _LATER_DAY
            )
        ],
        solcast_slots=[
            SolcastSlot(hour=hour, pv_estimate=pv, day_offset=offset)
            for offset in (0, 1)
            for hour, pv in enumerate(_PV)
        ],
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        time_discount_rate=1.0,
        excess_export_enabled=True,
    )


def _live(soc_pct: float) -> LiveState:
    live = LiveState()
    live.huawei_batteries_rated_capacity_wh = _RATED_KWH * 1000.0
    live.huawei_batteries_end_of_discharge_soc_pct = _HARDWARE_FLOOR_PCT
    live.huawei_batteries_charging_cutoff_capacity_pct = 100.0
    live.huawei_batteries_soc_pct = soc_pct
    return live


@dataclass
class _Replay:
    """What a closed-loop run executed, one entry per replan."""

    cash: float = 0.0
    soc_end_pct: float = 0.0
    margin: float = 0.0
    starts: list[datetime] = field(default_factory=list)
    socs: list[float] = field(default_factory=list)
    floors: list[float] = field(default_factory=list)
    end_floors: list[float] = field(default_factory=list)
    profiles: list[dict[str, float]] = field(default_factory=list)
    discharged: list[float] = field(default_factory=list)
    imported: list[float] = field(default_factory=list)
    recommendations: list[str | None] = field(default_factory=list)
    winners: list[str | None] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


def _replay(soc_pct: float, steps: int, *, with_profile: bool) -> _Replay:
    """Replan hourly for *steps* hours and execute each plan's live slot."""
    run = _Replay()
    floor_model = DynamicDischargeFloor()
    now = _START
    for _ in range(steps):
        planner_input = _planner_input(now, soc_pct)
        reference = run_planner(planner_input)
        with patch.object(coordinator_builder, "hsem_now", return_value=now):
            recs = coordinator_builder.generate_recommendation_intervals(60, 48)
        floor_pct, _diag, profile = compute_dynamic_floor_from_plan(
            floor_model, recs, reference, planner_input, _live(soc_pct), now
        )
        end_floor_pct = floor_required_at_slot_end(profile, now, floor_pct)
        floor_model.correct_margin(
            soc_pct, end_floor_pct if with_profile else floor_pct, now=now
        )
        plan = run_planner(
            replace(
                planner_input,
                dynamic_discharge_floor_pct=floor_pct,
                dynamic_floor_profile=profile if with_profile else None,
            )
        )
        slot = next(s for s in plan.slots if s.start <= now < s.end)
        run.cash += slot.grid_import_kwh * slot.price.import_price
        run.cash -= slot.grid_export_kwh * slot.price.export_price
        run.starts.append(now)
        run.socs.append(soc_pct)
        run.floors.append(floor_pct)
        run.end_floors.append(end_floor_pct)
        run.profiles.append(dict(profile))
        run.discharged.append(slot.batteries_discharged_kwh)
        run.imported.append(slot.grid_import_kwh)
        run.recommendations.append(slot.recommendation)
        run.winners.append(plan.winner_name)
        run.violations.extend(plan.plan_consistency_violations)
        soc_pct = slot.estimated_battery_soc_pct
        now += timedelta(hours=1)
    run.soc_end_pct = soc_pct
    run.margin = floor_model.safety_margin
    return run


@pytest.fixture(scope="module")
def profile_run() -> _Replay:
    """24 hourly replans from 21:00 at 68 % with the reserve profile."""
    return _replay(68.0, 24, with_profile=True)


@pytest.fixture(scope="module")
def constant_run() -> _Replay:
    """The same 24 replans with the floor held constant over the horizon."""
    return _replay(68.0, 24, with_profile=False)


def _first_bridge(run: _Replay) -> list[int]:
    """Return the replan indices before the first night's solar refill."""
    return [i for i, start in enumerate(run.starts) if start < _REFILL]


class TestClosedLoop:
    """The plan and the next replan's floor agree, replan after replan."""

    def test_every_replan_is_solved_by_the_milp(self, profile_run: _Replay) -> None:
        assert set(profile_run.winners) == {"milp"}
        assert profile_run.violations == []

    def test_each_floor_is_what_the_previous_replan_predicted(
        self, profile_run: _Replay
    ) -> None:
        """No oscillation: replan k+1's floor is replan k's profile for it.

        Exact within a calendar day.  At midnight tomorrow's PV becomes
        today's and loses its 10 % confidence decay, which shortens the bridge
        by 0.02 kWh (0.24 points); that is the forecast moving, not the floor.
        """
        bridge = _first_bridge(profile_run)

        assert len(bridge) == 11
        for prev, cur in zip(bridge, bridge[1:]):
            start = profile_run.starts[cur]
            predicted = profile_run.profiles[prev][start.isoformat()]
            tolerance = 0.3 if start.hour == 0 else 1e-6
            assert profile_run.floors[cur] == pytest.approx(predicted, abs=tolerance)

    def test_floor_declines_through_the_night(self, profile_run: _Replay) -> None:
        floors = [profile_run.floors[i] for i in _first_bridge(profile_run)]

        assert floors[0] > 68.0
        assert all(a > b for a, b in zip(floors, floors[1:]))
        assert profile_run.floors[len(floors)] == pytest.approx(_HARDWARE_FLOOR_PCT)

    def test_executed_soc_never_ends_a_slot_below_its_reserve(
        self, profile_run: _Replay
    ) -> None:
        """Each slot ends at or above the floor its own replan required."""
        socs_after = [*profile_run.socs[1:], profile_run.soc_end_pct]
        for soc_before, soc_after, end_floor in zip(
            profile_run.socs, socs_after, profile_run.end_floors
        ):
            assert soc_after >= min(soc_before, end_floor) - 0.02

    def test_battery_holds_then_serves_the_night(self, profile_run: _Replay) -> None:
        """Below the reserve it holds one slot, then follows the reserve down."""
        bridge = _first_bridge(profile_run)

        assert profile_run.discharged[0] == pytest.approx(0.0)
        assert profile_run.imported[0] > 0.5
        # From the second slot on the reserve is below the battery.  The
        # plan may still buy one cheap night hour to keep energy for the
        # 0.25 morning peak; that is economics, not the floor.
        assert sum(profile_run.discharged[i] for i in bridge[1:]) > 5.0
        assert sum(profile_run.discharged[i] > 0.4 for i in bridge[1:]) >= 9
        assert sum(profile_run.imported[i] for i in bridge[1:]) < 0.6

    def test_constant_floor_held_much_longer(
        self, profile_run: _Replay, constant_run: _Replay
    ) -> None:
        """Before #1188 the battery only moved once a replan lowered the floor."""
        bridge = _first_bridge(constant_run)

        assert sum(constant_run.imported[i] for i in bridge) > (
            sum(profile_run.imported[i] for i in bridge) + 1.0
        )

    def test_realised_cash_is_not_worse_than_the_constant_floor(
        self, profile_run: _Replay, constant_run: _Replay
    ) -> None:
        """Even with the SoC difference valued at the peak import price."""
        soc_gap_kwh = (
            max(constant_run.soc_end_pct - profile_run.soc_end_pct, 0.0)
            / 100.0
            * _RATED_KWH
        )

        assert profile_run.cash + soc_gap_kwh * _PEAK_PRICE <= constant_run.cash + 0.01

    def test_following_the_reserve_does_not_move_the_margin(
        self, profile_run: _Replay
    ) -> None:
        assert profile_run.margin == pytest.approx(1.15)

"""Closed-loop regression for issue #1198 — the floor flipped between replans.

The dynamic discharge floor is computed from a floor-free reference solve of
the same replan (issue #1140).  When several night slots have the same import
price that solve is free to put its grid charge in any of them, and it does
not pick the same one on every replan.  The bridge scan tested the charge
against the consumption bridged up to the charge's own slot, so:

- a charge early in the night covered the little consumption before it, the
  bridge ended there, and the floor was the configured minimum;
- the same charge three hours later did not, the bridge ran on to the solar
  surplus, and a 24-30 % reserve appeared.

On consecutive hourly replans the floor went 5.0 / 29.8 / 5.0 / 24.0 / 5.0 /
26.6 % and the executed slot alternated between holding and discharging.

This replays that night through the real ``run_planner``: every replan solves
the reference plan, takes its own floor from it, solves again with the floor
and executes the live slot.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.hsem import coordinator_builder
from custom_components.hsem.coordinator_dynamic_floor import (
    compute_dynamic_floor_from_plan,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils.dynamic_floor import DynamicDischargeFloor
from custom_components.hsem.utils.recommendations import Recommendations
from tests.test_dynamic_floor_reference_plan import (
    _HARDWARE_FLOOR_PCT,
    _MIDNIGHT,
    _PV_TOMORROW,
    _live,
    _planner_input,
)

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_WAIT = Recommendations.BatteriesWaitMode.value
_DISCHARGE = Recommendations.BatteriesDischargeMode.value
_EXPORT_SPIKE = 0.45
#: The evening the battery was sold into the spike: 23:00, at the floor.
_START = _MIDNIGHT + timedelta(days=1, hours=23)


def _prices(day: int) -> list[tuple[float, float]]:
    """Return ``(import, export)`` per hour of *day* (0 = the fixture's day).

    Evenings cost 0.19-0.21, the night 0.15 from 02:00 to 06:00 (and at
    23:00 from day 1 on), mornings and evenings peak at 0.25, the day costs
    0.12.  Export pays 0.45 at 21:00-23:00, so the battery is sold in the
    evening and the night is bought back.
    """
    if day == 0:
        imports = [0.12] * 6 + [0.22] * 3 + [0.12] * 8 + [0.21] * 4 + [0.19] * 3
    else:
        imports = [0.19] * 2 + [0.15] * 4 + [0.25] * 3 + [0.12] * 7 + [0.25] * 5
        imports += [0.15] * 3
    prices = [(price, max(price - 0.10, 0.0)) for price in imports]
    for hour in (21, 22):
        prices[hour] = (max(prices[hour][0], _EXPORT_SPIKE + 0.02), _EXPORT_SPIKE)
    return prices


def _input(now: datetime, soc_pct: float) -> PlannerInput:
    """Return the #1125 house at *now*: 10 kWh, 5 % floor, hourly slots, 48 h."""
    day = (now.date() - _MIDNIGHT.date()).days
    return replace(
        _planner_input(0.15),
        now_iso=now.isoformat(),
        battery_soc_pct=soc_pct,
        excess_export_enabled=True,
        price_points=[
            PricePoint(hour=hour, import_price=imp, export_price=exp, day_offset=offset)
            for offset in (0, 1)
            for hour, (imp, exp) in enumerate(_prices(day + offset))
        ],
        solcast_slots=[
            SolcastSlot(hour=hour, pv_estimate=pv, day_offset=offset)
            for offset in (0, 1)
            for hour, pv in enumerate(_PV_TOMORROW)
        ],
    )


@dataclass(frozen=True)
class _Replan:
    """One closed-loop step."""

    now: datetime
    soc_pct: float
    floor_pct: float
    refill_type: str
    executed: str | None


def _floor(now: datetime, soc_pct: float) -> tuple[float, dict, PlannerInput]:
    """Return this replan's floor from its own floor-free reference solve."""
    planner_input = _input(now, soc_pct)
    with patch.object(coordinator_builder, "hsem_now", return_value=now):
        recommendations = coordinator_builder.generate_recommendation_intervals(60, 48)
    live = _live()
    live.huawei_batteries_soc_pct = soc_pct
    floor_pct, diag, profile = compute_dynamic_floor_from_plan(
        DynamicDischargeFloor(),
        recommendations,
        run_planner(planner_input),
        planner_input,
        live,
        now,
    )
    return (
        floor_pct,
        diag,
        replace(
            planner_input,
            dynamic_discharge_floor_pct=floor_pct,
            dynamic_floor_profile=profile,
        ),
    )


def _closed_loop(start: datetime, soc_pct: float, steps: int) -> list[_Replan]:
    """Replan hourly from *start*, executing each plan's live slot."""
    now, replans = start, []
    for _ in range(steps):
        floor_pct, diag, final_input = _floor(now, soc_pct)
        plan = run_planner(final_input)
        live_slot = next(slot for slot in plan.slots if slot.start <= now < slot.end)
        replans.append(
            _Replan(
                now, soc_pct, floor_pct, diag["refill_type"], live_slot.recommendation
            )
        )
        soc_pct = live_slot.estimated_battery_soc_pct
        now += timedelta(hours=1)
    return replans


def _rises(replans: list[_Replan]) -> list[tuple[_Replan, _Replan]]:
    """Return consecutive replans whose floor rose by more than one point."""
    return [
        (before, after)
        for before, after in zip(replans, replans[1:])
        if after.floor_pct > before.floor_pct + 1.0
    ]


@pytest.fixture(scope="module")
def night() -> list[_Replan]:
    """23:00 to 07:00 after the evening sale: nine hourly replans."""
    return _closed_loop(_START, _HARDWARE_FLOOR_PCT, 9)


class TestFloorDoesNotFlipOverTheNight:
    """The night the issue reported, replayed through the real planner."""

    def test_floor_rises_once_where_the_next_bridge_starts(
        self, night: list[_Replan]
    ) -> None:
        """Released while the plan refills from the grid, then one new bridge.

        Before #1198: 5.0 / 29.8 / 5.0 / 24.0 / 5.0 / 26.6 % — three rises.
        """
        rises = _rises(night)

        assert len(rises) == 1
        before, after = rises[0]
        # The grid refill has happened; the bridge to the PV surplus starts.
        assert before.refill_type == "grid_charge"
        assert after.refill_type == "solar_surplus"
        # Within that bridge the floor only declines.
        tail = night[night.index(after) :]
        assert all(
            later.floor_pct <= earlier.floor_pct + 1e-6
            for earlier, later in zip(tail, tail[1:])
        )

    def test_floor_is_released_through_the_whole_cheap_window(
        self, night: list[_Replan]
    ) -> None:
        """Every replan up to the grid refill sees a covering planned charge."""
        charged_at = next(
            index
            for index, replan in enumerate(night)
            if replan.executed == Recommendations.BatteriesChargeGrid.value
            and index > 0
        )

        for replan in night[: charged_at + 1]:
            assert replan.refill_type == "grid_charge"
            assert replan.floor_pct == pytest.approx(_HARDWARE_FLOOR_PCT)

    def test_no_hold_between_two_discharges(self, night: list[_Replan]) -> None:
        """discharge / wait / discharge on consecutive slots was the symptom."""
        executed = [replan.executed for replan in night]

        assert not any(
            (first, second, third) == (_DISCHARGE, _WAIT, _DISCHARGE)
            or (first, second, third) == (_WAIT, _DISCHARGE, _WAIT)
            for first, second, third in zip(executed, executed[1:], executed[2:])
        )

    def test_same_inputs_give_the_same_floor_on_every_solve(self) -> None:
        """The floor is a function of the replan's inputs, not of solver ties."""
        now, soc_pct = _START + timedelta(hours=1), 15.31

        floors = [_floor(now, soc_pct)[0] for _ in range(3)]

        assert floors == pytest.approx([floors[0]] * 3)

"""Regression tests for issue #1094 — SoC estimate anchored to an unreached floor.

A reporter's battery read 11 % (0.6 kWh above a 5 % hardware floor on a
10 kWh pack) while the dynamic discharge floor was 75.74 %.  The current slot
published ``estimated_battery_soc_pct = 75.74`` next to
``estimated_battery_capacity_kwh = 0.0``: the model measured energy from the
effective floor, clamped the below-floor battery to 0 kWh above it, and then
converted that 0 kWh back to "floor %".  The same origin also capped the
model's charge headroom at ``rated × (100 − 75.74) %`` = 2.43 kWh, so the plan
called the battery full at a real ~35 % and exported PV it could have stored.

The dynamic floor is now capped at the live SoC, so the origin is a SoC the
battery is actually at.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.planner.engine_core import run_planner
from custom_components.hsem.utils.datetime_utils import as_tz
from tests.planner.fixtures import make_summer_day_input

_RATED_KWH = 10.0
_HARDWARE_FLOOR_PCT = 5.0
_REPORTED_FLOOR_PCT = 75.74
_REPORTED_SOC_PCT = 11.0


def _plan(
    soc_pct: float, dynamic_floor_pct: float | None
) -> tuple[PlannerOutput, list[PlannedSlot]]:
    """Run the summer fixture and return the output plus its non-past slots."""
    inp = make_summer_day_input(
        battery_soc_pct=soc_pct,
        battery_rated_capacity_kwh=_RATED_KWH,
        battery_end_of_discharge_soc_pct=_HARDWARE_FLOOR_PCT,
    )
    inp.dynamic_discharge_floor_pct = dynamic_floor_pct
    output = run_planner(inp)
    now = datetime.fromisoformat(inp.now_iso)
    future = [slot for slot in output.slots if as_tz(slot.end, now.tzinfo) > now]
    return output, future


@pytest.mark.parametrize(
    ("soc_pct", "dynamic_floor_pct", "origin_pct"),
    [
        pytest.param(
            _REPORTED_SOC_PCT,
            _REPORTED_FLOOR_PCT,
            _REPORTED_SOC_PCT,
            id="below_dynamic_floor_issue_1094",
        ),
        pytest.param(80.0, _REPORTED_FLOOR_PCT, _REPORTED_FLOOR_PCT, id="above_floor"),
        pytest.param(
            _REPORTED_SOC_PCT, None, _HARDWARE_FLOOR_PCT, id="dynamic_floor_disabled"
        ),
    ],
)
def test_published_soc_and_capacity_describe_the_same_battery(
    soc_pct: float, dynamic_floor_pct: float | None, origin_pct: float
) -> None:
    """Every slot's SoC equals the model origin plus its capacity above it.

    Before the fix the below-floor case published ``75.74 + cap`` — a SoC the
    battery never had — so the two projected trajectories disagreed with the
    live reading by the whole floor-to-SoC gap.
    """
    _output, future = _plan(soc_pct, dynamic_floor_pct)

    assert future
    for slot in future:
        expected_soc = (
            origin_pct + slot.estimated_battery_capacity_kwh / _RATED_KWH * 100
        )
        assert slot.estimated_battery_soc_pct == pytest.approx(expected_soc, abs=0.01)


def test_current_slot_starts_from_the_live_soc_not_the_floor() -> None:
    """The reporter's current slot: 11 % and 0.0 kWh, never 75.74 % and 0.0 kWh."""
    _output, future = _plan(_REPORTED_SOC_PCT, _REPORTED_FLOOR_PCT)
    current = future[0]

    assert current.estimated_battery_capacity_kwh == pytest.approx(0.0, abs=1e-6)
    assert current.estimated_battery_soc_pct == pytest.approx(_REPORTED_SOC_PCT)


def test_soc_stays_within_live_soc_and_ceiling() -> None:
    """SoC-bounds invariant: never below the live SoC, never above the ceiling.

    Being below the dynamic floor still forbids discharging below the current
    level — the model origin is the live SoC, and capacity above it is >= 0.
    """
    _output, future = _plan(_REPORTED_SOC_PCT, _REPORTED_FLOOR_PCT)

    for slot in future:
        assert slot.estimated_battery_soc_pct >= _REPORTED_SOC_PCT - 1e-6
        assert slot.estimated_battery_soc_pct <= 100.0 + 1e-6


def test_charge_headroom_is_the_real_battery_not_the_floor_gap() -> None:
    """The plan may store more than the old ``100 % − floor`` ceiling.

    The summer fixture has hours of exported PV surplus, so a model that knows
    the real 8.9 kWh of headroom stores beyond the 2.43 kWh the unreached
    75.74 % origin allowed — and never beyond the physical headroom.
    """
    _output, future = _plan(_REPORTED_SOC_PCT, _REPORTED_FLOOR_PCT)
    old_ceiling_kwh = _RATED_KWH * (100.0 - _REPORTED_FLOOR_PCT) / 100
    real_headroom_kwh = _RATED_KWH * (100.0 - _REPORTED_SOC_PCT) / 100

    peak_kwh = max(slot.estimated_battery_capacity_kwh for slot in future)

    assert peak_kwh > old_ceiling_kwh + 1e-6
    assert peak_kwh <= real_headroom_kwh + 1e-6


def test_winner_cost_is_the_published_cost() -> None:
    """Cost identity holds on the below-floor plan (no post-selection mutation)."""
    output, _future = _plan(_REPORTED_SOC_PCT, _REPORTED_FLOOR_PCT)
    winner = next(
        candidate
        for candidate in output.candidates
        if candidate.name == output.winner_name
    )

    assert winner.slots is output.slots
    assert winner._cost is not None
    assert output.plan_cost is not None
    assert winner._cost.total_cost == pytest.approx(output.plan_cost.total_cost)

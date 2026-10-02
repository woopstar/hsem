"""When the floor's reference solve may drop the battery target (issue #1207).

The dynamic floor is read from a floor-free reference solve of the same
replan.  With the house-battery target enabled (issue #1109) that solve runs
the target's second stage too.  Since #1203 the second stage may only hold
back PV the normal plan exports, which happens at or behind the first PV
surplus — and the bridge scan never reads past that slot.

So without an EV the target cannot change the floor, and the reference solve
skips it.  A sweep of 700 random days (400 hourly, 300 at 15 minutes) found no
day on which the floor, its diagnostics or its profile differed; the property
test below repeats a sample of it.

With an EV in the plan it can: the second stage pins each slot's grid import
but not how that import is split between the EV and the house battery.  One
day in 400 moved a grid charge in front of the PV surplus and released a
41 % floor.  That day is pinned here as the reason the EV case keeps the
target.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from custom_components.hsem import coordinator_builder
from custom_components.hsem.coordinator_dynamic_floor import (
    compute_dynamic_floor_from_plan,
    reference_solve_input,
)
from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils.dynamic_floor import DynamicDischargeFloor
from custom_components.hsem.utils.recommendations import Recommendations

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_TZ = timezone(timedelta(hours=3))
_DAY0 = datetime(2026, 9, 28, tzinfo=_TZ)
_LOAD = [0.5] * 6 + [0.7] * 3 + [0.5] * 8 + [0.8] * 5 + [0.7] * 2
_PV = [0.0] * 7 + [0.2, 0.8, 1.6, 2.4, 2.8, 3.0, 2.8, 2.3, 1.6, 0.8, 0.2] + [0.0] * 6
_HARDWARE_FLOOR_PCT = 5.0
_SWEPT_SEEDS = range(30)
#: The day of the sweep on which a deadline EV changes the floor.
_EV_COUNTEREXAMPLE_SEED = 86


def _random_day(
    seed: int, *, deadline_ev: bool = False
) -> tuple[datetime, PlannerInput]:
    """Return a random replan: time of day, SoC, prices, PV, load and target.

    Hourly slots over 48 h.  The night costs 0.03-0.19, mornings and evenings
    peak at 0.25-0.6, four days in ten have an export spike in the evening,
    and six in ten allow battery export.  The target is 60, 80 or 100 % by a
    random hour from 06:00 on.
    """
    rng = random.Random(seed)
    now = _DAY0 + timedelta(hours=rng.randrange(24))
    night = rng.choice((0.03, 0.10, 0.15, 0.19))
    day_price = rng.uniform(0.12, 0.25)
    peak = rng.uniform(0.25, 0.6)
    spike_hours: tuple[int, ...] = ()
    spike = 0.0
    if rng.random() < 0.4:
        start = rng.choice((17, 18, 19, 20, 21))
        spike_hours = (start, start + 1)
        spike = rng.uniform(0.3, 0.6)
    points: list[PricePoint] = []
    solcast: list[SolcastSlot] = []
    for offset in (0, 1):
        pv_scale = rng.uniform(0.0, 1.3)
        for hour in range(24):
            if hour < 6:
                price = night
            elif 6 <= hour < 9 or 17 <= hour < 22:
                price = peak
            else:
                price = day_price
            price = max(price + rng.uniform(-0.03, 0.03), 0.01)
            export = max(price - 0.10, 0.0)
            if hour in spike_hours:
                price, export = max(price, spike + 0.02), spike
            points.append(
                PricePoint(
                    hour=hour,
                    import_price=round(price, 4),
                    export_price=round(export, 4),
                    day_offset=offset,
                )
            )
            solcast.append(
                SolcastSlot(
                    hour=hour,
                    pv_estimate=round(_PV[hour] * pv_scale, 3),
                    day_offset=offset,
                )
            )
    load_scale = rng.uniform(0.6, 1.6)
    planner_input = PlannerInput(
        now_iso=now.isoformat(),
        interval_minutes=60,
        interval_length_hours=48,
        battery_soc_pct=round(rng.uniform(5.0, 100.0), 1),
        battery_rated_capacity_kwh=10.0,
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
            HourlyConsumptionAverage(
                hour=hour,
                avg_1d=value * load_scale,
                avg_3d=value * load_scale,
                avg_7d=value * load_scale,
                avg_14d=value * load_scale,
            )
            for hour, value in enumerate(_LOAD)
        ],
        price_points=points,
        solcast_slots=solcast,
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        time_discount_rate=1.0,
        excess_export_enabled=rng.random() < 0.6,
        battery_target_soc_enabled=True,
        battery_target_soc_pct=rng.choice((60.0, 80.0, 100.0)),
        battery_target_soc_time=f"{rng.randrange(6, 24):02d}:00:00",
    )
    if deadline_ev:
        # An EV that needs grid energy before tomorrow morning's PV surplus.
        planner_input = replace(
            planner_input,
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_battery_capacity_kwh=60.0,
            ev_planned_load_charger_power_kw=3.68,
            ev_planned_load_charger_efficiency_pct=92.0,
            ev_planned_load_target_soc_pct=80.0,
            ev_planned_load_current_soc_pct=round(rng.uniform(40.0, 70.0), 1),
            ev_planned_load_deadline=(now + timedelta(days=1)).replace(hour=7),
        )
    return now, planner_input


def _floor(
    planner_input: PlannerInput, now: datetime
) -> tuple[float, dict, list[tuple[str, float]], PlannerOutput]:
    """Return the floor the bridge scan reads from a solve of *planner_input*."""
    plan = run_planner(planner_input)
    with patch.object(coordinator_builder, "hsem_now", return_value=now):
        recommendations = coordinator_builder.generate_recommendation_intervals(60, 48)
    live = LiveState()
    live.huawei_batteries_rated_capacity_wh = 10_000.0
    live.huawei_batteries_end_of_discharge_soc_pct = _HARDWARE_FLOOR_PCT
    live.huawei_batteries_charging_cutoff_capacity_pct = 100.0
    live.huawei_batteries_soc_pct = planner_input.battery_soc_pct
    floor_pct, diag, profile = compute_dynamic_floor_from_plan(
        DynamicDischargeFloor(), recommendations, plan, planner_input, live, now
    )
    return floor_pct, diag, profile, plan


Solved = tuple[float, dict, list[tuple[str, float]], PlannerOutput]


@pytest.fixture(scope="module")
def swept() -> list[tuple[Solved, Solved]]:
    """Each swept day solved with the target and with the reference input."""
    days = []
    for seed in _SWEPT_SEEDS:
        now, planner_input = _random_day(seed)
        days.append(
            (
                _floor(planner_input, now),
                _floor(reference_solve_input(planner_input), now),
            )
        )
    return days


class TestReferenceSolveInput:
    def test_without_an_ev_the_target_is_dropped(self) -> None:
        _now, planner_input = _random_day(0)

        reference = reference_solve_input(planner_input)

        assert reference.battery_target_soc_enabled is False
        assert replace(reference, battery_target_soc_enabled=True) == planner_input

    @pytest.mark.parametrize(
        ("primary_ev", "second_ev"), [(True, False), (False, True)]
    )
    def test_with_an_ev_feature_enabled_the_input_is_unchanged(
        self, primary_ev: bool, second_ev: bool
    ) -> None:
        _now, planner_input = _random_day(0)
        with_ev = replace(
            planner_input,
            ev_planned_load_enabled=primary_ev,
            ev_second_planned_load_enabled=second_ev,
        )

        assert reference_solve_input(with_ev) is with_ev

    def test_without_a_target_the_input_is_unchanged(self) -> None:
        _now, planner_input = _random_day(0)
        without_target = replace(planner_input, battery_target_soc_enabled=False)

        assert reference_solve_input(without_target) is without_target


class TestFloorWithoutAnEvDoesNotDependOnTheTarget:
    """What makes dropping the target from the reference solve safe."""

    def test_the_floor_is_the_same_on_every_swept_day(
        self, swept: list[tuple[Solved, Solved]]
    ) -> None:
        for seed, (with_target, without_target) in zip(_SWEPT_SEEDS, swept):
            assert with_target[0] == pytest.approx(without_target[0]), seed

    def test_the_diagnostics_are_the_same_on_every_swept_day(
        self, swept: list[tuple[Solved, Solved]]
    ) -> None:
        for seed, (with_target, without_target) in zip(_SWEPT_SEEDS, swept):
            assert with_target[1] == without_target[1], seed

    def test_the_profile_is_the_same_on_every_swept_day(
        self, swept: list[tuple[Solved, Solved]]
    ) -> None:
        for seed, (with_target, without_target) in zip(_SWEPT_SEEDS, swept):
            assert [start for start, _ in with_target[2]] == [
                start for start, _ in without_target[2]
            ], seed
            assert [pct for _, pct in with_target[2]] == pytest.approx(
                [pct for _, pct in without_target[2]]
            ), seed

    def test_the_sample_exercises_the_second_stage_and_the_floor(
        self, swept: list[tuple[Solved, Solved]]
    ) -> None:
        """A sample where the target never acts would prove nothing."""
        reports = [with_target[3].battery_target or {} for with_target, _ in swept]

        assert sum(1 for report in reports if report.get("stage2_ran")) >= 10
        assert sum(report.get("stage2_status") == "solved" for report in reports) >= 2
        assert (
            sum(1 for with_target, _ in swept if with_target[0] > _HARDWARE_FLOOR_PCT)
            >= 2
        )


class TestWhyTheEvCaseKeepsTheTarget:
    """The counterexample: with a deadline EV the target changes the floor."""

    def test_the_second_stage_moves_a_grid_charge_in_front_of_the_pv_surplus(
        self,
    ) -> None:
        now, planner_input = _random_day(_EV_COUNTEREXAMPLE_SEED, deadline_ev=True)

        with_target, diag, _profile, plan = _floor(planner_input, now)
        without_target, diag_off, _profile_off, stage1_only = _floor(
            replace(planner_input, battery_target_soc_enabled=False), now
        )

        # Same grid import in the live slot, split differently: the second
        # stage gives the EV a little less and the house battery the rest.
        live, live_off = plan.slots[0], stage1_only.slots[0]
        assert live.grid_import_kwh == pytest.approx(live_off.grid_import_kwh)
        assert live.ev_total_planned_load_kwh < live_off.ev_total_planned_load_kwh
        assert live.recommendation == Recommendations.BatteriesChargeGrid.value
        assert live.batteries_charged_kwh > 0.1
        assert live_off.batteries_charged_kwh == pytest.approx(0.0)
        # The scan reads that grid charge as the refill and releases the floor.
        assert diag["refill_type"] == "grid_charge"
        assert with_target == pytest.approx(_HARDWARE_FLOOR_PCT)
        # Without the target the live slot is a cheap window (issue #1247),
        # so the floor is the same; before #1247 this day bridged to the
        # surplus at 41.5 %.  The split is still real, so the target stays.
        assert diag_off["refill_type"] == "cheap_window"
        assert without_target == pytest.approx(_HARDWARE_FLOOR_PCT)

    def test_so_the_reference_solve_keeps_the_target_for_it(self) -> None:
        _now, planner_input = _random_day(_EV_COUNTEREXAMPLE_SEED, deadline_ev=True)

        assert reference_solve_input(planner_input) is planner_input

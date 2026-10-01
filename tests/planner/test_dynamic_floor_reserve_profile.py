"""Tests for issue #1188 — the dynamic floor as a per-slot reserve profile.

The dynamic discharge floor is a bridge reserve: the house load from now to
the next refill, times a safety margin.  It shrinks every slot and is gone
after the refill.  The planner used to hold it constant for the whole horizon
by moving the battery model's origin up to it.  In production (#1125) the
published plan then showed ``batteries_wait_mode`` with grid import all night
while every replan lowered the floor and the battery kept discharging, and the
day after the refill was planned with a battery smaller than the real one.

The floor is now a per-slot lower bound on stored energy
(``PlannedSlot.discharge_reserve_kwh``) above a model origin that stays at the
hardware floor.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from custom_components.hsem.coordinator_dynamic_floor import (
    compute_dynamic_floor_from_plan,
    floor_required_at_slot_end,
)
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.candidate_generator import (
    CANDIDATE_MILP,
    CandidatePlan,
)
from custom_components.hsem.planner.candidate_selector import _validate_candidate
from custom_components.hsem.planner.discharge_reserve import apply_discharge_reserve
from custom_components.hsem.planner.milp._postwrite_validation import (
    validate_primary_inventory,
)
from custom_components.hsem.planner.soc_simulation import simulate_soc
from custom_components.hsem.utils.dynamic_floor import DynamicDischargeFloor
from custom_components.hsem.utils.recommendations import Recommendations
from tests.test_dynamic_floor_reference_plan import (
    _HARDWARE_FLOOR_PCT,
    _MIDNIGHT,
    _NOW,
    _hourly_recommendations,
    _live,
    _planner_input,
)

_TZ = timezone(timedelta(hours=3))
_RATED_KWH = 10.0
_USABLE_KWH = 9.5
_MARGIN = 1.15
_REFILL = _MIDNIGHT + timedelta(days=1, hours=8)
_DISCHARGE = Recommendations.BatteriesDischargeMode.value
_WAIT = Recommendations.BatteriesWaitMode.value
_CHARGE_GRID = Recommendations.BatteriesChargeGrid.value


# ---------------------------------------------------------------------------
# The profile the bridge scan returns
# ---------------------------------------------------------------------------


@dataclass
class _BridgeSlot:
    """Minimal slot for ``DynamicDischargeFloor.compute_floor_profile``."""

    start: datetime
    end: datetime
    estimated_net_consumption_kwh: float
    batteries_charged_kwh: float = 0.0
    recommendation: str | None = None
    import_price: float = float("nan")


_T0 = datetime(2026, 9, 28, 21, 0, tzinfo=_TZ)


def _bridge(
    nets: list[float], charges: dict[int, float] | None = None
) -> list[_BridgeSlot]:
    """Return hourly bridge slots from 21:00 with the given net loads."""
    charges = charges or {}
    return [
        _BridgeSlot(
            start=_T0 + timedelta(hours=i),
            end=_T0 + timedelta(hours=i + 1),
            estimated_net_consumption_kwh=net,
            batteries_charged_kwh=charges.get(i, 0.0),
            recommendation=_CHARGE_GRID if i in charges else None,
        )
        for i, net in enumerate(nets)
    ]


def _pct(reserve_kwh: float) -> float:
    """Return the floor the scalar formula gives for *reserve_kwh*."""
    return max(_HARDWARE_FLOOR_PCT, reserve_kwh / _USABLE_KWH * 100.0 * _MARGIN)


def _profile(slots: list[_BridgeSlot]) -> tuple[float, dict, list[float]]:
    floor, diag, profile = DynamicDischargeFloor().compute_floor_profile(
        _T0, slots, _USABLE_KWH, _HARDWARE_FLOOR_PCT
    )
    assert [start for start, _ in profile] == [slot.start for slot in slots]
    return floor, diag, [pct for _, pct in profile]


class TestFloorProfile:
    """``compute_floor_profile``: the floor at the start of every slot."""

    def test_first_entry_is_the_scalar_floor(self) -> None:
        slots = _bridge([0.8, 0.7, 0.5, -1.0, 0.6])

        floor, _diag, profile = _profile(slots)
        scalar, _ = DynamicDischargeFloor().compute_floor(
            _T0, slots, _USABLE_KWH, _HARDWARE_FLOOR_PCT
        )

        assert floor == pytest.approx(scalar) == pytest.approx(_pct(2.0))
        assert profile[0] == pytest.approx(scalar)

    def test_reserve_declines_by_each_slots_load(self) -> None:
        _floor, diag, profile = _profile(_bridge([0.8, 0.7, 0.5, -1.0, 0.6]))

        assert diag["refill_type"] == "solar_surplus"
        assert profile[:3] == pytest.approx([_pct(2.0), _pct(1.2), _pct(0.5)])

    def test_floor_is_the_configured_minimum_from_the_refill_on(self) -> None:
        """After the refill the reserve is not needed, even for a later night."""
        _floor, _diag, profile = _profile(_bridge([0.8, 0.7, 0.5, -1.0, 0.6]))

        assert profile[3:] == pytest.approx([_HARDWARE_FLOOR_PCT] * 2)

    def test_without_a_refill_the_reserve_runs_to_the_end(self) -> None:
        _floor, diag, profile = _profile(_bridge([0.8, 0.7, 0.5]))

        assert diag["refill_type"] == "none"
        assert profile == pytest.approx([_pct(2.0), _pct(1.2), _pct(0.5)])

    def test_covering_grid_charge_leaves_no_reserve_anywhere(self) -> None:
        slots = _bridge([0.8, 0.7, 0.0, 0.5, -1.0], charges={2: 2.0})

        floor, diag, profile = _profile(slots)

        assert diag["refill_type"] == "grid_charge"
        assert floor == pytest.approx(_HARDWARE_FLOOR_PCT)
        assert profile == pytest.approx([_HARDWARE_FLOOR_PCT] * 5)

    def test_affordable_refill_leaves_no_reserve_anywhere(self) -> None:
        slots = _bridge([0.8, 0.7, 0.5, 0.5, -1.0])
        for slot, price in zip(slots, [0.19, 0.19, 0.03, 0.03, 0.12]):
            slot.import_price = price

        floor, diag, profile = DynamicDischargeFloor().compute_floor_profile(
            _T0,
            slots,
            _USABLE_KWH,
            _HARDWARE_FLOOR_PCT,
            cycle_cost_per_kwh=0.008,
            max_grid_charge_kw=5.0,
        )

        assert diag["refill_type"] == "grid_available"
        assert floor == pytest.approx(_HARDWARE_FLOOR_PCT)
        assert [pct for _, pct in profile] == pytest.approx([_HARDWARE_FLOOR_PCT] * 5)

    def test_partial_grid_charge_ends_the_bridge(self) -> None:
        """A charge too small to cover the bridge is still where it ends (#1214)."""
        slots = _bridge([1.5, 0.0, 0.6, 0.4, -1.0], charges={1: 1.0})

        floor, diag, profile = _profile(slots)

        assert diag["refill_type"] == "grid_charge"
        assert diag["reserve_kwh"] == pytest.approx(1.5)
        assert floor == pytest.approx(_pct(1.5))
        # The 1.5 kWh before the charge is reserved; nothing behind it is.
        assert profile == pytest.approx([_pct(1.5)] + [_HARDWARE_FLOOR_PCT] * 4)

    @pytest.mark.parametrize("charged_kwh", [0.05, 0.5, 1.4])
    def test_the_size_of_a_partial_charge_does_not_change_the_floor(
        self, charged_kwh: float
    ) -> None:
        """The reserve must not depend on how much the reference plan buys.

        That amount depends on the live SoC, which the floor itself produced
        one replan earlier (issue #1214).
        """
        slots = _bridge([0.8, 0.7, 0.0, 0.6, 0.4, -1.0], charges={2: charged_kwh})

        floor, diag, profile = _profile(slots)

        assert diag["reserve_kwh"] == pytest.approx(1.5)
        assert floor == pytest.approx(_pct(1.5))
        assert profile == pytest.approx(
            [_pct(1.5), _pct(0.7)] + [_HARDWARE_FLOOR_PCT] * 4
        )

    def test_profile_never_rises(self) -> None:
        """No bridge has a credit inside it, so the profile only declines."""
        slots = _bridge([0.8, 0.7, 0.0, 0.6, 0.4, -1.0], charges={2: 0.3})

        _floor, _diag, profile = _profile(slots)

        assert all(a >= b for a, b in zip(profile, profile[1:]))

    def test_no_slots_gives_an_empty_profile(self) -> None:
        floor, _diag, profile = DynamicDischargeFloor().compute_floor_profile(
            _T0, [], _USABLE_KWH, _HARDWARE_FLOOR_PCT
        )

        assert floor == pytest.approx(_HARDWARE_FLOOR_PCT)
        assert profile == []

    def test_only_past_slots_gives_an_empty_profile(self) -> None:
        slots = _bridge([0.8, 0.7])

        _floor, _diag, profile = DynamicDischargeFloor().compute_floor_profile(
            _T0 + timedelta(hours=5), slots, _USABLE_KWH, _HARDWARE_FLOOR_PCT
        )

        assert profile == []


# ---------------------------------------------------------------------------
# From the profile to the per-slot bound
# ---------------------------------------------------------------------------


def _slots(count: int = 6) -> list[PlannedSlot]:
    return [
        PlannedSlot(start=_T0 + timedelta(hours=i), end=_T0 + timedelta(hours=i + 1))
        for i in range(count)
    ]


def _reserve_input(**overrides: object) -> PlannerInput:
    inp = PlannerInput(
        now_iso=_T0.isoformat(),
        battery_soc_pct=80.0,
        battery_rated_capacity_kwh=_RATED_KWH,
        battery_end_of_discharge_soc_pct=_HARDWARE_FLOOR_PCT,
    )
    for key, value in overrides.items():
        setattr(inp, key, value)
    return inp


def _iso_profile(floors: list[float]) -> list[tuple[str, float]]:
    return [
        ((_T0 + timedelta(hours=i)).isoformat(), pct) for i, pct in enumerate(floors)
    ]


def _reserves(
    inp: PlannerInput, *, now: datetime = _T0, stored_kwh: float = 7.5
) -> list[float]:
    slots = _slots()
    apply_discharge_reserve(slots, inp, now, stored_kwh)
    return [slot.discharge_reserve_kwh for slot in slots]


class TestApplyDischargeReserve:
    """``apply_discharge_reserve``: what each slot must still hold at its end."""

    def test_disabled_floor_reserves_nothing(self) -> None:
        assert _reserves(_reserve_input()) == pytest.approx([0.0] * 6)

    def test_slot_reserve_is_the_floor_at_the_next_slots_start(self) -> None:
        """Serving the house in a slot is what the reserve is for."""
        inp = _reserve_input(
            dynamic_discharge_floor_pct=65.0,
            dynamic_floor_profile=_iso_profile([65.0, 55.0, 45.0, 30.0, 5.0, 5.0]),
        )

        # (55-5) %, (45-5) %, (30-5) % of 10 kWh, then the refill.
        assert _reserves(inp) == pytest.approx([5.0, 4.0, 2.5, 0.0, 0.0, 0.0])

    def test_reserve_never_exceeds_the_energy_held(self) -> None:
        """A battery below the reserve holds; it is not asked to charge (#1094)."""
        inp = _reserve_input(
            battery_soc_pct=35.0,
            dynamic_discharge_floor_pct=65.0,
            dynamic_floor_profile=_iso_profile([65.0, 55.0, 45.0, 30.0, 5.0, 5.0]),
        )

        assert _reserves(inp, stored_kwh=3.0) == pytest.approx(
            [3.0, 3.0, 2.5, 0.0, 0.0, 0.0]
        )

    def test_reserve_never_rises_along_the_horizon(self) -> None:
        """A step up in a profile handed to the planner is ignored."""
        inp = _reserve_input(
            dynamic_discharge_floor_pct=40.0,
            dynamic_floor_profile=_iso_profile([40.0, 20.0, 5.0, 35.0, 25.0, 5.0]),
        )

        reserves = _reserves(inp)

        assert reserves == pytest.approx([1.5, 0.0, 0.0, 0.0, 0.0, 0.0])
        assert all(a >= b for a, b in zip(reserves, reserves[1:]))

    def test_scalar_without_a_profile_is_a_constant_reserve(self) -> None:
        inp = _reserve_input(dynamic_discharge_floor_pct=30.0)

        assert _reserves(inp) == pytest.approx([2.5] * 6)

    def test_past_slots_carry_no_reserve(self) -> None:
        inp = _reserve_input(dynamic_discharge_floor_pct=30.0)

        reserves = _reserves(inp, now=_T0 + timedelta(hours=2, minutes=10))

        assert reserves == pytest.approx([0.0, 0.0, 2.5, 2.5, 2.5, 2.5])

    def test_slot_missing_from_the_profile_has_no_reserve(self) -> None:
        inp = _reserve_input(
            dynamic_discharge_floor_pct=65.0,
            dynamic_floor_profile=_iso_profile([65.0, 55.0]),
        )

        assert _reserves(inp) == pytest.approx([5.0, 0.0, 0.0, 0.0, 0.0, 0.0])

    def test_profile_is_matched_by_utc_instant(self) -> None:
        """The profile may be in another zone than the planner's slots."""
        utc_profile = [
            ((_T0 + timedelta(hours=i)).astimezone(UTC).isoformat(), pct)
            for i, pct in enumerate([65.0, 55.0, 45.0, 30.0, 5.0, 5.0])
        ]
        inp = _reserve_input(
            dynamic_discharge_floor_pct=65.0, dynamic_floor_profile=utc_profile
        )

        assert _reserves(inp) == pytest.approx([5.0, 4.0, 2.5, 0.0, 0.0, 0.0])

    def test_floor_above_the_maximum_soc_is_clamped(self) -> None:
        inp = _reserve_input(
            battery_soc_pct=100.0,
            battery_max_soc_pct=90.0,
            dynamic_discharge_floor_pct=120.0,
        )

        # (90 - 5) % of 10 kWh, not (120 - 5) %.
        assert _reserves(inp, stored_kwh=8.5) == pytest.approx([8.5] * 6)

    def test_unusable_profile_value_counts_as_no_reserve(self) -> None:
        inp = _reserve_input(
            dynamic_discharge_floor_pct=65.0,
            dynamic_floor_profile=_iso_profile([65.0, float("nan"), 45.0]),
        )

        assert _reserves(inp) == pytest.approx([0.0] * 6)


# ---------------------------------------------------------------------------
# Every consumer reads the same bound
# ---------------------------------------------------------------------------


class TestConsumersHonourTheReserve:
    """Simulation, validation and the MILP post-write check use the slot bound."""

    @staticmethod
    def _discharge_slots(reserve_kwh: float) -> list[PlannedSlot]:
        slots = _slots(3)
        for slot in slots:
            slot.avg_house_consumption_kwh = 1.0
            slot.recommendation = _DISCHARGE
            slot.discharge_reserve_kwh = reserve_kwh
        return slots

    def test_greedy_discharge_stops_at_the_reserve(self) -> None:
        slots = self._discharge_slots(2.0)

        simulate_soc(slots, _T0, 3.5, 9.5, 9.5, 5.0, 5.0, rated_kwh=_RATED_KWH)

        assert [s.batteries_discharged_kwh for s in slots] == pytest.approx(
            [1.0, 0.5, 0.0]
        )
        assert [s.estimated_battery_capacity_kwh for s in slots] == pytest.approx(
            [2.5, 2.0, 2.0]
        )
        assert slots[1].grid_import_kwh == pytest.approx(0.5)

    def test_greedy_discharge_without_a_reserve_is_unchanged(self) -> None:
        slots = self._discharge_slots(0.0)

        simulate_soc(slots, _T0, 3.5, 9.5, 9.5, 5.0, 5.0, rated_kwh=_RATED_KWH)

        assert [s.batteries_discharged_kwh for s in slots] == pytest.approx(
            [1.0, 1.0, 1.0]
        )

    def test_validation_rejects_a_plan_below_its_slot_reserve(self) -> None:
        slots = _slots(2)
        for slot, soc in zip(slots, [40.0, 20.0]):
            slot.estimated_battery_soc_pct = soc
            slot.discharge_reserve_kwh = 2.5  # 5 % + 25 points = 30 %
        candidate = CandidatePlan(name=CANDIDATE_MILP, slots=slots)

        valid, reason = _validate_candidate(candidate, _HARDWARE_FLOOR_PCT, _RATED_KWH)

        assert not valid
        assert "30.0%" in reason
        # Without the reserve the same trajectory is fine.
        for slot in slots:
            slot.discharge_reserve_kwh = 0.0
        assert _validate_candidate(candidate, _HARDWARE_FLOOR_PCT, _RATED_KWH)[0]

    def test_postwrite_inventory_check_uses_the_slot_reserve(self) -> None:
        slots = _slots(2)
        for slot in slots:
            slot.batteries_discharged_kwh = 1.0
            slot.discharge_reserve_kwh = 2.0

        result = validate_primary_inventory(
            slots, [0, 1], current_kwh=3.5, usable_kwh=9.5
        )

        assert result["valid"] is False
        assert result["reason"] == "primary_inventory_below_floor"
        assert result["slot"] == 1


# ---------------------------------------------------------------------------
# Real planner
# ---------------------------------------------------------------------------


def _replan(
    soc_pct: float,
    *,
    interval_minutes: int = 60,
    export_spike: float | None = None,
    with_profile: bool = True,
) -> tuple[float, list[tuple[str, float]], PlannerOutput, PlannerOutput]:
    """Run one replan as the coordinator does: reference, floor, final.

    The 0.15 night is no affordable refill, so the bridge runs to tomorrow's
    first solar surplus at 08:00.
    """
    planner_input = replace(
        _planner_input(0.15, 1.0, interval_minutes),
        battery_soc_pct=soc_pct,
        excess_export_enabled=True,
    )
    if export_spike is not None:
        planner_input.price_points = [
            PricePoint(
                hour=p.hour,
                import_price=max(p.import_price, export_spike + 0.02),
                export_price=export_spike,
                day_offset=p.day_offset,
            )
            if p.day_offset == 0 and p.hour in (21, 22)
            else p
            for p in planner_input.price_points
        ]
    reference = run_planner(planner_input)
    live = _live()
    live.huawei_batteries_soc_pct = soc_pct
    floor_pct, _diag, profile = compute_dynamic_floor_from_plan(
        DynamicDischargeFloor(),
        _hourly_recommendations(1.0, interval_minutes),
        reference,
        planner_input,
        live,
        _NOW,
    )
    final = run_planner(
        replace(
            planner_input,
            dynamic_discharge_floor_pct=floor_pct,
            dynamic_floor_profile=profile if with_profile else None,
        )
    )
    return floor_pct, profile, reference, final


def _future(output: PlannerOutput) -> list[PlannedSlot]:
    return [slot for slot in output.slots if slot.end > _NOW]


def _bridge_slots(output: PlannerOutput) -> list[PlannedSlot]:
    return [slot for slot in _future(output) if slot.start < _REFILL]


def _assert_invariants(output: PlannerOutput) -> None:
    """Spec invariants on a published plan."""
    assert output.winner_name == CANDIDATE_MILP
    assert output.plan_consistency_violations == []
    winner = next(c for c in output.candidates if c.name == output.winner_name)
    assert winner.slots is output.slots
    assert winner._cost is not None
    assert output.plan_cost is not None
    assert winner._cost.total_cost == pytest.approx(output.plan_cost.total_cost)
    for slot in _future(output):
        assert slot.estimated_battery_soc_pct >= _HARDWARE_FLOOR_PCT - 0.01
        assert slot.estimated_battery_soc_pct <= 100.0 + 0.01
        assert slot.estimated_battery_capacity_kwh >= slot.discharge_reserve_kwh - 1e-3


class TestPlanFollowsTheDecliningReserve:
    """A battery above the reserve serves the house through the night."""

    @pytest.mark.parametrize("interval_minutes", [60, 15])
    def test_night_is_served_from_the_battery(self, interval_minutes: int) -> None:
        """(a) No ``batteries_wait_mode`` with grid import before the refill."""
        floor_pct, _profile, reference, final = _replan(
            95.0, interval_minutes=interval_minutes
        )

        assert floor_pct > 70.0
        bridge = _bridge_slots(final)
        assert sum(s.batteries_discharged_kwh for s in bridge) > 6.0
        assert sum(s.grid_import_kwh for s in bridge) == pytest.approx(0.0, abs=0.01)
        assert not any(s.recommendation == _WAIT for s in bridge)
        assert bridge[0].recommendation == _DISCHARGE
        # The reserve never binds here, so the plan is the floor-free one.
        assert final.plan_cost is not None
        assert reference.plan_cost is not None
        assert final.plan_cost.total_cost == pytest.approx(
            reference.plan_cost.total_cost, abs=0.01
        )
        _assert_invariants(final)

    def test_constant_floor_held_the_battery_all_night(self) -> None:
        """The pre-#1188 shape, still what a scalar-only caller gets."""
        floor_pct, _profile, _reference, constant = _replan(95.0, with_profile=False)

        bridge = _bridge_slots(constant)
        assert sum(s.grid_import_kwh for s in bridge) > 4.0
        assert sum(s.grid_import_kwh for s in bridge if s.recommendation == _WAIT) > 2.0
        assert min(s.estimated_battery_soc_pct for s in _future(constant)) >= (
            floor_pct - 0.01
        )
        _assert_invariants(constant)

    @pytest.mark.parametrize("interval_minutes", [60, 15])
    def test_battery_is_usable_down_to_the_hardware_floor_after_the_refill(
        self, interval_minutes: int
    ) -> None:
        """(b) The reserve is gone from the refill slot on."""
        floor_pct, _profile, _reference, final = _replan(
            95.0, interval_minutes=interval_minutes
        )

        after_refill = [s for s in _future(final) if s.start >= _REFILL]
        assert all(s.discharge_reserve_kwh == pytest.approx(0.0) for s in after_refill)
        assert min(s.estimated_battery_soc_pct for s in after_refill) < floor_pct - 20.0

    @pytest.mark.parametrize("interval_minutes", [60, 15])
    def test_reserve_now_is_the_scalar_floor(self, interval_minutes: int) -> None:
        """(c) The sensor's floor is the first profile entry."""
        floor_pct, profile, _reference, _final = _replan(
            95.0, interval_minutes=interval_minutes
        )

        live_start = next(s.start for s in _future(_reference))
        assert profile[0] == (live_start.isoformat(), pytest.approx(floor_pct))
        floors = [pct for _, pct in profile]
        refill_index = next(
            i for i, (start, _) in enumerate(profile) if start == _REFILL.isoformat()
        )
        bridge_floors = floors[: refill_index + 1]
        assert all(a >= b for a, b in zip(bridge_floors, bridge_floors[1:]))
        # Strictly declining while the reserve is above the configured minimum.
        assert all(
            a > b
            for a, b in zip(bridge_floors, bridge_floors[1:])
            if a > _HARDWARE_FLOOR_PCT + 1e-9
        )
        assert floors[refill_index:] == pytest.approx(
            [_HARDWARE_FLOOR_PCT] * (len(floors) - refill_index)
        )


class TestReserveBlocksExportNotTheHouse:
    """An evening export spike may not take the night's reserve."""

    def test_export_stops_at_the_reserve_and_the_house_is_still_served(self) -> None:
        _floor, _profile, reference, final = _replan(95.0, export_spike=0.45)

        # Floor-free, the 0.45 spike empties the battery in the evening.
        assert min(s.estimated_battery_soc_pct for s in _bridge_slots(reference)) < 10.0
        bridge = _bridge_slots(final)
        assert sum(s.grid_export_kwh for s in bridge) > 0.5
        assert sum(s.grid_export_kwh for s in bridge) < sum(
            s.grid_export_kwh for s in _bridge_slots(reference)
        )
        # The spike slots end exactly on their reserve: export took the rest.
        spike_end = next(s for s in bridge if s.start.hour == 22)
        assert spike_end.estimated_battery_capacity_kwh == pytest.approx(
            spike_end.discharge_reserve_kwh, abs=2e-3
        )
        # 23:00-02:00 is 1.7 kWh of house load before the plan's night charge.
        assert spike_end.discharge_reserve_kwh > 1.5
        # The hours after the spike are served from the reserve, not the grid.
        # The floor is a share of the usable capacity read as an absolute SoC,
        # so a reserve this small holds a little less than the 1.7 kWh it
        # bridges and the last 0.2 kWh is imported (issue #1221).
        after_spike = [s for s in bridge if s.start.hour in (23, 0, 1)]
        assert sum(s.batteries_discharged_kwh for s in after_spike) > 1.5
        assert sum(s.grid_import_kwh for s in after_spike) < 0.25
        _assert_invariants(final)


class TestBatteryBelowTheReserve:
    """The #1094 behaviour without moving the model origin."""

    def test_holds_until_the_reserve_has_declined_to_it(self) -> None:
        floor_pct, _profile, _reference, final = _replan(68.0)
        future = _future(final)

        assert floor_pct > 68.0
        # Live slot: the reserve is above the battery, so no discharge, the
        # real SoC is reported, and nothing is charged to "reach" the floor.
        assert future[0].batteries_discharged_kwh == pytest.approx(0.0)
        assert future[0].batteries_charged_kwh == pytest.approx(0.0)
        assert future[0].estimated_battery_soc_pct == pytest.approx(68.0)
        assert future[0].discharge_reserve_kwh == pytest.approx(6.3)
        # Later the reserve drops below the battery and the night is served.
        assert sum(s.batteries_discharged_kwh for s in _bridge_slots(final)) > 4.0
        _assert_invariants(final)

    def test_charges_into_the_full_headroom(self) -> None:
        _floor, _profile, _reference, final = _replan(68.0)

        assert max(s.estimated_battery_soc_pct for s in _future(final)) > 99.0


class TestEveryCandidateIsHeldToTheSameBound:
    """``no_action``, ``passive`` and the MILP all respect the slot reserve."""

    @pytest.mark.parametrize("soc_pct", [95.0, 68.0])
    def test_all_candidates_stay_at_or_above_the_reserve(self, soc_pct: float) -> None:
        _floor, _profile, _reference, final = _replan(soc_pct, export_spike=0.45)

        assert {c.name for c in final.candidates} >= {"no_action", "passive", "milp"}
        for candidate in final.candidates:
            assert candidate.is_valid, candidate.rejection_reason
            reserves = [s.discharge_reserve_kwh for s in candidate.slots]
            assert reserves == [s.discharge_reserve_kwh for s in final.slots]
            for slot in candidate.slots:
                if slot.end > _NOW:
                    assert (
                        slot.estimated_battery_capacity_kwh
                        >= slot.discharge_reserve_kwh - 1e-3
                    ), (candidate.name, slot.start.isoformat())


# ---------------------------------------------------------------------------
# Safety-margin learning along the profile
# ---------------------------------------------------------------------------


class TestMarginLearningAlongTheProfile:
    """The learner is judged against the floor the plan may reach in the slot."""

    _PROFILE = _iso_profile([65.0, 55.0, 45.0, 30.0, 5.0, 5.0])

    def test_floor_required_at_slot_end_is_the_next_slots_floor(self) -> None:
        now = _T0 + timedelta(minutes=20)

        assert floor_required_at_slot_end(self._PROFILE, now, 65.0) == pytest.approx(
            55.0
        )

    def test_profile_is_read_by_the_current_time_between_replans(self) -> None:
        now = _T0 + timedelta(hours=2, minutes=5)

        assert floor_required_at_slot_end(self._PROFILE, now, 65.0) == pytest.approx(
            30.0
        )

    @pytest.mark.parametrize("profile", [None, [], _iso_profile([65.0])])
    def test_without_a_later_slot_the_floor_in_force_is_used(
        self, profile: list[tuple[str, float]] | None
    ) -> None:
        now = _T0 + timedelta(minutes=20)

        assert floor_required_at_slot_end(profile, now, 65.0) == pytest.approx(65.0)

    def test_following_the_profile_is_not_a_shortfall(self) -> None:
        """Two nights of planned self-consumption must not raise the margin.

        The battery sits on the profile: at each slot start it holds that
        slot's floor and discharges to the next slot's floor.  Judged against
        the slot-start floor, every slot would be a shortfall and the margin
        would climb to its 1.50 ceiling.
        """
        floors = [65.0, 55.0, 45.0, 30.0, 5.0, 5.0]
        by_end = DynamicDischargeFloor()
        by_start = DynamicDischargeFloor()
        for day in range(3):
            base = _T0 + timedelta(days=day)
            for i, floor_now in enumerate(floors[:-1]):
                for minute, soc in ((0, floor_now), (55, floors[i + 1] + 0.5)):
                    now = base + timedelta(hours=i, minutes=minute)
                    profile = [
                        ((base + timedelta(hours=k)).isoformat(), pct)
                        for k, pct in enumerate(floors)
                    ]
                    by_end.correct_margin(
                        soc,
                        floor_required_at_slot_end(profile, now, floor_now),
                        now=now,
                    )
                    by_start.correct_margin(soc, floor_now, now=now)

        assert by_end.safety_margin == pytest.approx(_MARGIN)
        assert by_start.safety_margin > _MARGIN

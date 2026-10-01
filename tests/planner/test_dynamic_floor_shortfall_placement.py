"""A battery below the dynamic floor's reserve serves the dearest hours (issue #1222).

The dynamic discharge floor reserves the house load from now to the next
refill.  When the battery holds less than that, it cannot bridge every slot.
Until issue #1222 the planner's per-slot bound was the reserve capped at the
energy held, so the battery was held through the **first** hours of the bridge
and served the last ones, whatever their prices: in the evening the house
imported at the 0.25 peak and the battery was spent on the 0.15 night.

``reserve_bounds_kwh`` now assigns the energy held to the bridge's dearest
slots, and the shortfall falls on the cheapest ones.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.hsem import coordinator_builder
from custom_components.hsem.coordinator_dynamic_floor import (
    compute_dynamic_floor_from_plan,
)
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.discharge_reserve import reserve_bounds_kwh
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

_WAIT = Recommendations.BatteriesWaitMode.value
_DISCHARGE = Recommendations.BatteriesDischargeMode.value


# ---------------------------------------------------------------------------
# The bound itself
# ---------------------------------------------------------------------------


class TestReserveBounds:
    """``reserve_bounds_kwh``: what each slot must still hold at its end."""

    def test_a_battery_holding_the_reserve_follows_it(self) -> None:
        """Unchanged: the bound of a slot is the reserve at the next slot's start."""
        need = [3.0, 2.0, 1.0, 0.0]

        for held in (3.0, 5.0):
            assert reserve_bounds_kwh(need, [0.25, 0.15, 0.25], held) == pytest.approx(
                [2.0, 1.0, 0.0]
            )

    def test_the_shortfall_falls_on_the_cheapest_slot(self) -> None:
        """2 kWh for a 3 kWh bridge: the 0.15 slot imports, both 0.25 slots are served."""
        bounds = reserve_bounds_kwh([3.0, 2.0, 1.0, 0.0], [0.25, 0.15, 0.25], 2.0)

        # Slot 0 may take 1 kWh, slot 1 nothing, slot 2 the last kWh.
        assert bounds == pytest.approx([1.0, 1.0, 0.0])

    def test_before_the_battery_was_held_through_the_first_dear_slot(self) -> None:
        """The bound used to be ``min(held, reserve)``: [2, 1, 0], slot 0 held."""
        bounds = reserve_bounds_kwh([3.0, 2.0, 1.0, 0.0], [0.25, 0.15, 0.25], 2.0)

        assert bounds[0] < 2.0

    def test_equal_prices_keep_the_time_order(self) -> None:
        """Flat prices: the last slots are served, as before issue #1222."""
        need = [3.0, 2.0, 1.0, 0.0]

        for held in (0.5, 1.0, 2.0, 2.5):
            assert reserve_bounds_kwh(need, [0.2, 0.2, 0.2], held) == pytest.approx(
                [min(held, level) for level in need[1:]]
            )

    def test_a_partly_covered_slot_gets_what_is_left(self) -> None:
        """1.5 kWh: the dearest slot whole, half of the next dearest."""
        bounds = reserve_bounds_kwh([3.0, 2.0, 1.0, 0.0], [0.30, 0.10, 0.20], 1.5)

        # Slot 0 (0.30) takes 1.0, slot 2 (0.20) takes 0.5, slot 1 nothing.
        assert bounds == pytest.approx([0.5, 0.5, 0.0])

    def test_the_reserve_beyond_the_horizon_is_assigned_first(self) -> None:
        """A constant floor (no profile) is never released inside the horizon."""
        assert reserve_bounds_kwh(
            [5.0, 5.0, 5.0, 5.0], [0.3, 0.1, 0.2], 3.0
        ) == pytest.approx([3.0, 3.0, 3.0])
        assert reserve_bounds_kwh(
            [5.0, 4.0, 3.0, 3.0], [0.3, 0.1, 0.2], 3.5
        ) == pytest.approx([3.0, 3.0, 3.0])

    def test_a_slot_without_a_price_counts_as_the_cheapest(self) -> None:
        bounds = reserve_bounds_kwh(
            [3.0, 2.0, 1.0, 0.0], [float("nan"), 0.15, 0.25], 2.0
        )

        assert bounds == pytest.approx([2.0, 1.0, 0.0])

    def test_an_empty_battery_reserves_nothing(self) -> None:
        for held in (0.0, -0.4):
            assert reserve_bounds_kwh(
                [3.0, 2.0, 1.0, 0.0], [0.25, 0.15, 0.25], held
            ) == pytest.approx([0.0, 0.0, 0.0])

    def test_no_slots(self) -> None:
        assert reserve_bounds_kwh([2.0], [], 1.0) == []

    def test_bounds_never_exceed_the_energy_held_and_never_rise(self) -> None:
        """So no plan has to charge to satisfy them (the #1094 and #1188 rules)."""
        rng = random.Random(1222)
        for _ in range(300):
            count = rng.randint(1, 12)
            takes = [rng.choice([0.0, rng.uniform(0.0, 1.5)]) for _ in range(count)]
            tail = rng.choice([0.0, rng.uniform(0.0, 2.0)])
            need = [tail + sum(takes[i:]) for i in range(count)] + [tail]
            prices = [rng.choice([0.12, 0.15, 0.19, 0.25]) for _ in range(count)]
            held = rng.uniform(0.0, need[0] * 1.3 + 0.1)

            bounds = reserve_bounds_kwh(need, prices, held)

            assert all(bound <= held + 1e-9 for bound in bounds)
            assert all(a >= b - 1e-9 for a, b in zip(bounds, bounds[1:]))
            # Never more than the reserve itself asks for.
            assert all(b <= level + 1e-9 for b, level in zip(bounds, need[1:]))
            # What a slot may take is never more than its share of the reserve.
            starts = [min(held, need[0]), *bounds[:-1]]
            for start, bound, take in zip(starts, bounds, takes):
                assert start - bound <= take + 1e-9
            if held >= need[0]:
                assert bounds == pytest.approx(need[1:])


# ---------------------------------------------------------------------------
# Through the real planner
# ---------------------------------------------------------------------------

pytestmark_planner = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)


def _prices(day: int) -> list[float]:
    """Return the import price per hour of *day* (0 = the fixture's day).

    The #1125 house without an export spike: 0.19-0.21 on the first evening,
    a 0.15 night at 02:00-06:00 (and from 21:00 on later days), 0.25 morning
    and evening peaks, a 0.12 day.
    """
    if day == 0:
        return [0.12] * 6 + [0.22] * 3 + [0.12] * 8 + [0.21] * 4 + [0.19] * 3
    return [0.19] * 2 + [0.15] * 4 + [0.25] * 3 + [0.12] * 7 + [0.25] * 5 + [0.15] * 3


def _replan(now: datetime, soc_pct: float) -> tuple[float, dict, PlannerOutput]:
    """Run one replan as the coordinator does: reference, floor, final."""
    day = (now.date() - _MIDNIGHT.date()).days
    planner_input = replace(
        _planner_input(0.15),
        now_iso=now.isoformat(),
        battery_soc_pct=soc_pct,
        excess_export_enabled=True,
        price_points=[
            PricePoint(
                hour=hour,
                import_price=price,
                export_price=max(price - 0.10, 0.0),
                day_offset=offset,
            )
            for offset in (0, 1)
            for hour, price in enumerate(_prices(day + offset))
        ],
        solcast_slots=[
            SolcastSlot(hour=hour, pv_estimate=pv, day_offset=offset)
            for offset in (0, 1)
            for hour, pv in enumerate(_PV_TOMORROW)
        ],
    )
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
    final = run_planner(
        replace(
            planner_input,
            dynamic_discharge_floor_pct=floor_pct,
            dynamic_floor_profile=profile,
        )
    )
    return floor_pct, diag, final


def _bridge(output: PlannerOutput, now: datetime, diag: dict) -> list[PlannedSlot]:
    """Return the plan's slots from the live one up to the refill slot."""
    refill = datetime.fromisoformat(diag["next_refill_slot"])
    return [slot for slot in output.slots if slot.end > now and slot.start < refill]


def _assert_shortfall_is_on_the_cheapest_slots(bridge: list[PlannedSlot]) -> None:
    """No bridge slot imports while the battery later serves a cheaper one."""
    imported = [s.price.import_price for s in bridge if s.grid_import_kwh > 0.05]
    served = [s.price.import_price for s in bridge if s.batteries_discharged_kwh > 0.05]
    assert imported, "the battery is below its reserve, so some slot imports"
    assert served
    for slot in bridge:
        if slot.grid_import_kwh > 0.05:
            later = [
                s.price.import_price
                for s in bridge
                if s.start > slot.start and s.batteries_discharged_kwh > 0.05
            ]
            assert all(price >= slot.price.import_price - 1e-9 for price in later)
    assert max(imported) <= min(served) + 1e-9


@pytestmark_planner
class TestFullBatteryUnderALongBridge:
    """17:00 on day 1: a 15-hour bridge asks for more than the battery holds."""

    _NOW = _MIDNIGHT + timedelta(days=1, hours=17)

    def test_a_full_battery_is_not_held_at_the_evening_peak(self) -> None:
        """Before: ``batteries_wait_mode`` and 0.6 kWh imported at 0.25."""
        floor_pct, diag, final = _replan(self._NOW, 100.0)
        live_slot = next(s for s in final.slots if s.start <= self._NOW < s.end)

        assert diag["refill_type"] == "solar_surplus"
        # The reserve is more than the battery holds; the floor says "all of it".
        assert diag["reserve_kwh"] * 1.15 > 9.5
        assert floor_pct == pytest.approx(100.0)
        assert live_slot.price.import_price == pytest.approx(0.25)
        assert live_slot.recommendation == _DISCHARGE
        assert live_slot.batteries_discharged_kwh > 0.5
        assert live_slot.grid_import_kwh == pytest.approx(0.0, abs=0.01)

    def test_the_house_imports_at_the_cheapest_bridge_prices(self) -> None:
        _floor, diag, final = _replan(self._NOW, 100.0)

        _assert_shortfall_is_on_the_cheapest_slots(_bridge(final, self._NOW, diag))

    def test_the_reserve_is_still_a_bound_nothing_is_charged_for_it(self) -> None:
        _floor, _diag, final = _replan(self._NOW, 100.0)
        future = [slot for slot in final.slots if slot.end > self._NOW]

        assert final.plan_consistency_violations == []
        assert future[0].discharge_reserve_kwh <= 9.5 + 1e-9
        reserves = [slot.discharge_reserve_kwh for slot in future]
        assert all(a >= b - 1e-9 for a, b in zip(reserves, reserves[1:]))
        for slot in future:
            assert slot.estimated_battery_capacity_kwh >= (
                slot.discharge_reserve_kwh - 1e-3
            )


@pytestmark_planner
class TestBatteryBelowTheReserveInTheEvening:
    """The #1125 evening: 68 % at 21:00 under a 78.8 % floor, 0.19 now, 0.15 night."""

    _NOW = _MIDNIGHT + timedelta(hours=21)

    def test_the_evening_is_served_and_the_night_imports(self) -> None:
        """Before: held at 21:00 (import at 0.19), then spent on the 0.15 night."""
        floor_pct, diag, final = _replan(self._NOW, 68.0)
        live_slot = next(s for s in final.slots if s.start <= self._NOW < s.end)

        assert floor_pct > 68.0
        assert live_slot.recommendation == _DISCHARGE
        assert live_slot.grid_import_kwh == pytest.approx(0.0, abs=0.01)
        _assert_shortfall_is_on_the_cheapest_slots(_bridge(final, self._NOW, diag))

    def test_the_morning_peak_and_the_evening_are_served_before_the_night(
        self,
    ) -> None:
        """55 %: 5 kWh for a 7.4 kWh reserve; the four 0.15 hours go short."""
        floor_pct, diag, final = _replan(self._NOW, 55.0)
        bridge = _bridge(final, self._NOW, diag)

        assert floor_pct > 55.0
        for slot in bridge:
            if slot.price.import_price > 0.18:
                assert slot.grid_import_kwh == pytest.approx(0.0, abs=0.01)
                assert slot.batteries_discharged_kwh > 0.4
        night = [s for s in bridge if s.price.import_price < 0.16]
        assert len(night) == 4
        assert sum(s.grid_import_kwh for s in night) > 1.5
        # The battery waits in the night with what the 0.25 morning needs:
        # (0.70 + 0.52) kWh of net load × the 1.15 margin.
        assert sum(s.recommendation == _WAIT for s in night) >= 3
        assert night[-1].discharge_reserve_kwh == pytest.approx(1.403, abs=0.01)

    def test_a_flat_priced_bridge_keeps_the_time_order(self) -> None:
        """25 %: the plan refills at 02:00 and every slot before it costs 0.19.

        With nothing to choose between, the first slot goes short, as before.
        """
        floor_pct, diag, final = _replan(self._NOW, 25.0)
        bridge = _bridge(final, self._NOW, diag)

        assert diag["refill_type"] == "grid_charge"
        assert floor_pct > 25.0
        assert {round(s.price.import_price, 6) for s in bridge} == {0.19}
        assert bridge[0].recommendation == _WAIT
        assert bridge[0].batteries_discharged_kwh == pytest.approx(0.0, abs=1e-6)
        assert bridge[0].discharge_reserve_kwh == pytest.approx(2.0, abs=1e-6)
        assert sum(s.batteries_discharged_kwh for s in bridge[2:]) > 1.5


@pytestmark_planner
class TestReserveStillBlocksExport:
    """The reserve is a lower bound: the battery is not sold below it."""

    def test_a_battery_below_the_reserve_is_not_exported(self) -> None:
        now = _MIDNIGHT + timedelta(days=1, hours=17)
        _floor, diag, final = _replan(now, 100.0)

        bridge = _bridge(final, now, diag)
        assert sum(s.grid_export_kwh for s in bridge) == pytest.approx(0.0, abs=0.05)
        assert not any(
            s.recommendation == Recommendations.ForceBatteriesDischarge.value
            for s in bridge
        )
        assert min(s.estimated_battery_soc_pct for s in bridge) > _HARDWARE_FLOOR_PCT

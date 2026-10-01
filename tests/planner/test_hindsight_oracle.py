"""Tests for the hindsight oracle and its baseline (issues #1208, #1182).

The oracle is quoted as a lower bound on what a day cost, so the property that
matters most is tested first and at length: no feasible way of running the
battery that ends with at least the same stored energy is cheaper.
"""

from __future__ import annotations

import random
from unittest.mock import patch

import pytest

from custom_components.hsem.planner import hindsight_oracle
from custom_components.hsem.planner.hindsight_oracle import (
    HindsightBattery,
    HindsightRun,
    HindsightSlot,
    grid_cost,
    simulate_self_consumption,
    solve_hindsight_oracle,
    stored_trajectory,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available

needs_scipy = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_BATTERY = HindsightBattery(
    min_stored_kwh=0.5,
    max_stored_kwh=10.0,
    max_charge_kw=5.0,
    max_discharge_kw=5.0,
    charge_efficiency=0.97,
    discharge_efficiency=0.97,
)
_LOSSLESS = HindsightBattery(
    min_stored_kwh=0.0, max_stored_kwh=10.0, max_charge_kw=5.0, max_discharge_kw=5.0
)


def _slot(
    net: float, imp: float, exp: float | None = None, *, curtailable: float = 0.0
) -> HindsightSlot:
    """Return one hourly slot; export defaults to 0.10 below import."""
    return HindsightSlot(
        hours=1.0,
        net_load_kwh=net,
        import_price=imp,
        export_price=imp - 0.10 if exp is None else exp,
        curtailable_kwh=curtailable,
    )


def _random_day(rng: random.Random, count: int = 96) -> list[HindsightSlot]:
    """Return a day of quarter-hours with PV, load and a price curve."""
    slots = []
    for index in range(count):
        hour = index * 24 / count
        pv = max(0.0, 1.2 - abs(hour - 13.0) * 0.25) * rng.uniform(0.2, 1.0)
        load = rng.uniform(0.05, 0.5)
        price = 1.0 + 0.8 * (17 <= hour < 21) - 0.4 * (2 <= hour < 6)
        price += rng.uniform(-0.1, 0.1)
        slots.append(
            HindsightSlot(
                hours=24 / count,
                net_load_kwh=load - pv,
                import_price=price,
                export_price=price - rng.uniform(0.2, 0.6),
                curtailable_kwh=pv,
            )
        )
    return slots


def _random_run(
    rng: random.Random,
    slots: list[HindsightSlot],
    battery: HindsightBattery,
    start_kwh: float,
) -> HindsightRun:
    """Return a feasible run that charges or discharges at random each slot."""
    stored = start_kwh
    charged: list[float] = []
    discharged: list[float] = []
    for slot in slots:
        charge = discharge = 0.0
        action = rng.random()
        if action < 0.35:
            charge = rng.uniform(0.0, 1.0) * min(
                battery.max_charge_kw * slot.hours,
                (battery.max_stored_kwh - stored) / battery.charge_efficiency,
            )
        elif action < 0.7:
            discharge = rng.uniform(0.0, 1.0) * min(
                battery.max_discharge_kw * slot.hours,
                (stored - battery.min_stored_kwh) * battery.discharge_efficiency,
            )
        stored += (
            charge * battery.charge_efficiency
            - discharge / battery.discharge_efficiency
        )
        charged.append(charge)
        discharged.append(discharge)
    nets = [slot.net_load_kwh + c - d for slot, c, d in zip(slots, charged, discharged)]
    grid_import = [max(net, 0.0) for net in nets]
    grid_export = [max(-net, 0.0) for net in nets]
    return HindsightRun(
        cost=grid_cost(slots, grid_import, grid_export),
        grid_import_kwh=tuple(grid_import),
        grid_export_kwh=tuple(grid_export),
        charged_kwh=tuple(charged),
        discharged_kwh=tuple(discharged),
        stored_kwh=tuple(stored_trajectory(battery, start_kwh, charged, discharged)),
    )


class TestGridCost:
    def test_import_is_paid_and_export_is_earned(self) -> None:
        slots = [_slot(0.0, 2.0, 0.5), _slot(0.0, 1.0, 0.25)]

        assert grid_cost(slots, [3.0, 0.0], [0.0, 4.0]) == pytest.approx(
            3.0 * 2.0 - 4.0 * 0.25
        )

    def test_a_negative_export_price_is_a_cost(self) -> None:
        assert grid_cost([_slot(0.0, 1.0, -0.2)], [0.0], [5.0]) == pytest.approx(1.0)

    def test_mismatched_lengths_are_refused(self) -> None:
        with pytest.raises(ValueError):
            grid_cost([_slot(0.0, 1.0)], [1.0, 2.0], [0.0])


class TestStoredTrajectory:
    def test_charging_loses_on_the_way_in_and_discharging_on_the_way_out(self) -> None:
        trajectory = stored_trajectory(_BATTERY, 5.0, [1.0, 0.0], [0.0, 1.0])

        assert trajectory == pytest.approx([5.0 + 0.97, 5.0 + 0.97 - 1.0 / 0.97])

    def test_it_is_not_clamped_to_the_capacity(self) -> None:
        """A measured day under an assumed efficiency may leave the limits."""
        trajectory = stored_trajectory(_BATTERY, 9.9, [2.0], [0.0])

        assert trajectory[0] > _BATTERY.max_stored_kwh


class TestSelfConsumption:
    def test_surplus_charges_and_deficit_discharges(self) -> None:
        slots = [_slot(-2.0, 1.0), _slot(1.5, 2.0)]

        run = simulate_self_consumption(slots, _LOSSLESS, 3.0)

        assert run.charged_kwh == pytest.approx((2.0, 0.0))
        assert run.discharged_kwh == pytest.approx((0.0, 1.5))
        assert run.grid_import_kwh == pytest.approx((0.0, 0.0))
        assert run.grid_export_kwh == pytest.approx((0.0, 0.0))
        assert run.stored_kwh == pytest.approx((5.0, 3.5))
        assert run.cost == pytest.approx(0.0)

    def test_a_full_battery_exports_and_an_empty_one_imports(self) -> None:
        slots = [_slot(-2.0, 1.0, 0.4), _slot(3.0, 2.0)]

        full = simulate_self_consumption(slots[:1], _LOSSLESS, 10.0)
        empty = simulate_self_consumption(slots[1:], _LOSSLESS, 0.0)

        assert full.grid_export_kwh == pytest.approx((2.0,))
        assert full.cost == pytest.approx(-0.8)
        assert empty.grid_import_kwh == pytest.approx((3.0,))
        assert empty.cost == pytest.approx(6.0)

    def test_power_limits_cap_both_directions(self) -> None:
        slots = [
            HindsightSlot(0.25, -3.0, 1.0, 0.5),
            HindsightSlot(0.25, 3.0, 1.0, 0.5),
        ]

        run = simulate_self_consumption(slots, _LOSSLESS, 5.0)

        # 5 kW for a quarter of an hour is 1.25 kWh either way.
        assert run.charged_kwh[0] == pytest.approx(1.25)
        assert run.grid_export_kwh[0] == pytest.approx(1.75)
        assert run.discharged_kwh[1] == pytest.approx(1.25)
        assert run.grid_import_kwh[1] == pytest.approx(1.75)

    def test_efficiency_is_paid_on_both_sides(self) -> None:
        run = simulate_self_consumption(
            [_slot(-1.0, 1.0), _slot(0.5, 1.0)], _BATTERY, 5.0
        )

        assert run.stored_kwh[0] == pytest.approx(5.0 + 0.97)
        assert run.stored_kwh[1] == pytest.approx(5.0 + 0.97 - 0.5 / 0.97)

    def test_discharge_stops_at_the_hardware_floor(self) -> None:
        run = simulate_self_consumption([_slot(4.0, 1.0)], _BATTERY, 1.0)

        assert run.stored_kwh[0] == pytest.approx(_BATTERY.min_stored_kwh)
        assert run.discharged_kwh[0] == pytest.approx(0.5 * 0.97)

    def test_a_start_outside_the_capacity_is_clamped(self) -> None:
        run = simulate_self_consumption([_slot(0.0, 1.0)], _BATTERY, 99.0)

        assert run.stored_kwh == pytest.approx((_BATTERY.max_stored_kwh,))

    def test_an_empty_window_costs_nothing(self) -> None:
        run = simulate_self_consumption([], _BATTERY, 5.0)

        assert run.cost == pytest.approx(0.0)
        assert run.end_stored_kwh is None


@needs_scipy
class TestOracleIsALowerBound:
    """No feasible run that ends at least as full is cheaper than the oracle."""

    @pytest.mark.parametrize("seed", range(5))
    def test_no_random_feasible_run_beats_it(self, seed: int) -> None:
        rng = random.Random(seed)
        slots = _random_day(rng)
        start_kwh = rng.uniform(_BATTERY.min_stored_kwh, _BATTERY.max_stored_kwh)

        for _ in range(10):
            run = _random_run(rng, slots, _BATTERY, start_kwh)
            oracle = solve_hindsight_oracle(
                slots, _BATTERY, start_kwh, run.stored_kwh[-1]
            )

            assert oracle is not None
            assert oracle.cost <= run.cost + 1e-6

    @pytest.mark.parametrize("seed", range(8))
    def test_it_never_loses_to_self_consumption(self, seed: int) -> None:
        rng = random.Random(100 + seed)
        slots = _random_day(rng)
        start_kwh = rng.uniform(_BATTERY.min_stored_kwh, _BATTERY.max_stored_kwh)
        baseline = simulate_self_consumption(slots, _BATTERY, start_kwh)

        oracle = solve_hindsight_oracle(
            slots, _BATTERY, start_kwh, baseline.stored_kwh[-1]
        )

        assert oracle is not None
        assert oracle.cost <= baseline.cost + 1e-6

    @pytest.mark.parametrize("seed", range(4))
    def test_a_higher_end_energy_never_makes_it_cheaper(self, seed: int) -> None:
        rng = random.Random(200 + seed)
        slots = _random_day(rng)

        costs = [
            oracle.cost
            for end_kwh in (0.5, 3.0, 6.0, 9.0)
            if (oracle := solve_hindsight_oracle(slots, _BATTERY, 5.0, end_kwh))
        ]

        assert len(costs) == 4
        assert all(a <= b + 1e-6 for a, b in zip(costs, costs[1:]))


@needs_scipy
class TestOracleRespectsTheHardLimits:
    @pytest.fixture
    def oracle(self) -> HindsightRun:
        rng = random.Random(7)
        run = solve_hindsight_oracle(_random_day(rng), _BATTERY, 4.0, 6.0)
        assert run is not None
        return run

    def test_stored_energy_stays_within_the_capacity(
        self, oracle: HindsightRun
    ) -> None:
        assert min(oracle.stored_kwh) >= _BATTERY.min_stored_kwh - 1e-6
        assert max(oracle.stored_kwh) <= _BATTERY.max_stored_kwh + 1e-6

    def test_it_ends_with_at_least_the_required_energy(
        self, oracle: HindsightRun
    ) -> None:
        assert oracle.end_stored_kwh is not None
        assert oracle.end_stored_kwh >= 6.0 - 1e-6

    def test_power_stays_within_the_limits(self, oracle: HindsightRun) -> None:
        assert max(oracle.charged_kwh) <= 5.0 * 0.25 + 1e-6
        assert max(oracle.discharged_kwh) <= 5.0 * 0.25 + 1e-6

    def test_no_slot_charges_and_discharges(self, oracle: HindsightRun) -> None:
        assert not any(
            c > 1e-6 and d > 1e-6
            for c, d in zip(oracle.charged_kwh, oracle.discharged_kwh)
        )

    def test_no_slot_imports_and_exports(self, oracle: HindsightRun) -> None:
        assert not any(
            i > 1e-6 and e > 1e-6
            for i, e in zip(oracle.grid_import_kwh, oracle.grid_export_kwh)
        )

    def test_the_stored_trajectory_is_the_one_its_flows_imply(
        self, oracle: HindsightRun
    ) -> None:
        assert oracle.stored_kwh == pytest.approx(
            stored_trajectory(_BATTERY, 4.0, oracle.charged_kwh, oracle.discharged_kwh)
        )

    def test_the_cost_is_the_cost_of_its_grid_flows(self) -> None:
        slots = _random_day(random.Random(7))
        run = solve_hindsight_oracle(slots, _BATTERY, 4.0, 6.0)

        assert run is not None
        assert run.cost == pytest.approx(
            grid_cost(slots, run.grid_import_kwh, run.grid_export_kwh)
        )


@needs_scipy
class TestOracleDecisions:
    def test_it_buys_cheap_and_serves_the_expensive_hour(self) -> None:
        slots = [_slot(0.0, 0.5, 0.0), _slot(2.0, 3.0, 0.0)]

        run = solve_hindsight_oracle(slots, _LOSSLESS, 0.0, 0.0)

        assert run is not None
        assert run.charged_kwh[0] == pytest.approx(2.0)
        assert run.discharged_kwh[1] == pytest.approx(2.0)
        assert run.cost == pytest.approx(1.0)

    def test_it_does_not_drain_the_battery_below_the_required_end(self) -> None:
        """Without the end constraint it would sell the 5 kWh it started with."""
        slots = [_slot(0.0, 2.0, 1.5)]

        kept = solve_hindsight_oracle(slots, _LOSSLESS, 5.0, 5.0)
        sold = solve_hindsight_oracle(slots, _LOSSLESS, 5.0, 0.0)

        assert kept is not None and sold is not None
        assert kept.cost == pytest.approx(0.0)
        assert sold.cost == pytest.approx(-7.5)

    def test_a_spread_below_the_round_trip_loss_is_not_traded(self) -> None:
        slots = [_slot(0.0, 1.00), _slot(1.0, 1.02)]

        run = solve_hindsight_oracle(slots, _BATTERY, 0.5, 0.5)

        # 1.00 / (0.97 × 0.97) = 1.063 per delivered kWh, dearer than 1.02.
        assert run is not None
        assert run.charged_kwh == pytest.approx((0.0, 0.0), abs=1e-6)
        assert run.cost == pytest.approx(1.02)

    def test_a_negative_export_price_is_avoided_by_curtailing(self) -> None:
        slots = [_slot(-2.0, 0.5, -0.3, curtailable=2.0)]

        run = solve_hindsight_oracle(slots, _LOSSLESS, 10.0, 10.0)

        assert run is not None
        assert run.grid_export_kwh == pytest.approx((0.0,), abs=1e-6)
        assert run.cost == pytest.approx(0.0, abs=1e-6)

    def test_without_curtailable_pv_the_surplus_must_be_exported(self) -> None:
        slots = [_slot(-2.0, 0.5, -0.3)]

        run = solve_hindsight_oracle(slots, _LOSSLESS, 10.0, 10.0)

        assert run is not None
        assert run.grid_export_kwh == pytest.approx((2.0,))
        assert run.cost == pytest.approx(0.6)

    def test_a_negative_import_price_is_not_burned_through_the_battery(self) -> None:
        """Charging and discharging at once would turn losses into income."""
        slots = [_slot(0.0, -1.0, -1.5)]

        run = solve_hindsight_oracle(slots, _BATTERY, 5.0, 5.0)

        assert run is not None
        assert not (run.charged_kwh[0] > 1e-6 and run.discharged_kwh[0] > 1e-6)
        # At most one slot of charging can be bought, and it must be kept.
        assert run.cost >= -5.0 - 1e-6

    def test_a_grid_import_limit_caps_the_cheap_slot(self) -> None:
        slots = [_slot(0.0, 0.5, 0.0), _slot(4.0, 3.0, 0.0)]

        free = solve_hindsight_oracle(slots, _LOSSLESS, 0.0, 0.0)
        capped = solve_hindsight_oracle(
            slots, _LOSSLESS, 0.0, 0.0, max_grid_import_kwh=[1.0, 10.0]
        )

        assert free is not None and capped is not None
        assert free.grid_import_kwh[0] == pytest.approx(4.0)
        assert capped.grid_import_kwh[0] == pytest.approx(1.0)
        assert capped.cost > free.cost

    def test_a_grid_export_limit_caps_the_sale(self) -> None:
        slots = [_slot(0.0, 3.0, 2.5)]

        capped = solve_hindsight_oracle(
            slots, _LOSSLESS, 5.0, 0.0, max_grid_export_kwh=[1.0]
        )

        assert capped is not None
        assert capped.grid_export_kwh == pytest.approx((1.0,))

    def test_per_slot_battery_limits_replace_the_power_limit(self) -> None:
        slots = [_slot(0.0, 0.5, 0.0), _slot(4.0, 3.0, 0.0)]

        run = solve_hindsight_oracle(
            slots,
            _LOSSLESS,
            0.0,
            0.0,
            max_charge_kwh=[1.5, 0.0],
            max_discharge_kwh=[0.0, 1.5],
        )

        assert run is not None
        assert run.charged_kwh[0] == pytest.approx(1.5)
        assert run.discharged_kwh[1] == pytest.approx(1.5)


class TestOracleWithoutAnAnswer:
    def test_an_empty_window_has_no_oracle(self) -> None:
        assert solve_hindsight_oracle([], _BATTERY, 5.0, 5.0) is None

    @needs_scipy
    def test_an_unreachable_end_energy_has_no_oracle(self) -> None:
        """One hour at 5 kW cannot lift the battery from 1 to 9 kWh."""
        assert solve_hindsight_oracle([_slot(0.0, 1.0)], _BATTERY, 1.0, 9.0) is None

    def test_without_scipy_there_is_no_oracle(self) -> None:
        with patch.object(hindsight_oracle, "is_scipy_available", return_value=False):
            assert solve_hindsight_oracle([_slot(0.0, 1.0)], _BATTERY, 5.0, 5.0) is None

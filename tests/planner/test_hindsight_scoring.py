"""Tests for the shared hindsight scoring (issues #1208 and #1182).

``planner/hindsight_scoring.py`` holds the definitions the offline backtest
and the on-device performance sensors both use.  These tests pin what each
number means on windows small enough to check by hand, and that the oracle
only ever beats the realized run inside the user's own limits.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.hindsight_oracle import simulate_self_consumption
from custom_components.hsem.planner.hindsight_scoring import (
    MIN_POTENTIAL,
    RealizedSlot,
    SiteLimits,
    WindowScore,
    battery_flows_from_soc,
    capture_share,
    realized_window,
    score_window,
    utilisation_share,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_SITE = SiteLimits(
    rated_kwh=10.0,
    min_soc_pct=5.0,
    max_soc_pct=100.0,
    max_charge_kw=5.0,
    max_discharge_kw=5.0,
    charge_efficiency=0.97,
    discharge_efficiency=0.97,
)
#: Cheap night, dear evening: a day on which control has something to win.
_PRICES = [0.5] * 6 + [1.2] * 11 + [2.5] * 4 + [1.2] * 3
_LOAD = [0.6] * 17 + [2.1] * 4 + [0.6] * 3


def _idle_slots(
    prices: list[float] = _PRICES,
    load: list[float] = _LOAD,
    *,
    export_discount: float = 0.3,
) -> list[RealizedSlot]:
    """Return hourly slots in which the battery did nothing."""
    return [
        RealizedSlot(
            hours=1.0,
            grid_import_kwh=max(net, 0.0),
            grid_export_kwh=max(-net, 0.0),
            charged_kwh=0.0,
            discharged_kwh=0.0,
            import_price=price,
            export_price=price - export_discount,
            pv_kwh=0.0,
        )
        for price, net in zip(prices, load, strict=True)
    ]


def _run_as(slots: list[RealizedSlot], run: object) -> list[RealizedSlot]:
    """Return *slots* with the grid and battery flows of a hindsight run."""
    return [
        replace(
            slot,
            grid_import_kwh=run.grid_import_kwh[i],  # type: ignore[attr-defined]
            grid_export_kwh=run.grid_export_kwh[i],  # type: ignore[attr-defined]
            charged_kwh=run.charged_kwh[i],  # type: ignore[attr-defined]
            discharged_kwh=run.discharged_kwh[i],  # type: ignore[attr-defined]
        )
        for i, slot in enumerate(slots)
    ]


class TestWhatTheNumbersMean:
    def test_the_worked_example(self) -> None:
        """Baseline 5.00, realized 3.40, oracle 3.00, ends equal (issue #1182)."""
        score = WindowScore(
            realized_cost=3.40,
            oracle_cost=3.00,
            baseline_cost=5.00,
            potential=2.00,
            realized_end_kwh=4.0,
            baseline_end_kwh=4.0,
        )

        assert score.regret == pytest.approx(0.40)
        assert score.savings == pytest.approx(1.60)
        assert score.capture == pytest.approx(0.80)

    def test_savings_and_regret_add_up_to_the_potential(self) -> None:
        window = realized_window(_idle_slots(), 4.0, _SITE)

        score = score_window(window)

        assert score is not None
        assert score.savings + score.regret == pytest.approx(score.potential)
        assert score.regret >= -1e-9

    def test_capture_is_unknown_below_the_threshold(self) -> None:
        assert capture_share(0.01, MIN_POTENTIAL - 0.001) is None
        assert capture_share(0.04, MIN_POTENTIAL) == pytest.approx(0.8)

    def test_a_negative_capture_is_not_clamped(self) -> None:
        """A run worse than self-consumption says so."""
        assert capture_share(-1.5, 2.0) == pytest.approx(-0.75)

    def test_a_capture_above_one_is_not_clamped_either(self) -> None:
        assert capture_share(2.2, 2.0) == pytest.approx(1.1)

    def test_a_window_run_like_the_oracle_has_no_regret(self) -> None:
        idle = realized_window(_idle_slots(), 4.0, _SITE)
        best = idle.oracle(4.0)
        assert best is not None

        score = score_window(realized_window(_run_as(_idle_slots(), best), 4.0, _SITE))

        assert score is not None
        assert score.regret == pytest.approx(0.0, abs=1e-6)
        assert score.realized_end_kwh == pytest.approx(4.0, abs=1e-6)

    def test_a_window_of_plain_self_consumption_captures_nothing(self) -> None:
        idle = realized_window(_idle_slots(), 4.0, _SITE)
        plain = simulate_self_consumption(idle.slots, idle.configured, 4.0)

        score = score_window(realized_window(_run_as(_idle_slots(), plain), 4.0, _SITE))

        assert score is not None
        assert score.savings == pytest.approx(0.0, abs=1e-6)
        assert score.capture == pytest.approx(0.0, abs=1e-6)


class TestStoredEnergyAtTheEnd:
    def test_the_oracle_does_not_win_by_draining_the_battery(self) -> None:
        """Without the end constraint it would sell the battery's content."""
        window = realized_window(_idle_slots(), 9.0, _SITE)

        kept = window.oracle(9.0)
        drained = window.oracle(_SITE.battery().min_stored_kwh)

        assert kept is not None and drained is not None
        assert kept.stored_kwh[-1] >= 9.0 - 1e-6
        assert drained.stored_kwh[-1] < 9.0 - 1.0
        assert drained.cost < kept.cost - 0.5

    def test_a_window_that_ends_fuller_is_not_punished_for_it(self) -> None:
        """Charging the battery and keeping it costs money, but is not regret.

        The realized run buys 4 kWh at the cheapest price and still holds it
        at the end.  The oracle has to end as full, so the purchase is in its
        cost too: the capture stays between 0 and 100 %.
        """
        slots = _idle_slots()
        slots[2] = replace(
            slots[2], grid_import_kwh=slots[2].grid_import_kwh + 4.0, charged_kwh=4.0
        )
        window = realized_window(slots, 1.0, _SITE)

        score = score_window(window)

        assert score is not None
        assert score.realized_end_kwh == pytest.approx(1.0 + 4.0 * 0.97)
        assert score.baseline_end_kwh < score.realized_end_kwh - 3.0
        assert score.regret >= -1e-9
        assert score.capture is not None
        assert 0.0 <= score.capture <= 1.0

    def test_the_realized_window_is_feasible_for_its_own_oracle(self) -> None:
        """Limits are widened to the realized flows, never tightened."""
        slots = _idle_slots()
        # 7 kWh charged in one hour against a 5 kW limit, on a 6 kW fuse.
        slots[1] = replace(slots[1], grid_import_kwh=7.6, charged_kwh=7.0)
        tight = replace(_SITE, max_grid_import_kw=6.0)

        window = realized_window(slots, 1.0, tight)
        score = score_window(window)

        assert window.caps["max_charge_kwh"] is not None
        assert window.caps["max_charge_kwh"][1] == pytest.approx(7.0)
        assert window.caps["max_grid_import_kwh"] is not None
        assert window.caps["max_grid_import_kwh"][1] == pytest.approx(7.6)
        assert window.caps["max_grid_import_kwh"][0] == pytest.approx(6.0)
        assert any("exceeds the configured limit" in note for note in window.notes)
        assert score is not None
        assert score.regret >= -1e-9

    def test_flows_beyond_the_capacity_widen_it(self) -> None:
        slots = _idle_slots()
        slots[1] = replace(slots[1], grid_import_kwh=4.6, charged_kwh=4.0)

        window = realized_window(slots, 9.0, _SITE)

        assert window.capacity_drift_kwh == pytest.approx(9.0 + 4.0 * 0.97 - 10.0)
        assert window.widened.max_stored_kwh == pytest.approx(9.0 + 4.0 * 0.97)
        assert any("leave the configured capacity" in note for note in window.notes)

    def test_an_empty_window_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="at least one slot"):
            realized_window([], 1.0, _SITE)


class TestTheUsersOwnSettings:
    """Money the user's settings forbid is not a missed opportunity."""

    #: An evening export price well above the night import price: exporting
    #: the battery pays, when it is allowed.
    _SPREAD = [0.2] * 6 + [1.0] * 11 + [3.0] * 4 + [1.0] * 3

    def _oracle_export(self, site: SiteLimits) -> float:
        window = realized_window(
            _idle_slots(self._SPREAD, export_discount=0.1), 5.0, site
        )
        run = window.oracle(5.0)
        assert run is not None
        return sum(run.grid_export_kwh)

    def test_the_oracle_exports_the_battery_when_the_user_allows_it(self) -> None:
        assert self._oracle_export(_SITE) > 0.5

    def test_it_does_not_when_battery_export_is_switched_off(self) -> None:
        off = replace(_SITE, battery_export_enabled=False)

        assert self._oracle_export(off) == pytest.approx(0.0, abs=1e-6)

    def test_it_does_not_below_the_users_export_floor(self) -> None:
        """The floor is compared with the raw export price, before the fee."""
        floored = replace(_SITE, battery_export_min_price=3.5)

        assert self._oracle_export(floored) == pytest.approx(0.0, abs=1e-6)

    def test_pv_surplus_is_still_exported(self) -> None:
        """Only the battery is kept from exporting, not the PV."""
        slots = _idle_slots(self._SPREAD, export_discount=0.1)
        slots[12] = replace(slots[12], grid_import_kwh=0.0, grid_export_kwh=3.0)
        # A battery that cannot charge leaves the surplus nowhere else to go.
        off = replace(_SITE, battery_export_enabled=False, max_charge_kw=0.0)

        window = realized_window(slots, 5.0, off)
        run = window.oracle(5.0)

        assert window.caps["max_grid_export_kwh"] is not None
        assert window.caps["max_grid_export_kwh"][12] == pytest.approx(3.0)
        assert window.caps["max_grid_export_kwh"][11] == pytest.approx(0.0)
        assert run is not None
        assert run.grid_export_kwh[12] == pytest.approx(3.0, abs=1e-6)

    def test_an_export_the_meters_recorded_is_never_cut(self) -> None:
        """The realized run stays feasible even where it broke the setting."""
        slots = _idle_slots(self._SPREAD, export_discount=0.1)
        slots[18] = replace(
            slots[18], grid_import_kwh=0.0, grid_export_kwh=1.0, discharged_kwh=3.1
        )
        off = replace(_SITE, battery_export_enabled=False)

        window = realized_window(slots, 9.0, off)
        score = score_window(window)

        assert window.caps["max_grid_export_kwh"] is not None
        assert window.caps["max_grid_export_kwh"][18] == pytest.approx(1.0)
        assert score is not None
        assert score.regret >= -1e-9

    def test_no_export_limit_means_no_export_caps(self) -> None:
        window = realized_window(_idle_slots(), 4.0, _SITE)

        assert window.caps["max_grid_export_kwh"] is None

    def test_the_export_limit_still_applies_with_battery_export_on(self) -> None:
        capped = replace(_SITE, max_grid_export_kw=2.0)

        window = realized_window(_idle_slots(), 4.0, capped)

        assert window.caps["max_grid_export_kwh"] == pytest.approx([2.0] * 24)


class TestSiteLimits:
    def test_the_users_export_settings_are_read_from_a_planner_input(self) -> None:
        site = SiteLimits.from_planner_input(
            PlannerInput(
                excess_export_enabled=True,
                export_min_price=0.03,
                battery_export_min_price=0.25,
            )
        )

        assert site.battery_export_enabled is True
        assert site.battery_export_min_price == pytest.approx(0.25)
        assert site.battery_may_export(0.25)
        assert not site.battery_may_export(0.24)

    def test_battery_export_is_off_unless_the_user_enabled_it(self) -> None:
        site = SiteLimits.from_planner_input(PlannerInput())

        assert site.battery_export_enabled is False
        assert not site.battery_may_export(10.0)


class TestBatteryFlowsFromSoc:
    def test_a_rise_is_a_charge_on_the_ac_side(self) -> None:
        charged, discharged = battery_flows_from_soc(40.0, 50.0, _SITE)

        assert charged == pytest.approx(1.0 / 0.97)
        assert discharged == pytest.approx(0.0)

    def test_a_fall_is_a_discharge_on_the_ac_side(self) -> None:
        charged, discharged = battery_flows_from_soc(50.0, 40.0, _SITE)

        assert charged == pytest.approx(0.0)
        assert discharged == pytest.approx(1.0 * 0.97)

    def test_the_flows_reproduce_the_soc(self) -> None:
        """Run back through the battery model they end where the SoC ended."""
        readings = [40.0, 55.0, 55.0, 31.0, 62.0]
        flows = [
            battery_flows_from_soc(a, b, _SITE)
            for a, b in zip(readings, readings[1:], strict=False)
        ]
        slots = [
            RealizedSlot(
                hours=1.0,
                grid_import_kwh=charged,
                grid_export_kwh=discharged,
                charged_kwh=charged,
                discharged_kwh=discharged,
                import_price=1.0,
                export_price=1.0,
            )
            for charged, discharged in flows
        ]

        window = realized_window(slots, 4.0, _SITE)

        assert window.stored_kwh == pytest.approx([5.5, 5.5, 3.1, 6.2])


class TestBatteryAndSolarSplit:
    """The regret is split by an oracle that keeps the PV handling (issue #1270)."""

    #: Flat prices with a poor export price: nothing to gain from the grid,
    #: everything to gain from keeping PV surplus for later.
    _FLAT = [1.0] * 24
    #: 3 kWh of surplus in each of three midday hours, load at all other times.
    _SUNNY = [0.6] * 11 + [-3.0] * 3 + [0.6] * 10

    #: A cheap night and a dear evening on top: the grid has something to
    #: offer as well.
    _PEAKY = [0.3] * 6 + [1.0] * 11 + [3.0] * 4 + [0.3] * 3

    def _sunny_idle(self, prices: list[float] | None = None) -> list[RealizedSlot]:
        """Return a sunny day with an idle battery and an export price of 0.1."""
        return [
            replace(slot, export_price=0.1)
            for slot in _idle_slots(prices or self._FLAT, self._SUNNY)
        ]

    def test_the_two_parts_add_up_to_the_regret(self) -> None:
        score = score_window(realized_window(_idle_slots(), 4.0, _SITE), breakdown=True)

        assert score is not None
        assert score.battery_loss is not None and score.solar_loss is not None
        assert score.battery_loss + score.solar_loss == pytest.approx(score.regret)
        assert score.battery_loss >= -1e-9
        assert score.solar_loss >= -1e-9

    def test_the_costs_are_ordered(self) -> None:
        """Oracle ≤ PV-as-realized ≤ realized: each can do what the next can."""
        score = score_window(
            realized_window(self._sunny_idle(), 1.0, _SITE), breakdown=True
        )

        assert score is not None and score.pv_as_realized_cost is not None
        assert score.oracle_cost <= score.pv_as_realized_cost + 1e-9
        assert score.pv_as_realized_cost <= score.realized_cost + 1e-9

    def test_an_unused_battery_without_pv_is_battery_loss(self) -> None:
        """No surplus to handle: all that was missed is dispatch."""
        score = score_window(realized_window(_idle_slots(), 4.0, _SITE), breakdown=True)

        assert score is not None
        assert score.solar_loss == pytest.approx(0.0, abs=1e-6)
        assert score.battery_loss == pytest.approx(score.regret)
        assert score.regret > 1.0

    def test_surplus_sold_cheaply_and_bought_back_is_solar_loss(self) -> None:
        """With flat prices the grid offers nothing; the PV surplus does."""
        score = score_window(
            realized_window(self._sunny_idle(), 0.5, _SITE), breakdown=True
        )

        assert score is not None
        assert score.battery_loss == pytest.approx(0.0, abs=1e-6)
        assert score.solar_loss == pytest.approx(score.regret)
        # 9 kWh of surplus was sold at 0.1 and bought back at 1.0.
        assert score.regret > 5.0
        # Self-consumption alone would have got it right, so there is no
        # potential to take a share of: the amounts say it, the shares cannot.
        assert score.potential == pytest.approx(0.0, abs=1e-6)
        assert score.solar_utilisation is None

    def test_surplus_that_was_stored_but_never_used_is_battery_loss(self) -> None:
        """The PV handling was right; the energy was then held."""
        slots = self._sunny_idle(self._PEAKY)
        for index in (11, 12, 13):
            slots[index] = replace(slots[index], grid_export_kwh=0.0, charged_kwh=3.0)

        score = score_window(realized_window(slots, 0.5, _SITE), breakdown=True)

        assert score is not None
        # Using it in the dear evening and refilling in the cheap hours after
        # would have paid; storing it was already the best use of the surplus.
        assert score.solar_loss == pytest.approx(0.0, abs=1e-6)
        assert score.battery_loss == pytest.approx(score.regret)
        assert score.regret > 5.0

    def test_a_run_like_the_oracle_loses_nothing_on_either(self) -> None:
        day = self._sunny_idle(self._PEAKY)
        best = realized_window(day, 0.5, _SITE).oracle(0.5)
        assert best is not None

        score = score_window(
            realized_window(_run_as(day, best), 0.5, _SITE), breakdown=True
        )

        assert score is not None
        assert score.battery_loss == pytest.approx(0.0, abs=1e-6)
        assert score.solar_loss == pytest.approx(0.0, abs=1e-6)
        assert score.battery_utilisation == pytest.approx(1.0, abs=1e-6)
        assert score.solar_utilisation == pytest.approx(1.0, abs=1e-6)

    def test_the_shortfalls_of_the_shares_add_up_to_the_captures(self) -> None:
        score = score_window(
            realized_window(self._sunny_idle(self._PEAKY), 0.5, _SITE),
            breakdown=True,
        )

        assert score is not None and score.capture is not None
        assert score.battery_utilisation is not None
        assert score.solar_utilisation is not None
        # An idle battery on a day that offers both: each part lost something.
        assert score.battery_utilisation < 1.0 - 1e-3
        assert score.solar_utilisation < 1.0 - 1e-3
        assert (1 - score.battery_utilisation) + (1 - score.solar_utilisation) == (
            pytest.approx(1 - score.capture)
        )

    def test_without_the_split_the_score_is_the_same(self) -> None:
        window = realized_window(self._sunny_idle(), 0.5, _SITE)

        plain = score_window(window)
        split = score_window(window, breakdown=True)

        assert plain is not None and split is not None
        assert plain.pv_as_realized_cost is None
        assert plain.battery_loss is None and plain.solar_loss is None
        assert plain.battery_utilisation is None and plain.solar_utilisation is None
        assert replace(split, pv_as_realized_cost=None) == plain

    def test_a_failed_split_leaves_the_score(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the extra solve failed: the stage-1 figures stand."""
        window = realized_window(self._sunny_idle(), 0.5, _SITE)
        solve = type(window).oracle

        def without_split(self: object, *args: object, **kwargs: object) -> object:
            if kwargs.get("pv_as_realized"):
                return None
            return solve(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(type(window), "oracle", without_split)

        score = score_window(window, breakdown=True)

        assert score is not None
        assert score.regret > 5.0
        assert score.pv_as_realized_cost is None

    def test_the_shares_are_not_clamped_and_need_a_potential(self) -> None:
        assert utilisation_share(3.0, 2.0) == pytest.approx(-0.5)
        assert utilisation_share(0.0, 2.0) == pytest.approx(1.0)
        assert utilisation_share(None, 2.0) is None
        assert utilisation_share(0.01, MIN_POTENTIAL - 0.001) is None


class TestPvChargeBounds:
    """What "the PV handling as it happened" means per slot."""

    def _bounds(self, slot: RealizedSlot) -> tuple[float, float]:
        window = realized_window([slot], 1.0, _SITE)
        minimum, maximum = window.pv_charge_bounds()
        return minimum[0], maximum[0]

    def _slot(
        self,
        *,
        grid_import: float = 0.0,
        grid_export: float = 0.0,
        charged: float = 0.0,
    ) -> RealizedSlot:
        return RealizedSlot(
            hours=1.0,
            grid_import_kwh=grid_import,
            grid_export_kwh=grid_export,
            charged_kwh=charged,
            discharged_kwh=0.0,
            import_price=1.0,
            export_price=0.5,
        )

    def test_a_slot_without_surplus_is_left_free(self) -> None:
        """Any charge there is grid charge, which the oracle may redo."""
        assert self._bounds(self._slot(grid_import=2.0, charged=1.5)) == (
            pytest.approx(0.0),
            pytest.approx(5.0),
        )

    def test_surplus_that_was_all_exported_may_not_be_stored(self) -> None:
        assert self._bounds(self._slot(grid_export=3.0)) == (
            pytest.approx(0.0),
            pytest.approx(0.0),
        )

    def test_surplus_that_was_partly_stored_is_stored_just_as_much(self) -> None:
        """3 kWh of surplus: 1 stored, 2 exported."""
        assert self._bounds(self._slot(grid_export=2.0, charged=1.0)) == (
            pytest.approx(1.0),
            pytest.approx(1.0),
        )

    def test_surplus_stored_in_full_leaves_grid_charge_on_top_free(self) -> None:
        """2 kWh of surplus all stored, plus 1 kWh from the grid."""
        assert self._bounds(self._slot(grid_import=1.0, charged=3.0)) == (
            pytest.approx(2.0),
            pytest.approx(5.0),
        )

    def test_the_realized_run_satisfies_its_own_bounds(self) -> None:
        slots = [
            self._slot(grid_import=2.0, charged=1.5),
            self._slot(grid_export=3.0),
            self._slot(grid_export=2.0, charged=1.0),
            self._slot(grid_import=1.0, charged=3.0),
        ]
        window = realized_window(slots, 1.0, _SITE)

        minimum, maximum = window.pv_charge_bounds()

        for charged, low, high in zip(
            window.charged_kwh, minimum, maximum, strict=True
        ):
            assert low - 1e-9 <= charged <= high + 1e-9

    def test_pv_is_not_curtailed_further_either(self) -> None:
        """A negative export price: the oracle curtails, the held run cannot."""
        slot = replace(self._slot(grid_export=3.0), export_price=-0.5, pv_kwh=4.0)
        window = realized_window([slot], 9.5, _SITE)

        free = window.oracle(9.5)
        held = window.oracle(9.5, pv_as_realized=True)

        assert free is not None and held is not None
        assert free.grid_export_kwh[0] == pytest.approx(0.0, abs=1e-6)
        assert held.grid_export_kwh[0] == pytest.approx(3.0, abs=1e-6)


class TestTimeLimit:
    def test_a_solve_that_cannot_finish_in_time_gives_no_score(self) -> None:
        """An unproven result is never reported as a number."""
        window = realized_window(_idle_slots(), 4.0, _SITE)

        assert score_window(window, time_limit_s=0.0) is None
        assert score_window(window, time_limit_s=30.0) is not None

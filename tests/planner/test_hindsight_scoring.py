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


class TestTimeLimit:
    def test_a_solve_that_cannot_finish_in_time_gives_no_score(self) -> None:
        """An unproven result is never reported as a number."""
        window = realized_window(_idle_slots(), 4.0, _SITE)

        assert score_window(window, time_limit_s=0.0) is None
        assert score_window(window, time_limit_s=30.0) is not None

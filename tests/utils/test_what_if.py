"""Tests for the replay behind ``hsem.what_if`` (issue #1432).

The service answers "what would another battery have been worth?" with the
difference of two perfect-foresight costs.  These tests pin the direction of
that difference for every limit a scenario can change, that both sides of a
comparison cover the same days, and that a day the performance scoring would
not stand behind is counted as skipped instead of compared.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.hsem.models.performance_record import (
    REASON_INSUFFICIENT_COVERAGE,
    REASON_ORACLE_UNSOLVED,
    REASON_SCIPY_UNAVAILABLE,
    REASON_STORED_ENERGY_OUTSIDE_LIMITS,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils import what_if
from custom_components.hsem.utils.what_if import (
    MAX_SCENARIOS,
    MIN_DAYS_FOR_YEARLY,
    NOTE,
    REASON_NOT_RECORDED,
    REASON_ORACLE_TIMEOUT,
    REASON_RUN_TIME_LIMIT,
    WhatIfRequest,
    WhatIfScenario,
    capacity_range,
    run_what_if,
    usable_capacity_kwh,
)
from tests.utils.what_if_days import FIRST_DAY, SITE, make_day, make_days

needs_scipy = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_DAYS = make_days(3)
#: Enough days for a yearly figure.
_WEEK = make_days(MIN_DAYS_FOR_YEARLY)
#: Rounding of the response: three decimals per figure.
_ROUNDING = 2e-3


def _request(*scenarios: WhatIfScenario, **changes: Any) -> WhatIfRequest:
    """Return a what-if over the three recorded days."""
    request = WhatIfRequest(
        days=tuple(_DAYS),
        recorded=_DAYS,
        reasons={},
        site=SITE,
        cycle_cost_per_kwh=0.0,
        scenarios=scenarios,
    )
    return replace(request, **changes)


def _run(*scenarios: WhatIfScenario, **changes: Any) -> dict[str, Any]:
    response = run_what_if(_request(*scenarios, **changes))
    assert response is not None
    return response


def _run_week(*scenarios: WhatIfScenario, **changes: Any) -> dict[str, Any]:
    return _run(*scenarios, days=tuple(_WEEK), recorded=_WEEK, **changes)


class TestScenario:
    def test_an_empty_scenario_is_the_configured_battery(self) -> None:
        assert WhatIfScenario().changes_nothing
        assert WhatIfScenario().site(SITE) == SITE
        assert not WhatIfScenario(cycle_cost_per_kwh=0.1).changes_nothing

    def test_the_usable_capacity_scales_the_rated_capacity(self) -> None:
        """The SoC limits stay as configured: 5 to 100 % of 20 kWh is 19 kWh."""
        site = WhatIfScenario(usable_kwh=19.0).site(SITE)

        assert usable_capacity_kwh(SITE) == pytest.approx(9.5)
        assert usable_capacity_kwh(site) == pytest.approx(19.0)
        assert site.rated_kwh == pytest.approx(20.0)
        assert (site.min_soc_pct, site.max_soc_pct) == pytest.approx((5.0, 100.0))

    def test_every_other_override_replaces_its_own_limit(self) -> None:
        site = WhatIfScenario(
            max_charge_kw=2.0,
            max_discharge_kw=3.0,
            charge_efficiency=0.9,
            discharge_efficiency=0.8,
        ).site(SITE)

        assert site == replace(
            SITE,
            max_charge_kw=2.0,
            max_discharge_kw=3.0,
            charge_efficiency=0.9,
            discharge_efficiency=0.8,
        )


class TestCapacityRange:
    def test_the_end_is_included_when_the_steps_land_on_it(self) -> None:
        assert capacity_range(5.0, 15.0, 5.0) == pytest.approx([5.0, 10.0, 15.0])
        assert capacity_range(5.0, 14.0, 5.0) == pytest.approx([5.0, 10.0])
        assert capacity_range(0.1, 0.3, 0.1) == pytest.approx([0.1, 0.2, 0.3])
        assert capacity_range(7.0, 7.0, 1.0) == pytest.approx([7.0])

    @pytest.mark.parametrize(
        ("start", "end", "step"),
        [(5.0, 15.0, 0.0), (5.0, 15.0, -1.0), (15.0, 5.0, 1.0)],
    )
    def test_an_empty_range_is_refused(
        self, start: float, end: float, step: float
    ) -> None:
        with pytest.raises(ValueError, match="start <= end and a positive step"):
            capacity_range(start, end, step)

    def test_a_range_with_too_many_sizes_is_refused(self) -> None:
        assert len(capacity_range(1.0, float(MAX_SCENARIOS), 1.0)) == MAX_SCENARIOS
        with pytest.raises(ValueError, match=f"at most {MAX_SCENARIOS}"):
            capacity_range(1.0, MAX_SCENARIOS + 1.0, 1.0)


@needs_scipy
class TestDirection:
    """A scenario moves the cost the way the hardware change must move it."""

    def test_the_configured_battery_against_itself_saves_nothing(self) -> None:
        response = _run(WhatIfScenario(usable_kwh=usable_capacity_kwh(SITE)))

        row = response["scenarios"][0]
        assert row["grid_savings"] == pytest.approx(0.0, abs=_ROUNDING)
        assert row["scenario_grid_cost"] == pytest.approx(
            row["current_grid_cost"], abs=_ROUNDING
        )

    def test_the_configured_power_entered_by_hand_saves_nothing(self) -> None:
        """A meter that read above the configured power widens both sides.

        The days were recorded charging at up to 5 kW.  With 1 kW configured,
        the configured side may repeat what was measured; a scenario that
        enters the same 1 kW must get the same limits, not the bare 1 kW.
        """
        site = replace(SITE, max_charge_kw=1.0, max_discharge_kw=1.0)

        response = _run(
            WhatIfScenario(max_charge_kw=1.0, max_discharge_kw=1.0), site=site
        )

        assert response["days_compared"] == 3
        assert response["scenarios"][0]["grid_savings"] == pytest.approx(
            0.0, abs=_ROUNDING
        )
        assert all(
            day["grid_savings"] == pytest.approx(0.0, abs=_ROUNDING)
            for day in response["days"]
        )

    def test_a_day_is_rebuilt_with_the_battery_it_was_recorded_with(self) -> None:
        """Doubling the battery later does not double the old days' flows.

        The days were recorded with 10 kWh.  Seen from a site that has 20 kWh
        today, the 20 kWh battery must cost what the 20 kWh scenario cost when
        10 kWh was configured, and the other way round.
        """
        before = _run(WhatIfScenario(usable_kwh=19.0))
        after = _run(WhatIfScenario(usable_kwh=9.5), site=replace(SITE, rated_kwh=20.0))

        assert after["days_compared"] == 3
        was, now = before["scenarios"][0], after["scenarios"][0]
        assert now["current_grid_cost"] == pytest.approx(
            was["scenario_grid_cost"], abs=_ROUNDING
        )
        assert now["scenario_grid_cost"] == pytest.approx(
            was["current_grid_cost"], abs=_ROUNDING
        )

    def test_more_capacity_is_worth_something_and_less_costs(self) -> None:
        response = _run(
            WhatIfScenario(usable_kwh=4.0),
            WhatIfScenario(usable_kwh=15.0),
            WhatIfScenario(usable_kwh=25.0),
        )

        small, large, larger = (row["grid_savings"] for row in response["scenarios"])
        assert small < 0.0
        assert large > 0.0
        assert larger >= large - _ROUNDING

    def test_less_power_never_makes_a_day_cheaper(self) -> None:
        response = _run(WhatIfScenario(max_charge_kw=0.5, max_discharge_kw=0.5))

        assert response["scenarios"][0]["grid_savings"] < 0.0
        assert all(day["grid_savings"] <= _ROUNDING for day in response["days"])

    def test_lower_efficiency_costs(self) -> None:
        response = _run(WhatIfScenario(charge_efficiency=0.8, discharge_efficiency=0.8))

        row = response["scenarios"][0]
        assert row["grid_savings"] < 0.0
        assert row["charge_efficiency_pct"] == pytest.approx(80.0)
        assert row["discharge_efficiency_pct"] == pytest.approx(80.0)

    def test_wear_holds_the_battery_back_and_is_part_of_the_cost(self) -> None:
        """A dearer cycle buys less arbitrage: the grid bill goes up."""
        response = _run(WhatIfScenario(cycle_cost_per_kwh=0.4))

        row = response["scenarios"][0]
        assert response["current"]["cycle_cost_per_kwh"] == pytest.approx(0.0)
        assert row["cycle_cost_per_kwh"] == pytest.approx(0.4)
        assert row["current_wear_cost"] == pytest.approx(0.0)
        assert row["scenario_grid_cost"] > row["current_grid_cost"]
        assert row["scenario_wear_cost"] >= 0.0
        assert row["grid_savings"] < 0.0
        assert row["savings_after_wear"] == pytest.approx(
            row["grid_savings"] - row["scenario_wear_cost"], abs=2 * _ROUNDING
        )

    def test_the_configured_wear_applies_to_both_sides(self) -> None:
        response = _run(WhatIfScenario(usable_kwh=15.0), cycle_cost_per_kwh=0.2)

        row = response["scenarios"][0]
        assert row["cycle_cost_per_kwh"] == pytest.approx(0.2)
        assert row["current_wear_cost"] > 0.0
        assert row["scenario_wear_cost"] > 0.0


@needs_scipy
class TestResponse:
    def test_a_single_scenario_lists_each_day(self) -> None:
        response = _run(WhatIfScenario(usable_kwh=15.0))

        row = response["scenarios"][0]
        assert [day["date"] for day in response["days"]] == list(_DAYS)
        for name in (
            "current_grid_cost",
            "scenario_grid_cost",
            "grid_savings",
            "savings_after_wear",
        ):
            assert sum(day[name] for day in response["days"]) == pytest.approx(
                row[name], abs=3 * _ROUNDING
            )
        for day in response["days"]:
            assert day["grid_savings"] == pytest.approx(
                day["current_grid_cost"] - day["scenario_grid_cost"], abs=_ROUNDING
            )

    def test_it_says_what_was_compared_and_what_the_figures_are(self) -> None:
        response = _run(WhatIfScenario(usable_kwh=15.0))

        assert response["history_days"] == 3
        assert response["days_compared"] == 3
        assert response["first_day_compared"] == "2026-06-01"
        assert response["last_day_compared"] == "2026-06-03"
        assert response["days_skipped"] == 0
        assert response["days_timed_out"] == 0
        assert response["skipped_reasons"] == {}
        assert response["note"] == NOTE
        assert "perfect-foresight bounds" in response["note"]
        assert "season" in response["note"]
        assert "grid savings alone" in response["note"]
        assert response["current"] == {
            "usable_capacity_kwh": pytest.approx(9.5),
            "max_charge_power_w": pytest.approx(5000.0),
            "max_discharge_power_w": pytest.approx(5000.0),
            "charge_efficiency_pct": pytest.approx(97.0),
            "discharge_efficiency_pct": pytest.approx(97.0),
            "cycle_cost_per_kwh": pytest.approx(0.0),
        }
        assert response["scenarios"][0]["usable_capacity_kwh"] == pytest.approx(15.0)

    def test_a_year_is_the_daily_mean_times_365(self) -> None:
        row = _run_week(WhatIfScenario(usable_kwh=15.0))["scenarios"][0]

        assert row["grid_savings_per_year"] == pytest.approx(
            row["grid_savings"] / MIN_DAYS_FOR_YEARLY * 365, abs=0.2
        )

    def test_a_few_days_are_not_scaled_to_a_year(self) -> None:
        """Below a week the yearly figure and the payback are left out."""
        days = dict(list(_WEEK.items())[: MIN_DAYS_FOR_YEARLY - 1])

        response = _run(
            WhatIfScenario(usable_kwh=15.0),
            days=tuple(days),
            recorded=days,
            capacity_price_per_kwh=100.0,
        )

        row = response["scenarios"][0]
        assert response["days_compared"] == MIN_DAYS_FOR_YEARLY - 1
        assert row["grid_savings"] > 0.0
        assert row["grid_savings_per_year"] is None
        assert row["investment"] == pytest.approx(550.0)
        assert row["payback_years"] is None

    def test_several_scenarios_give_one_row_each_and_no_days(self) -> None:
        response = _run(*(WhatIfScenario(usable_kwh=size) for size in (12.0, 15.0)))

        assert [row["usable_capacity_kwh"] for row in response["scenarios"]] == (
            pytest.approx([12.0, 15.0])
        )
        assert "days" not in response

    def test_the_response_is_json(self) -> None:
        response = _run(WhatIfScenario(usable_kwh=15.0), capacity_price_per_kwh=100.0)

        assert json.loads(json.dumps(response)) == response


@needs_scipy
class TestPayback:
    def test_no_price_no_payback(self) -> None:
        row = _run(WhatIfScenario(usable_kwh=15.0))["scenarios"][0]

        assert "payback_years" not in row
        assert "investment" not in row

    def test_the_extra_capacity_is_paid_back_by_the_yearly_savings(self) -> None:
        row = _run_week(WhatIfScenario(usable_kwh=15.0), capacity_price_per_kwh=100.0)[
            "scenarios"
        ][0]

        assert row["extra_capacity_kwh"] == pytest.approx(5.5)
        assert row["investment"] == pytest.approx(550.0)
        assert row["payback_years"] == pytest.approx(
            550.0 / row["grid_savings_per_year"], abs=0.06
        )

    def test_the_payback_does_not_charge_the_battery_twice(self) -> None:
        """Wear is the purchase price per kWh cycled: it stays out of the payback.

        The bigger battery cycles more, so its wear is higher and the savings
        after wear are lower than the grid savings.  The investment is paid
        back by the grid savings.
        """
        row = _run_week(
            WhatIfScenario(usable_kwh=15.0),
            cycle_cost_per_kwh=0.2,
            capacity_price_per_kwh=100.0,
        )["scenarios"][0]

        assert row["scenario_wear_cost"] > row["current_wear_cost"]
        assert row["savings_after_wear"] < row["grid_savings"] - 10 * _ROUNDING
        assert row["grid_savings_per_year"] == pytest.approx(
            row["grid_savings"] / MIN_DAYS_FOR_YEARLY * 365, abs=0.2
        )
        assert row["payback_years"] == pytest.approx(
            550.0 / row["grid_savings_per_year"], abs=0.06
        )

    def test_a_smaller_battery_has_no_payback(self) -> None:
        row = _run_week(WhatIfScenario(usable_kwh=4.0), capacity_price_per_kwh=100.0)[
            "scenarios"
        ][0]

        assert row["extra_capacity_kwh"] == pytest.approx(-5.5)
        assert row["payback_years"] is None

    def test_a_change_that_saves_nothing_has_no_payback(self) -> None:
        row = _run_week(
            WhatIfScenario(usable_kwh=15.0, max_charge_kw=0.1, max_discharge_kw=0.1),
            capacity_price_per_kwh=100.0,
        )["scenarios"][0]

        assert row["grid_savings"] < 0.0
        assert row["investment"] == pytest.approx(550.0)
        assert row["payback_years"] is None


class TestSkippedDays:
    def test_a_day_that_was_never_recorded_is_counted(self) -> None:
        """Every day of the asked-for history is compared or skipped."""
        missing = (FIRST_DAY - timedelta(days=1)).isoformat()
        unscored = (FIRST_DAY - timedelta(days=2)).isoformat()

        response = _run(
            WhatIfScenario(usable_kwh=15.0),
            days=(unscored, missing),
            reasons={unscored: REASON_INSUFFICIENT_COVERAGE},
            capacity_price_per_kwh=100.0,
        )

        assert response["days_compared"] == 0
        assert response["days_skipped"] == 2
        assert response["skipped_reasons"] == {
            REASON_INSUFFICIENT_COVERAGE: 1,
            REASON_NOT_RECORDED: 1,
        }
        assert response["days"] == []
        assert response["first_day_compared"] is None
        assert response["last_day_compared"] is None
        row = response["scenarios"][0]
        assert row["grid_savings"] == pytest.approx(0.0)
        assert row["grid_savings_per_year"] is None
        assert row["payback_years"] is None

    @needs_scipy
    def test_a_day_whose_meters_contradict_the_battery_is_skipped(self) -> None:
        """A SoC that leaves the configured capacity is not replayed."""
        good = make_day(FIRST_DAY)
        bad = replace(
            good,
            day="2026-06-02",
            soc_pct=tuple(value + 80.0 for value in good.soc_pct),
        )

        response = _run(
            WhatIfScenario(usable_kwh=15.0),
            days=(good.day, bad.day),
            recorded={good.day: good, bad.day: bad},
        )

        assert response["days_compared"] == 1
        assert response["skipped_reasons"] == {REASON_STORED_ENERGY_OUTSIDE_LIMITS: 1}
        assert [day["date"] for day in response["days"]] == [good.day]

    @needs_scipy
    def test_a_day_one_scenario_cannot_solve_is_skipped_for_all(self) -> None:
        """Every row of a response covers the same days."""
        solve = what_if.solve_hindsight_oracle
        calls: list[float] = []

        def unsolved_for_the_large_battery(*args: Any, **kwargs: Any) -> Any:
            calls.append(args[1].max_stored_kwh)
            if args[1].max_stored_kwh > 20.0 and len(calls) <= 3:
                return None
            return solve(*args, **kwargs)

        with patch.object(
            what_if, "solve_hindsight_oracle", unsolved_for_the_large_battery
        ):
            response = _run(
                WhatIfScenario(usable_kwh=15.0), WhatIfScenario(usable_kwh=25.0)
            )

        assert response["days_compared"] == 2
        assert response["skipped_reasons"] == {REASON_ORACLE_UNSOLVED: 1}
        assert response["days_timed_out"] == 0

    @needs_scipy
    def test_a_solve_that_ran_out_of_time_is_counted_as_that(self) -> None:
        """A day lost to the time limit is told apart from one with no answer."""
        with patch.object(what_if, "solve_hindsight_oracle", return_value=None):
            response = _run(WhatIfScenario(usable_kwh=15.0), time_limit_s=0.0)

        assert response["days_compared"] == 0
        assert response["skipped_reasons"] == {REASON_ORACLE_TIMEOUT: 3}
        assert response["days_timed_out"] == 3

    def test_a_run_out_of_time_skips_the_days_it_did_not_reach(self) -> None:
        response = _run(WhatIfScenario(usable_kwh=15.0), budget_s=0.0)

        assert response["days_compared"] == 0
        assert response["skipped_reasons"] == {REASON_RUN_TIME_LIMIT: 3}
        assert response["days_timed_out"] == 3

    @needs_scipy
    def test_a_run_inside_its_time_compares_every_day(self) -> None:
        response = _run(WhatIfScenario(usable_kwh=15.0), budget_s=600.0)

        assert response["days_compared"] == 3

    def test_without_scipy_every_recorded_day_is_skipped(self) -> None:
        with patch.object(what_if, "is_scipy_available", return_value=False):
            response = _run(WhatIfScenario(usable_kwh=15.0))

        assert response["days_compared"] == 0
        assert response["skipped_reasons"] == {REASON_SCIPY_UNAVAILABLE: 3}


class TestCancel:
    def test_a_cancelled_run_returns_nothing(self) -> None:
        assert (
            run_what_if(_request(WhatIfScenario(usable_kwh=15.0)), lambda: True) is None
        )

    @needs_scipy
    def test_it_is_asked_before_every_solve(self) -> None:
        """Cancelling after the first solve of a day stops before the second."""
        solve = what_if.solve_hindsight_oracle
        solved: list[int] = []
        asked: list[int] = []

        def counting(*args: Any, **kwargs: Any) -> Any:
            solved.append(len(solved))
            return solve(*args, **kwargs)

        def cancelled() -> bool:
            asked.append(len(asked))
            return bool(solved)

        with patch.object(what_if, "solve_hindsight_oracle", counting):
            response = run_what_if(_request(WhatIfScenario(usable_kwh=15.0)), cancelled)

        assert response is None
        assert len(solved) == 1
        # Before the day, before its first solve and before its second.
        assert len(asked) == 3


class TestPause:
    def test_it_sleeps_after_each_replayed_day(self) -> None:
        missing = (FIRST_DAY - timedelta(days=1)).isoformat()

        with (
            patch.object(what_if, "is_scipy_available", return_value=False),
            patch.object(what_if.time, "sleep") as sleep,
        ):
            _run(
                WhatIfScenario(usable_kwh=15.0),
                days=(missing, *_DAYS),
                pause_s=0.01,
            )

        assert [call.args for call in sleep.call_args_list] == [(0.01,)] * 3

    def test_it_does_not_sleep_unless_asked(self) -> None:
        with (
            patch.object(what_if, "is_scipy_available", return_value=False),
            patch.object(what_if.time, "sleep") as sleep,
        ):
            _run(WhatIfScenario(usable_kwh=15.0))

        sleep.assert_not_called()

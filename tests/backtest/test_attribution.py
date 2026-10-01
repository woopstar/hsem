"""Tests for the regret attribution of the planner backtest (issue #1208).

A real attribution needs a planner cycle for every slot of a day, which the
committed corpus does not hold.  These tests build such a day: an hourly
24 h plan, one recorded cycle per slot, and actuals that either match the
recorded forecasts or deliberately do not.  The planner that replays them is
the real one.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner.hindsight_oracle import HindsightBattery
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils.datetime_utils import slot_key
from custom_components.hsem.utils.diagnostics import _planner_input_to_dict
from custom_components.hsem.utils.recommendations import Recommendations
from tests.backtest.actuals import Actuals
from tests.backtest.attribution import (
    DayAttribution,
    Decision,
    attribute_day,
    cycles_by_slot,
    describe,
    execute_decision,
    with_realized_forecasts,
)
from tests.backtest.scoring import SiteLimits, day_slot_keys

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_ZONE = ZoneInfo("Europe/Copenhagen")
_DAY = date(2026, 6, 10)
_MIDNIGHT = datetime(2026, 6, 10, 0, 0, tzinfo=_ZONE)
_START_SOC_PCT = 30.0

#: Hourly truth: a night barely cheaper than the day, so it is only worth
#: buying for the dear evening, and only when no PV is expected to fill the
#: battery for free.
_PRICES = [1.0] * 2 + [0.9] * 4 + [1.0] * 11 + [2.4] * 4 + [1.2] * 3
_LOAD = [0.4] * 17 + [1.2] * 4 + [0.5] * 3
_SUNNY = [0.0] * 8 + [1.0, 2.0, 3.0, 3.5, 3.5, 3.0, 2.0, 1.0] + [0.0] * 8
_CLOUDY = [0.0] * 24


def _planner_input(now: datetime, pv_forecast: list[float]) -> PlannerInput:
    """Return the input HSEM would have recorded at *now*."""
    return PlannerInput(
        now_iso=now.isoformat(),
        time_zone="Europe/Copenhagen",
        interval_minutes=60,
        interval_length_hours=24,
        battery_soc_pct=_START_SOC_PCT,
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=5.0,
        battery_max_charge_power_w=5000.0,
        battery_max_discharge_power_w=5000.0,
        battery_charge_efficiency_pct=97.0,
        battery_discharge_efficiency_pct=97.0,
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
            PricePoint(hour=h, import_price=p, export_price=p - 0.3, slot_in_day=h)
            for h, p in enumerate(_PRICES)
        ],
        solcast_slots=[
            SolcastSlot(hour=h, pv_estimate=v) for h, v in enumerate(pv_forecast)
        ],
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        time_discount_rate=1.0,
    )


def _cycles(pv_forecast: list[float], hours: range = range(24)) -> list[dict[str, Any]]:
    """Return one recorded cycle per hour, two minutes into the hour."""
    return [
        {
            "hsem_version": "test",
            "dump_timestamp": (_MIDNIGHT + timedelta(hours=h, minutes=5)).isoformat(),
            "planner_input": _planner_input_to_dict(
                _planner_input(_MIDNIGHT + timedelta(hours=h, minutes=2), pv_forecast)
            ),
        }
        for h in hours
    ]


def _actuals(pv: list[float]) -> Actuals:
    """Return a day of actuals in which the battery did nothing."""
    keys = day_slot_keys(_DAY, _ZONE, 60)
    nets = [load - sun for load, sun in zip(_LOAD, pv)]
    return Actuals(
        slot_minutes=60,
        energy_kwh={
            "grid_import": {k: max(n, 0.0) for k, n in zip(keys, nets)},
            "grid_export": {k: max(-n, 0.0) for k, n in zip(keys, nets)},
            "battery_charged": dict.fromkeys(keys, 0.0),
            "battery_discharged": dict.fromkeys(keys, 0.0),
            "pv_produced": dict(zip(keys, pv)),
            "house_load": dict(zip(keys, _LOAD)),
        },
        values={
            "import_price": dict(zip(keys, _PRICES)),
            "export_price": {k: p - 0.3 for k, p in zip(keys, _PRICES)},
            "battery_soc_pct": {keys[0]: _START_SOC_PCT},
        },
    )


def _site() -> SiteLimits:
    return SiteLimits.from_planner_input(_planner_input(_MIDNIGHT, _SUNNY))


@pytest.fixture(scope="module")
def perfect() -> DayAttribution:
    """HSEM forecast a sunny day and the day was sunny."""
    return attribute_day(_actuals(_SUNNY), _cycles(_SUNNY), _DAY, _ZONE, _site())


@pytest.fixture(scope="module")
def clouded() -> DayAttribution:
    """HSEM forecast a sunny day and the sun never came out."""
    return attribute_day(_actuals(_CLOUDY), _cycles(_SUNNY), _DAY, _ZONE, _site())


class TestAttribution:
    def test_the_three_errors_add_up_to_the_regret(
        self, perfect: DayAttribution, clouded: DayAttribution
    ) -> None:
        for result in (perfect, clouded):
            assert result.is_attributed
            assert result.regret is not None
            assert result.execution_error is not None
            assert result.forecast_error is not None
            assert result.planner_error is not None
            assert (
                result.execution_error + result.forecast_error + result.planner_error
            ) == pytest.approx(result.regret)

    def test_every_run_is_at_or_above_its_oracle(
        self, perfect: DayAttribution, clouded: DayAttribution
    ) -> None:
        for result in (perfect, clouded):
            for regret in (
                result.regret,
                result.forecast_run_regret,
                result.hindsight_run_regret,
            ):
                assert regret is not None
                assert regret >= -1e-6

    def test_perfect_forecasts_leave_no_forecast_error(
        self, perfect: DayAttribution
    ) -> None:
        """Both replays plan from the same numbers, so they are the same run."""
        assert perfect.forecast_run_cost == pytest.approx(
            perfect.hindsight_run_cost, abs=1e-6
        )
        assert perfect.forecast_error == pytest.approx(0.0, abs=1e-6)

    def test_a_forecast_that_was_wrong_is_forecast_error(
        self, clouded: DayAttribution
    ) -> None:
        """Expecting sun, the plan did not buy the night for the dear evening."""
        assert clouded.forecast_error is not None
        assert clouded.forecast_error > 0.5
        assert clouded.forecast_run_regret is not None
        assert clouded.hindsight_run_regret is not None
        assert clouded.forecast_run_regret > clouded.hindsight_run_regret

    def test_the_runs_are_compared_by_regret_not_by_cost(
        self, clouded: DayAttribution
    ) -> None:
        """The run that bought the night also ends the day fuller.

        Its bill is the higher one; its regret, against an oracle that has to
        end as full, is the lower one.
        """
        assert clouded.forecast_run_cost is not None
        assert clouded.hindsight_run_cost is not None
        assert clouded.hindsight_run_cost > clouded.forecast_run_cost

    def test_an_idle_battery_is_execution_error(self, perfect: DayAttribution) -> None:
        """The meters show a battery that did nothing; the replay used it."""
        assert perfect.realized_cost is not None
        assert perfect.forecast_run_cost is not None
        assert perfect.forecast_run_cost < perfect.realized_cost
        assert perfect.execution_error is not None
        assert perfect.execution_error > 0.5

    def test_a_fully_recorded_day_needs_no_note(self, perfect: DayAttribution) -> None:
        assert perfect.stale_slots == 0
        assert perfect.substituted_share == pytest.approx(1.0)
        assert perfect.notes == ()

    def test_attribution_is_deterministic(self, perfect: DayAttribution) -> None:
        again = attribute_day(_actuals(_SUNNY), _cycles(_SUNNY), _DAY, _ZONE, _site())

        assert again == perfect


class TestDaysThatCannotBeAttributed:
    def test_a_day_without_cycles(self) -> None:
        result = attribute_day(_actuals(_SUNNY), [], _DAY, _ZONE, _site())

        assert result.unscorable == "no planner cycle was recorded on this day"
        assert result.forecast_error is None
        assert result.planner_error is None
        assert result.execution_error is None

    def test_cycles_of_another_day_do_not_count(self) -> None:
        result = attribute_day(
            _actuals(_SUNNY), _cycles(_SUNNY), _DAY + timedelta(days=1), _ZONE, _site()
        )

        assert not result.is_attributed

    def test_too_few_cycles(self) -> None:
        result = attribute_day(
            _actuals(_SUNNY), _cycles(_SUNNY, range(0, 24, 3)), _DAY, _ZONE, _site()
        )

        assert result.unscorable == (
            "only 8 of 24 slot(s) have a planner cycle recorded in them"
        )

    def test_missing_actuals_are_passed_on(self) -> None:
        actuals = _actuals(_SUNNY)
        del actuals.energy_kwh["grid_import"][day_slot_keys(_DAY, _ZONE, 60)[5]]

        result = attribute_day(actuals, _cycles(_SUNNY), _DAY, _ZONE, _site())

        assert result.unscorable == "grid_import missing in 1/24 slot(s)"

    def test_cycles_at_another_slot_width(self) -> None:
        cycles = _cycles(_SUNNY)
        for cycle in cycles:
            cycle["planner_input"]["interval_minutes"] = 15

        result = attribute_day(_actuals(_SUNNY), cycles, _DAY, _ZONE, _site())

        assert result.unscorable == "cycles use ['15']-minute slots, the actuals 60"

    def test_an_unattributed_day_is_named_in_the_table(self) -> None:
        text = describe([DayAttribution(day=_DAY, unscorable="no data")])

        assert f"{_DAY}  not attributed: no data" in text


@pytest.fixture(scope="module")
def sparse() -> DayAttribution:
    """Every second hour has a cycle."""
    return attribute_day(
        _actuals(_SUNNY), _cycles(_SUNNY, range(1, 24, 2)), _DAY, _ZONE, _site()
    )


class TestPartlyRecordedDay:
    def test_slots_without_a_cycle_use_the_nearest_one(
        self, sparse: DayAttribution
    ) -> None:
        assert sparse.is_attributed
        assert sparse.stale_slots == 12

    def test_the_gap_is_noted(self, sparse: DayAttribution) -> None:
        assert sparse.notes == (
            "12 of 24 slot(s) had no cycle recorded in them and were planned "
            "from the nearest one",
        )
        assert "note: 12 of 24" in describe([sparse])


class TestDescribe:
    def test_an_attributed_day_shows_the_split(self, clouded: DayAttribution) -> None:
        """The example in docs/backtest-harness.md and docs/backtest-runbook.md."""
        assert describe([clouded]).splitlines() == [
            "day         realized forecast hindsight   regret = execution + forecast"
            " +  planner",
            "2026-06-10     19.96    11.03     13.10    11.24        5.84       1.65"
            "      3.75",
        ]


class TestCyclesBySlot:
    def test_the_first_cycle_of_a_slot_wins(self) -> None:
        early = _cycles(_SUNNY, range(3, 4))[0]
        late = {
            **early,
            "planner_input": {
                **early["planner_input"],
                "now_iso": (_MIDNIGHT + timedelta(hours=3, minutes=40)).isoformat(),
            },
        }

        chosen = cycles_by_slot([late, early], _DAY, _ZONE, 60)

        assert list(chosen) == [slot_key(_MIDNIGHT + timedelta(hours=3), 60)]
        assert chosen[slot_key(_MIDNIGHT + timedelta(hours=3), 60)] is early

    def test_a_cycle_without_a_usable_timestamp_is_ignored(self) -> None:
        broken = {"planner_input": {"now_iso": "not a time"}}
        naive = {"planner_input": {"now_iso": "2026-06-10T03:00:00"}}
        empty: dict[str, Any] = {}

        assert cycles_by_slot([broken, naive, empty], _DAY, _ZONE, 60) == {}


class TestRealizedForecasts:
    def test_covered_slots_get_realized_values(self) -> None:
        inp = _planner_input(_MIDNIGHT + timedelta(hours=6), _SUNNY)

        realized, share = with_realized_forecasts(inp, _actuals(_CLOUDY), _ZONE)

        assert share == pytest.approx(1.0)
        assert len(realized.price_points) == 24
        assert [p.import_price for p in realized.price_points] == pytest.approx(_PRICES)
        assert [s.pv_estimate for s in realized.solcast_slots] == pytest.approx(_CLOUDY)
        assert all(s.slot_in_day is not None for s in realized.solcast_slots)
        assert [c.avg_7d for c in realized.consumption_averages] == pytest.approx(_LOAD)
        assert realized.live_solar_production_available is False
        assert realized.live_house_consumption_available is False

    def test_uncovered_slots_keep_the_forecast(self) -> None:
        """A 48 h horizon reaches a day the actuals do not have."""
        inp = replace(
            _planner_input(_MIDNIGHT + timedelta(hours=6), _SUNNY),
            interval_length_hours=48,
        )

        realized, share = with_realized_forecasts(inp, _actuals(_CLOUDY), _ZONE)

        assert share == pytest.approx(0.5)
        tomorrow_pv = [s for s in realized.solcast_slots if s.day_offset == 1]
        # Hour-granular day-0 entries are the forecast for every day.
        assert [s.pv_estimate for s in tomorrow_pv] == pytest.approx(_SUNNY)
        tomorrow_load = [c for c in realized.consumption_averages if c.day_offset == 1]
        assert [c.avg_1d for c in tomorrow_load] == pytest.approx(_LOAD)
        # No price was published or recorded for tomorrow: left to the planner.
        assert {p.day_offset for p in realized.price_points} == {0}

    def test_a_quarter_hourly_hour_is_summed_to_the_hour(self) -> None:
        inp = replace(
            _planner_input(_MIDNIGHT + timedelta(hours=6), _SUNNY), interval_minutes=15
        )
        keys = day_slot_keys(_DAY, _ZONE, 15)
        actuals = Actuals(
            slot_minutes=15,
            energy_kwh={
                "house_load": dict.fromkeys(keys, 0.2),
                "pv_produced": dict.fromkeys(keys, 0.5),
            },
            values={
                "import_price": dict.fromkeys(keys, 1.5),
                "export_price": dict.fromkeys(keys, 1.0),
            },
        )

        realized, _share = with_realized_forecasts(inp, actuals, _ZONE)

        assert len(realized.price_points) == 96
        # 0.5 kWh in a quarter of an hour is 2 kW of average power.
        assert {s.pv_estimate for s in realized.solcast_slots} == {2.0}
        assert [c.avg_3d for c in realized.consumption_averages] == pytest.approx(
            [0.8] * 24
        )


_BATTERY = HindsightBattery(
    min_stored_kwh=0.5,
    max_stored_kwh=10.0,
    max_charge_kw=5.0,
    max_discharge_kw=5.0,
    charge_efficiency=1.0,
    discharge_efficiency=1.0,
)


def _execute(
    label: str | None,
    *,
    net: float,
    house: float | None = None,
    stored: float = 5.0,
    charged: float = 0.0,
    discharged: float = 0.0,
) -> tuple[float, float]:
    return execute_decision(
        Decision(label, charged_kwh=charged, discharged_kwh=discharged),
        net_load_kwh=net,
        house_net_kwh=net if house is None else house,
        stored_kwh=stored,
        battery=_BATTERY,
        hours=1.0,
    )


class TestExecutionModel:
    """What the battery does with a decision and the slot's real load."""

    def test_grid_charge_moves_the_planned_energy(self) -> None:
        label = Recommendations.BatteriesChargeGrid.value

        assert _execute(label, net=1.0, charged=2.0) == pytest.approx((2.0, 0.0))

    def test_grid_charge_stops_at_the_capacity_and_the_power_limit(self) -> None:
        label = Recommendations.BatteriesChargeGrid.value

        assert _execute(label, net=0.0, charged=9.0) == pytest.approx((5.0, 0.0))
        assert _execute(label, net=0.0, charged=9.0, stored=9.0) == pytest.approx(
            (1.0, 0.0)
        )

    @pytest.mark.parametrize(
        "label",
        [
            Recommendations.ForceBatteriesDischarge.value,
            Recommendations.ForceExport.value,
        ],
    )
    def test_forced_discharge_moves_the_planned_energy(self, label: str) -> None:
        assert _execute(label, net=0.3, discharged=3.0) == pytest.approx((0.0, 3.0))
        # Never below the hardware floor.
        assert _execute(label, net=0.3, discharged=3.0, stored=1.5) == pytest.approx(
            (0.0, 1.0)
        )

    @pytest.mark.parametrize(
        "label",
        [
            Recommendations.BatteriesDischargeMode.value,
            Recommendations.BatteriesDischargeWindowMode.value,
        ],
    )
    def test_self_consumption_follows_the_load(self, label: str) -> None:
        assert _execute(label, net=0.8) == pytest.approx((0.0, 0.8))
        assert _execute(label, net=-1.5) == pytest.approx((1.5, 0.0))

    def test_the_battery_does_not_serve_the_ev(self) -> None:
        """3 kWh of site load, of which the house's own deficit is 0.4."""
        label = Recommendations.BatteriesDischargeMode.value

        assert _execute(label, net=3.0, house=0.4) == pytest.approx((0.0, 0.4))

    def test_an_ev_eating_the_pv_surplus_leaves_nothing_to_discharge_for(
        self,
    ) -> None:
        """The house has a surplus; only the EV makes the site short."""
        label = Recommendations.BatteriesDischargeMode.value

        assert _execute(label, net=2.0, house=-1.0) == pytest.approx((0.0, 0.0))

    @pytest.mark.parametrize(
        "label",
        [
            Recommendations.BatteriesChargeSolar.value,
            Recommendations.BatteriesWaitMode.value,
        ],
    )
    def test_held_modes_take_a_surplus_and_never_discharge(self, label: str) -> None:
        assert _execute(label, net=-1.2) == pytest.approx((1.2, 0.0))
        assert _execute(label, net=0.9) == pytest.approx((0.0, 0.0))

    def test_ev_charging_serves_the_house_up_to_the_planned_discharge(self) -> None:
        label = Recommendations.EVSmartCharging.value

        assert _execute(label, net=4.0, house=0.9, discharged=0.5) == pytest.approx(
            (0.0, 0.5)
        )
        assert _execute(label, net=4.0, house=0.9, discharged=0.0) == pytest.approx(
            (0.0, 0.0)
        )
        assert _execute(label, net=-0.7) == pytest.approx((0.7, 0.0))

    @pytest.mark.parametrize(
        "label",
        [
            None,
            Recommendations.TimePassed.value,
            Recommendations.MissingInputEntities.value,
        ],
    )
    def test_no_decision_leaves_the_battery_idle(self, label: str | None) -> None:
        assert _execute(label, net=1.0) == pytest.approx((0.0, 0.0))
        assert _execute(label, net=-1.0) == pytest.approx((0.0, 0.0))

"""Tests for the day scoring of the planner backtest (issue #1208).

Two kinds of test.  The committed actuals are scored as they are: the numbers
the documentation quotes must come out of the repository with no live system,
and the oracle must be a lower bound on every committed day.  Synthetic days
then pin what each number means, using realized flows built from a known run.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.hindsight_oracle import (
    HindsightRun,
    HindsightSlot,
    simulate_self_consumption,
    solve_hindsight_oracle,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils.datetime_utils import slot_key
from tests.backtest.actuals import Actuals, load_actuals
from tests.backtest.conftest import CORPUS_DIR
from tests.backtest.harvest import committed_actuals, newest_cycle_of_site
from tests.backtest.replay import load_planner_input
from tests.backtest.scoring import (
    MIN_POTENTIAL,
    DayScore,
    SiteLimits,
    day_slot_keys,
    describe,
    merge_actuals,
    observed_days,
    score_day,
    score_days,
    summarize,
)

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

ACTUALS_DIR = Path(__file__).parent / "actuals"
_ZONE = ZoneInfo("Europe/Copenhagen")
_DAY = date(2026, 6, 10)
_SITE = SiteLimits(
    rated_kwh=10.0,
    min_soc_pct=5.0,
    max_soc_pct=100.0,
    max_charge_kw=5.0,
    max_discharge_kw=5.0,
    charge_efficiency=0.97,
    discharge_efficiency=0.97,
)
_START_SOC_PCT = 40.0


# ---------------------------------------------------------------------------
# The committed days
# ---------------------------------------------------------------------------


def _committed_site() -> SiteLimits:
    """Return the limits of the installation the committed actuals describe."""
    tags = {load_actuals(path).site_tag for path in ACTUALS_DIR.glob("actuals-*.json")}
    assert len(tags) == 1, tags
    cycle = newest_cycle_of_site(CORPUS_DIR, next(iter(tags)))
    assert cycle is not None
    planner_input, _ = load_planner_input(cycle)
    return SiteLimits.from_planner_input(planner_input)


def _committed_scores() -> list[DayScore]:
    paths = sorted(ACTUALS_DIR.glob("actuals-*.json"))
    actuals = merge_actuals([load_actuals(path) for path in paths])
    return score_days(
        actuals, committed_actuals(paths).keys(), _ZONE, _committed_site()
    )


@pytest.fixture(scope="module")
def committed() -> list[DayScore]:
    return _committed_scores()


class TestCommittedDays:
    """Every number is reproducible from the repository, with no live system."""

    def test_every_committed_day_is_scored(self, committed: list[DayScore]) -> None:
        assert committed
        assert [score.unscorable for score in committed] == [None] * len(committed)

    def test_the_oracle_is_a_lower_bound_on_the_realized_cost(
        self, committed: list[DayScore]
    ) -> None:
        for score in committed:
            assert score.regret is not None
            assert score.regret >= -1e-6, score.day

    def test_the_potential_is_never_negative(self, committed: list[DayScore]) -> None:
        for score in committed:
            assert score.potential is not None
            assert score.potential >= -1e-6, score.day

    def test_realized_cost_is_import_paid_minus_export_earned(
        self, committed: list[DayScore]
    ) -> None:
        """Recomputed straight from the committed file, without the scorer."""
        fee = _committed_site().export_fee_per_kwh
        for score in committed:
            raw = json.loads(
                (ACTUALS_DIR / f"actuals-{score.day}.json").read_text(encoding="utf-8")
            )
            energy, values = raw["slot_energy_kwh"], raw["slot_values"]
            imported = dict(map(tuple, energy["grid_import"]))
            exported = dict(map(tuple, energy["grid_export"]))
            import_price = dict(map(tuple, values["import_price"]))
            export_price = dict(map(tuple, values["export_price"]))
            expected = sum(
                imported[at] * import_price[at]
                - exported[at] * (export_price[at] - fee)
                for at in imported
            )

            assert score.realized_cost == pytest.approx(expected)

    def test_documented_numbers_are_reproduced(self, committed: list[DayScore]) -> None:
        """The table in docs/backtest-harness.md."""
        by_day = {score.day: score for score in committed}

        september_15 = by_day[date(2026, 9, 15)]
        assert september_15.realized_cost == pytest.approx(13.45, abs=0.01)
        assert september_15.oracle_cost == pytest.approx(10.19, abs=0.01)
        assert september_15.potential == pytest.approx(9.32, abs=0.01)
        assert september_15.capture == pytest.approx(0.650, abs=0.001)

        september_26 = by_day[date(2026, 9, 26)]
        assert september_26.realized_cost == pytest.approx(84.19, abs=0.01)
        assert september_26.oracle_cost == pytest.approx(64.68, abs=0.01)
        assert september_26.potential == pytest.approx(15.40, abs=0.01)
        # Worse than plain self-consumption that day, and reported as such.
        assert september_26.capture == pytest.approx(-0.267, abs=0.001)

    def test_scoring_is_deterministic(self, committed: list[DayScore]) -> None:
        assert _committed_scores() == committed


# ---------------------------------------------------------------------------
# Synthetic days
# ---------------------------------------------------------------------------


def _hindsight_slots(
    prices: list[tuple[float, float]] | None = None,
) -> list[HindsightSlot]:
    """Return a 15-minute day: night load, midday PV, an evening price peak."""
    slots = []
    for index in range(96):
        hour = index / 4.0
        pv = max(0.0, 0.9 - abs(hour - 13.0) * 0.2)
        load = 0.15 + 0.25 * (17 <= hour < 22)
        imp = 1.0 + 1.2 * (17 <= hour < 21) - 0.5 * (1 <= hour < 5)
        exp = imp - 0.6
        if prices is not None:
            imp, exp = prices[index]
        slots.append(HindsightSlot(0.25, load - pv, imp, exp, curtailable_kwh=pv))
    return slots


def _actuals_from(
    slots: list[HindsightSlot],
    run: HindsightRun,
    *,
    day: date = _DAY,
    start_soc_pct: float = _START_SOC_PCT,
) -> Actuals:
    """Return actuals in which the battery did exactly what *run* did."""
    keys = day_slot_keys(day, _ZONE, 15)
    assert len(keys) == len(slots)
    energy: dict[str, dict[datetime, float]] = {
        "grid_import": dict(zip(keys, run.grid_import_kwh)),
        "grid_export": dict(zip(keys, run.grid_export_kwh)),
        "battery_charged": dict(zip(keys, run.charged_kwh)),
        "battery_discharged": dict(zip(keys, run.discharged_kwh)),
        "pv_produced": {key: slot.curtailable_kwh for key, slot in zip(keys, slots)},
    }
    values: dict[str, dict[datetime, float]] = {
        "import_price": {key: slot.import_price for key, slot in zip(keys, slots)},
        "export_price": {key: slot.export_price for key, slot in zip(keys, slots)},
        "battery_soc_pct": {keys[0]: start_soc_pct},
    }
    return Actuals(slot_minutes=15, energy_kwh=energy, values=values)


def _start_kwh(soc_pct: float = _START_SOC_PCT) -> float:
    return _SITE.rated_kwh * soc_pct / 100.0


def _self_consumption_day() -> tuple[list[HindsightSlot], HindsightRun]:
    slots = _hindsight_slots()
    return slots, simulate_self_consumption(slots, _SITE.battery(), _start_kwh())


def _oracle_day() -> tuple[list[HindsightSlot], HindsightRun]:
    slots = _hindsight_slots()
    run = solve_hindsight_oracle(slots, _SITE.battery(), _start_kwh(), _start_kwh())
    assert run is not None
    return slots, run


class TestWhatTheNumbersMean:
    def test_a_day_run_like_the_oracle_captures_everything(self) -> None:
        slots, run = _oracle_day()

        score = score_day(_actuals_from(slots, run), _DAY, _ZONE, _SITE)

        assert score.is_scored
        assert score.realized_cost == pytest.approx(run.cost)
        assert score.regret == pytest.approx(0.0, abs=1e-6)
        assert score.potential is not None and score.potential > 1.0
        assert score.savings == pytest.approx(score.potential, abs=1e-6)
        assert score.capture == pytest.approx(1.0, abs=1e-6)

    def test_a_day_of_plain_self_consumption_captures_nothing(self) -> None:
        slots, run = _self_consumption_day()

        score = score_day(_actuals_from(slots, run), _DAY, _ZONE, _SITE)

        assert score.realized_cost == pytest.approx(run.cost)
        assert score.baseline_cost == pytest.approx(run.cost)
        assert score.regret == pytest.approx(score.potential, abs=1e-6)
        assert score.savings == pytest.approx(0.0, abs=1e-6)
        assert score.capture == pytest.approx(0.0, abs=1e-6)

    def test_a_day_worse_than_self_consumption_has_negative_capture(self) -> None:
        """Holding the battery idle all day loses to the inverter on its own."""
        slots = _hindsight_slots()
        nets = [slot.net_load_kwh for slot in slots]
        idle = HindsightRun(
            cost=0.0,
            grid_import_kwh=tuple(max(net, 0.0) for net in nets),
            grid_export_kwh=tuple(max(-net, 0.0) for net in nets),
            charged_kwh=(0.0,) * len(slots),
            discharged_kwh=(0.0,) * len(slots),
            stored_kwh=(_start_kwh(),) * len(slots),
        )

        score = score_day(_actuals_from(slots, idle), _DAY, _ZONE, _SITE)

        assert score.savings is not None and score.savings < 0.0
        assert score.capture is not None and score.capture < 0.0

    def test_ending_the_day_fuller_is_not_counted_as_a_loss(self) -> None:
        """A battery charged for tomorrow is not punished for it.

        The oracle has to end at least as full as the day really did, so the
        cost of the stored energy is in both.
        """
        slots = _hindsight_slots()
        full = solve_hindsight_oracle(slots, _SITE.battery(), _start_kwh(), 10.0)
        assert full is not None

        score = score_day(_actuals_from(slots, full), _DAY, _ZONE, _SITE)

        assert score.realized_end_kwh == pytest.approx(10.0, abs=1e-6)
        assert score.regret == pytest.approx(0.0, abs=1e-6)
        assert score.capture == pytest.approx(1.0, abs=1e-6)

    def test_capture_is_unknown_when_nothing_could_be_saved(self) -> None:
        """Flat prices, no PV: no control beats self-consumption."""
        flat = [
            HindsightSlot(0.25, 0.2, 1.0, 0.4, curtailable_kwh=0.0) for _ in range(96)
        ]
        run = simulate_self_consumption(flat, _SITE.battery(), _start_kwh())

        score = score_day(_actuals_from(flat, run), _DAY, _ZONE, _SITE)

        assert score.is_scored
        assert score.potential is not None and score.potential < MIN_POTENTIAL
        assert score.capture is None
        assert "unknown" in describe([score])

    def test_the_export_fee_is_subtracted_from_the_export_price(self) -> None:
        slots, run = _self_consumption_day()
        actuals = _actuals_from(slots, run)
        with_fee = SiteLimits(**{**_SITE.__dict__, "export_fee_per_kwh": 0.1})

        plain = score_day(actuals, _DAY, _ZONE, _SITE)
        charged = score_day(actuals, _DAY, _ZONE, with_fee)

        assert plain.realized_cost is not None and charged.realized_cost is not None
        assert charged.realized_cost - plain.realized_cost == pytest.approx(
            0.1 * sum(run.grid_export_kwh)
        )

    def test_the_ev_is_part_of_the_fixed_load(self) -> None:
        """Load the house meter does not see still reaches the oracle."""
        slots, run = _self_consumption_day()
        actuals = _actuals_from(slots, run)
        keys = day_slot_keys(_DAY, _ZONE, 15)
        for key in keys[8:16]:  # 02:00-04:00: an EV on the grid
            actuals.energy_kwh["grid_import"][key] += 2.0

        score = score_day(actuals, _DAY, _ZONE, _SITE)
        without = score_day(_actuals_from(slots, run), _DAY, _ZONE, _SITE)

        assert score.realized_cost is not None and without.realized_cost is not None
        assert score.oracle_cost is not None and without.oracle_cost is not None
        # 16 kWh more load: every run pays for it, the oracle included.
        assert score.realized_cost > without.realized_cost + 5.0
        assert score.oracle_cost > without.oracle_cost + 5.0


class TestMissingIsNotZero:
    def test_a_missing_slot_makes_the_day_unscorable(self) -> None:
        slots, run = _self_consumption_day()
        actuals = _actuals_from(slots, run)
        del actuals.energy_kwh["grid_import"][day_slot_keys(_DAY, _ZONE, 15)[40]]

        score = score_day(actuals, _DAY, _ZONE, _SITE)

        assert not score.is_scored
        assert score.unscorable == "grid_import missing in 1/96 slot(s)"
        assert score.realized_cost is None
        assert score.capture is None
        assert score.savings is None
        assert score.regret is None

    @pytest.mark.parametrize(
        ("bucket", "series"),
        [
            ("energy_kwh", "battery_charged"),
            ("energy_kwh", "battery_discharged"),
            ("energy_kwh", "grid_export"),
            ("values", "import_price"),
            ("values", "export_price"),
        ],
    )
    def test_every_required_series_is_required(self, bucket: str, series: str) -> None:
        slots, run = _self_consumption_day()
        actuals = _actuals_from(slots, run)
        del getattr(actuals, bucket)[series]

        score = score_day(actuals, _DAY, _ZONE, _SITE)

        assert score.unscorable == f"{series} missing in 96/96 slot(s)"

    def test_the_starting_soc_is_required(self) -> None:
        slots, run = _self_consumption_day()
        actuals = _actuals_from(slots, run)
        actuals.values["battery_soc_pct"].clear()

        score = score_day(actuals, _DAY, _ZONE, _SITE)

        assert score.unscorable == "battery_soc_pct missing at the start of the day"

    def test_pv_is_optional(self) -> None:
        """Without it the oracle just cannot curtail."""
        slots, run = _self_consumption_day()
        actuals = _actuals_from(slots, run)
        del actuals.energy_kwh["pv_produced"]

        assert score_day(actuals, _DAY, _ZONE, _SITE).is_scored

    def test_an_unscored_day_is_counted_not_summed(self) -> None:
        slots, run = _self_consumption_day()
        good = score_day(_actuals_from(slots, run), _DAY, _ZONE, _SITE)
        bad = DayScore(day=_DAY + timedelta(days=1), unscorable="no data")

        total = summarize([good, bad])

        assert (total.scored_days, total.unscored_days) == (1, 1)
        assert total.realized_cost == pytest.approx(good.realized_cost)
        assert "1 day(s) not scored" in describe([good, bad])
        assert "not scored: no data" in describe([good, bad])

    def test_a_period_without_a_scored_day_has_no_capture(self) -> None:
        total = summarize([DayScore(day=_DAY, unscorable="no data")])

        assert total.scored_days == 0
        assert total.capture is None
        assert total.carried_baseline_end_delta_kwh is None


class TestRealizedDayOutsideTheConfiguredLimits:
    """The realized day stays feasible for the oracle, and the score says so."""

    def test_flows_beyond_the_capacity_widen_it_and_are_noted(self) -> None:
        slots, run = _oracle_day()
        small = SiteLimits(**{**_SITE.__dict__, "rated_kwh": 6.0})

        score = score_day(
            _actuals_from(slots, run, start_soc_pct=_START_SOC_PCT / 0.6),
            _DAY,
            _ZONE,
            small,
        )

        assert score.is_scored
        assert score.regret is not None and score.regret >= -1e-6
        assert any("leave the configured capacity" in note for note in score.notes)
        assert "note: measured battery flows" in describe([score])

    def test_power_beyond_the_limit_is_allowed_and_noted(self) -> None:
        slots, run = _oracle_day()
        weak = SiteLimits(
            **{**_SITE.__dict__, "max_charge_kw": 1.0, "max_discharge_kw": 1.0}
        )

        score = score_day(_actuals_from(slots, run), _DAY, _ZONE, weak)

        assert score.is_scored
        assert score.regret is not None and score.regret >= -1e-6
        assert any("battery power exceeds" in note for note in score.notes)

    def test_grid_limits_never_cut_below_what_was_measured(self) -> None:
        slots, run = _oracle_day()
        fused = SiteLimits(
            **{**_SITE.__dict__, "max_grid_import_kw": 0.5, "max_grid_export_kw": 0.5}
        )

        score = score_day(_actuals_from(slots, run), _DAY, _ZONE, fused)

        assert score.is_scored
        assert score.regret is not None and score.regret >= -1e-6


class TestDaysAndSlots:
    def test_an_ordinary_day_has_96_quarter_hours(self) -> None:
        keys = day_slot_keys(_DAY, _ZONE, 15)

        assert len(keys) == 96
        assert keys[0] == slot_key(datetime(2026, 6, 10, 0, 0, tzinfo=_ZONE), 15)

    def test_dst_days_have_92_and_100(self) -> None:
        assert len(day_slot_keys(date(2026, 3, 29), _ZONE, 15)) == 92
        fall_back = day_slot_keys(date(2026, 10, 25), _ZONE, 15)
        assert len(fall_back) == 100
        assert len(set(fall_back)) == 100

    def test_observed_days_are_local_days(self) -> None:
        slots, run = _self_consumption_day()

        assert observed_days(_actuals_from(slots, run), _ZONE) == [_DAY]

    def test_a_day_without_slots_is_unscorable(self) -> None:
        actuals = Actuals(slot_minutes=60 * 48)

        assert (
            score_day(actuals, _DAY, _ZONE, _SITE).unscorable == "the day has no slots"
        )


class TestCarriedBaseline:
    """Over consecutive days the baseline is one battery, not one per day."""

    @staticmethod
    def _two_days(gap_days: int = 1) -> tuple[Actuals, list[date]]:
        slots = _hindsight_slots()
        days = [_DAY, _DAY + timedelta(days=gap_days)]
        full = solve_hindsight_oracle(slots, _SITE.battery(), _start_kwh(), 10.0)
        assert full is not None
        first = _actuals_from(slots, full, day=days[0])
        second_run = simulate_self_consumption(slots, _SITE.battery(), 10.0)
        second = _actuals_from(slots, second_run, day=days[1], start_soc_pct=100.0)
        return merge_actuals([first, second]), days

    def test_the_baseline_starts_the_next_day_where_it_ended(self) -> None:
        actuals, days = self._two_days()

        first, second = score_days(actuals, days, _ZONE, _SITE)

        assert first.carried_baseline_cost == pytest.approx(first.baseline_cost)
        assert second.baseline_cost is not None
        # The real battery started day two full; the baseline did not.
        assert first.carried_baseline_end_kwh is not None
        assert first.carried_baseline_end_kwh < 10.0 - 1.0
        assert second.carried_baseline_cost is not None
        assert second.carried_baseline_cost > second.baseline_cost

    def test_a_gap_restarts_the_baseline_from_the_real_battery(self) -> None:
        actuals, days = self._two_days(gap_days=2)

        _first, second = score_days(actuals, days, _ZONE, _SITE)

        assert second.carried_baseline_cost == pytest.approx(second.baseline_cost)

    def test_an_unscored_day_restarts_it_too(self) -> None:
        actuals, days = self._two_days()
        del actuals.energy_kwh["grid_export"][day_slot_keys(days[0], _ZONE, 15)[3]]

        first, second = score_days(actuals, days, _ZONE, _SITE)

        assert not first.is_scored
        assert second.carried_baseline_cost == pytest.approx(second.baseline_cost)

    def test_the_summary_reports_what_the_baseline_ends_with(self) -> None:
        actuals, days = self._two_days()
        scores = score_days(actuals, days, _ZONE, _SITE)

        total = summarize(scores)

        assert total.scored_days == 2
        assert total.carried_baseline_cost == pytest.approx(
            sum(score.carried_baseline_cost or 0.0 for score in scores)
        )
        assert total.carried_savings == pytest.approx(
            total.carried_baseline_cost - total.realized_cost
        )
        assert total.savings == pytest.approx(total.potential - total.regret)
        assert total.capture == pytest.approx(total.savings / total.potential)
        assert total.carried_baseline_end_delta_kwh == pytest.approx(
            (scores[-1].carried_baseline_end_kwh or 0.0)
            - (scores[-1].realized_end_kwh or 0.0)
        )
        text = describe(scores)
        assert "2 day(s)" in text
        assert "self-consumption as one battery" in text


class TestMergeActuals:
    def test_parts_must_share_a_slot_width(self) -> None:
        with pytest.raises(ValueError, match="slot width"):
            merge_actuals([Actuals(slot_minutes=15), Actuals(slot_minutes=60)])

    def test_nothing_to_merge_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="no actuals"):
            merge_actuals([])

    def test_parts_must_be_from_one_installation(self) -> None:
        """Issue #1225: two houses' meters are never merged into one day."""
        with pytest.raises(ValueError, match="different installations"):
            merge_actuals(
                [
                    Actuals(slot_minutes=15, site_tag="site-a"),
                    Actuals(slot_minutes=15, site_tag="site-b"),
                ]
            )

    def test_tagged_and_untagged_parts_do_not_merge(self) -> None:
        with pytest.raises(ValueError, match="no site tag"):
            merge_actuals(
                [Actuals(slot_minutes=15, site_tag="site-a"), Actuals(slot_minutes=15)]
            )

    def test_the_merge_keeps_the_site_tag(self) -> None:
        merged = merge_actuals(
            [
                Actuals(slot_minutes=15, site_tag="site-a"),
                Actuals(slot_minutes=15, site_tag="site-a"),
            ]
        )
        assert merged.site_tag == "site-a"
        assert merge_actuals([Actuals(slot_minutes=15)]).site_tag is None


class TestDefaultSiteDump:
    """Issue #1225: the limits come from the installation of the actuals."""

    def test_the_newest_cycle_of_the_installation_is_chosen(self) -> None:
        home = newest_cycle_of_site(CORPUS_DIR, "site-a")
        report = newest_cycle_of_site(CORPUS_DIR, "site-b")
        assert home is not None
        assert report is not None
        assert home.name == sorted(CORPUS_DIR.glob("cycle-2026-09-2*.json"))[-1].name
        assert report.name == "cycle-2026-09-14-1721.json"
        assert load_planner_input(home)[0].battery_rated_capacity_kwh == 15.0
        assert load_planner_input(report)[0].battery_rated_capacity_kwh == 10.0

    @pytest.mark.parametrize("tag", [None, "site-unknown"])
    def test_no_cycle_of_an_unknown_installation(self, tag: str | None) -> None:
        assert newest_cycle_of_site(CORPUS_DIR, tag) is None


class TestSiteLimits:
    def test_hard_limits_are_read_from_a_planner_input(self) -> None:
        site = SiteLimits.from_planner_input(
            PlannerInput(
                battery_rated_capacity_kwh=15.0,
                battery_end_of_discharge_soc_pct=5.0,
                battery_max_soc_pct=95.0,
                battery_max_charge_power_w=4000.0,
                battery_max_discharge_power_w=3000.0,
                battery_charge_efficiency_pct=98.0,
                battery_discharge_efficiency_pct=96.0,
                export_fee_per_kwh=0.07,
                main_fuse_amps=25.0,
                main_fuse_phases=3,
                max_grid_export_power_kw=6.0,
            )
        )

        assert site.battery().min_stored_kwh == pytest.approx(0.75)
        assert site.battery().max_stored_kwh == pytest.approx(14.25)
        assert (site.max_charge_kw, site.max_discharge_kw) == pytest.approx((4.0, 3.0))
        assert site.charge_efficiency == pytest.approx(0.98)
        assert site.discharge_efficiency == pytest.approx(0.96)
        assert site.export_fee_per_kwh == pytest.approx(0.07)
        assert site.max_grid_import_kw == pytest.approx(25.0 * 230.0 * 3 / 1000.0)
        assert site.max_grid_export_kw == pytest.approx(6.0)

    def test_missing_limits_stay_unlimited(self) -> None:
        site = SiteLimits.from_planner_input(
            PlannerInput(
                battery_max_charge_power_w=5000.0, battery_max_discharge_power_w=None
            )
        )

        assert site.max_discharge_kw == pytest.approx(5.0)
        assert site.max_grid_import_kw is None
        assert site.max_grid_export_kw is None

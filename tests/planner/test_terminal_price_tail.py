"""Estimated price tail behind the terminal end value (issue #1453).

The energy a plan leaves in the battery is valued at one end value ``V``.
With the tail enabled, half of ``V`` comes from price-profile prices for the
24 hours after the horizon.  These tests pin what the spec promises:

- the tail changes ``V`` and nothing else: no slot, no slot price;
- without a profile, or with weight 0, the plan is the plan without a tail;
- the level shift follows the rule of the missing-price fill (24 hours);
- the plan and the explanation say the prices are estimated;
- the planner invariants hold with the blended ``V``.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES
from custom_components.hsem.coordinator_builder import (
    build_planner_input,
    generate_recommendation_intervals,
)
from custom_components.hsem.custom_sensors.config_reader import build_sensor_config
from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    populate_price_and_solcast_from_snapshot,
)
from custom_components.hsem.flows.battery_economics import (
    get_battery_economics_step_schema,
    validate_battery_economics_input,
)
from custom_components.hsem.models.forecast_coverage import ForecastCoverage
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.price_profile import PriceProfile
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.planner.cost_helpers import (
    terminal_end_value,
    terminal_soc_value,
)
from custom_components.hsem.planner.engine_core import run_planner
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.planner.terminal_tail import (
    TERMINAL_TAIL_CONSTRAINT,
    TERMINAL_TAIL_HOURS,
    TERMINAL_TAIL_WEIGHT,
    TerminalPriceTail,
    flag_terminal_price_tail,
    resolve_terminal_price_tail,
    tail_share,
    terminal_price_tail,
)
from custom_components.hsem.utils.prices import SlotPrice
from tests.backtest.conftest import corpus_paths
from tests.backtest.invariants import check_invariants, format_violations
from tests.backtest.replay import load_planner_input

_TZ = ZoneInfo("Europe/Copenhagen")
#: A Monday.  The horizon below starts here.
_MONDAY = datetime(2026, 6, 1, tzinfo=_TZ)
_ETA = 0.98
_CYCLE_COST = 0.1
_LAST_DAY_VALUE = 1.0
#: Profile shape: a cheap night, a flat day and an evening peak.
_NIGHT, _DAY, _PEAK = 1.0, 2.0, 4.0
_PEAK_HOURS = range(17, 21)

_needs_scipy = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available"
)


def _shape(hour: int) -> float:
    """Return the profile's import price for a clock hour."""
    if hour < 6:
        return _NIGHT
    return _PEAK if hour in _PEAK_HOURS else _DAY


def _profile(hours: range = range(24), **levels: Any) -> PriceProfile:
    """Return an every-weekday profile of :func:`_shape` for *hours*."""
    return PriceProfile(
        any_day_hours={hour: (_shape(hour), _shape(hour) - 0.5) for hour in hours},
        **levels,
    )


def _slots(
    hours: int, offset: float = 0.0, *, step: int = 60, start: datetime = _MONDAY
) -> list[PlannedSlot]:
    """Return *hours* of slots from *start*, priced at the shape plus *offset*.

    Slots are stepped in real time, so a DST day keeps its 23 or 25 hours.
    """
    slots = []
    for index in range(hours * 60 // step):
        begin = (start.astimezone(UTC) + timedelta(minutes=step * index)).astimezone(
            _TZ
        )
        slots.append(
            PlannedSlot(
                start=begin,
                end=(begin.astimezone(UTC) + timedelta(minutes=step)).astimezone(_TZ),
                price=SlotPrice(
                    import_price=_shape(begin.hour) + offset, export_price=0.0
                ),
            )
        )
    return slots


def _tail(
    slots: list[PlannedSlot],
    published: list[bool],
    profile: PriceProfile | None,
    *,
    weight: float = 0.5,
    last_day_value: float | None = _LAST_DAY_VALUE,
    step: int = 60,
    published_until: datetime | None = None,
    top_n: int = 4,
) -> TerminalPriceTail | None:
    return terminal_price_tail(
        slots,
        published,
        profile,
        step,
        _TZ,
        last_day_value=last_day_value,
        weight=weight,
        published_until=published_until,
        top_n=top_n,
        charge_eff=_ETA,
        discharge_eff=_ETA,
        cycle_cost_per_kwh=_CYCLE_COST,
    )


def _value(shift: float) -> float:
    """Return the tail's own end value with every tail price moved by *shift*."""
    return terminal_end_value(
        peak_import=_PEAK + shift,
        night_import=_NIGHT + shift,
        charge_eff=_ETA,
        discharge_eff=_ETA,
        cycle_cost_per_kwh=_CYCLE_COST,
    )


class TestNoTail:
    """Every case that must leave the end value alone."""

    @pytest.mark.parametrize("weight", [0.0, -1.0, math.nan, math.inf, -math.inf])
    def test_a_weight_that_is_zero_or_not_a_share_disables_it(
        self, weight: float
    ) -> None:
        assert _tail(_slots(24), [True] * 24, _profile(), weight=weight) is None

    def test_no_profile(self) -> None:
        assert _tail(_slots(24), [True] * 24, None) is None

    def test_an_empty_profile(self) -> None:
        assert _tail(_slots(24), [True] * 24, PriceProfile()) is None

    def test_no_last_day_value(self) -> None:
        assert _tail(_slots(24), [True] * 24, _profile(), last_day_value=None) is None

    def test_no_slots(self) -> None:
        assert _tail([], [], _profile()) is None

    def test_a_profile_that_misses_a_tail_hour(self) -> None:
        """A tail with a hole is not priced at all, never partly."""
        assert _tail(_slots(24), [True] * 24, _profile(range(23))) is None


class TestTailShare:
    """The weight is a share between 0 and 1, and never NaN."""

    @pytest.mark.parametrize(
        ("weight", "expected"),
        [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0), (3.0, 1.0), (-2.0, 0.0)],
    )
    def test_clamped(self, weight: float, expected: float) -> None:
        assert tail_share(weight) == pytest.approx(expected)

    @pytest.mark.parametrize("weight", [math.nan, math.inf, -math.inf])
    def test_not_finite_is_off(self, weight: float) -> None:
        assert tail_share(weight) == pytest.approx(0.0)


class TestPublishedPricesAreNeverEstimated:
    """A horizon that ends before the last published price gets no tail.

    The horizon starts at local midnight.  Day-ahead prices cover today and,
    from about 13:00, tomorrow, so a 12, 24 or 36 hour horizon can end
    before prices that are already published.
    """

    _END_OF_MONDAY = _MONDAY + timedelta(days=1)
    _END_OF_TUESDAY = _MONDAY + timedelta(days=2)

    @pytest.mark.parametrize(
        ("hours", "published_days", "expected"),
        [
            # Tomorrow not published yet.
            (12, 1, None),  # ends at noon, today is published to midnight
            (24, 1, True),  # ends where the published prices end
            (36, 1, None),  # the shift would end inside the tail
            (48, 1, False),  # tail starts 24 h after the last price
            # Tomorrow published.
            (12, 2, None),
            (24, 2, None),  # the tail would be tomorrow, which is published
            (36, 2, None),
            (48, 2, True),
        ],
    )
    def test_horizons(
        self, hours: int, published_days: int, expected: bool | None
    ) -> None:
        """``expected``: no tail, a shifted tail or a tail as recorded."""
        until = _MONDAY + timedelta(days=published_days)
        priced = [slot.end <= until for slot in _slots(hours)]

        tail = _tail(
            _slots(hours, offset=0.7), priced, _profile(), published_until=until
        )

        if expected is None:
            assert tail is None
            return
        assert tail is not None
        assert tail.start == _MONDAY + timedelta(hours=hours)
        assert tail.start >= until
        assert tail.shifted is expected
        assert tail.tail_value_per_kwh == pytest.approx(
            _value(0.7 if expected else 0.0)
        )

    def test_a_flat_price_is_published_for_ever(self) -> None:
        """The populator reports ``datetime.max`` for a flat price: no tail."""
        forever = datetime.max.replace(tzinfo=UTC)

        assert (
            _tail(_slots(48), [True] * 48, _profile(), published_until=forever) is None
        )

    def test_unknown_reach_falls_back_to_the_priced_slots(self) -> None:
        """Without the populator's time the last priced slot is the anchor."""
        tail = _tail(_slots(24, offset=0.7), [True] * 24, _profile())

        assert tail is not None
        assert tail.shifted is True


class TestTailPrices:
    """The profile gives the shape; one level applies to the whole tail."""

    def test_tail_is_the_24_hours_after_the_horizon(self) -> None:
        tail = _tail(_slots(24), [True] * 24, _profile())

        assert tail is not None
        assert tail.start == _MONDAY + timedelta(days=1)
        assert tail.end == tail.start + timedelta(hours=TERMINAL_TAIL_HOURS)
        assert tail.slot_count == 24

    def test_published_prices_set_the_level(self) -> None:
        """Published prices 0.7 above the profile lift the whole tail by 0.7."""
        tail = _tail(_slots(24, offset=0.7), [True] * 24, _profile())

        assert tail is not None
        assert tail.shifted is True
        assert tail.peak_import == pytest.approx(_PEAK + 0.7)
        assert tail.night_import == pytest.approx(_NIGHT + 0.7)
        assert tail.tail_value_per_kwh == pytest.approx(_value(0.7))

    def test_a_tail_a_day_after_the_last_price_is_not_shifted(self) -> None:
        """48 h horizon, tomorrow unpublished: the tail starts 24 h too late."""
        published = [True] * 24 + [False] * 24
        tail = _tail(_slots(48, offset=0.7), published, _profile())

        assert tail is not None
        assert tail.shifted is False
        assert tail.tail_value_per_kwh == pytest.approx(_value(0.0))

    def test_a_tail_is_never_priced_at_two_levels(self) -> None:
        """36 h horizon, 24 h published: the shift would end at tail hour 12.

        Tuesday's evening peak would be shifted and Wednesday's night not,
        which moves the spread between them by the shift.  No tail.
        """
        published = [True] * 24 + [False] * 12

        assert _tail(_slots(36, offset=0.7), published, _profile()) is None

    def test_outage_uses_the_last_known_shift_while_it_is_valid(self) -> None:
        """No priced slot: the stored shift covers the whole tail."""
        until = _MONDAY + timedelta(days=2)
        tail = _tail(_slots(24), [False] * 24, _profile(import_level=(0.3, until)))

        assert tail is not None
        assert tail.shifted is True
        assert tail.night_import == pytest.approx(_NIGHT + 0.3)
        assert tail.peak_import == pytest.approx(_PEAK + 0.3)

    def test_outage_with_a_shift_that_ends_inside_the_tail_gives_no_tail(self) -> None:
        until = _MONDAY + timedelta(days=1, hours=12)
        profile = _profile(import_level=(0.3, until))

        assert _tail(_slots(24), [False] * 24, profile) is None

    def test_outage_with_a_shift_that_is_over_uses_the_profile_as_recorded(
        self,
    ) -> None:
        until = _MONDAY + timedelta(days=1)
        tail = _tail(_slots(24), [False] * 24, _profile(import_level=(0.3, until)))

        assert tail is not None
        assert tail.shifted is False
        assert tail.tail_value_per_kwh == pytest.approx(_value(0.0))

    def test_outage_without_a_stored_shift_uses_the_profile_as_recorded(self) -> None:
        tail = _tail(_slots(24, offset=0.7), [False] * 24, _profile())

        assert tail is not None
        assert tail.shifted is False
        assert tail.tail_value_per_kwh == pytest.approx(_value(0.0))

    def test_too_few_published_slots_give_no_shift(self) -> None:
        """Three published hours are below the 240 minutes a shift needs."""
        published = [True] * 3 + [False] * 21
        tail = _tail(_slots(24, offset=0.7), published, _profile())

        assert tail is not None
        assert tail.shifted is False


class TestQuarterHourSlots:
    """15-minute slots: the tail is priced per quarter."""

    @staticmethod
    def _quarter_profile() -> PriceProfile:
        """The hourly shape, with the :45 quarter of 18:00 at 8.0."""
        quarters = {
            (hour, minute): (_shape(hour), 0.0)
            for hour in range(24)
            for minute in (0, 15, 30, 45)
        }
        quarters[(18, 45)] = (8.0, 0.0)
        return PriceProfile(any_day_quarters=quarters)

    def test_tail_has_96_slots(self) -> None:
        slots = _slots(24, step=15)
        tail = _tail(slots, [True] * 96, self._quarter_profile(), step=15)

        assert tail is not None
        assert tail.slot_count == 96
        assert tail.start == _MONDAY + timedelta(days=1)
        assert tail.end == _MONDAY + timedelta(days=2)

    def test_peak_reads_single_quarters(self) -> None:
        """The dearest quarter counts once, not as a whole hour."""
        slots = _slots(24, step=15)
        tail = _tail(slots, [True] * 96, self._quarter_profile(), step=15, top_n=2)

        assert tail is not None
        # The horizon's own 18:45 is 4.0, the profile's 8.0: one of 96 slots,
        # so the median shift stays 0.
        assert tail.shifted is True
        assert tail.peak_import == pytest.approx((8.0 + _PEAK) / 2)
        assert tail.night_import == pytest.approx(_NIGHT)

    def test_the_shift_needs_240_minutes_of_quarters(self) -> None:
        """15 priced quarters are 225 minutes: no shift.  16 are enough."""
        slots = _slots(24, offset=0.7, step=15)
        profile = self._quarter_profile()

        short = _tail(slots, [True] * 15 + [False] * 81, profile, step=15)
        enough = _tail(
            slots,
            [True] * 16 + [False] * 80,
            profile,
            step=15,
            published_until=_MONDAY + timedelta(days=1),
        )

        assert short is not None
        assert short.shifted is False
        assert enough is not None
        assert enough.shifted is True
        assert enough.night_import == pytest.approx(_NIGHT + 0.7)

    def test_published_prices_past_a_quarter_hour_horizon_give_no_tail(self) -> None:
        slots = _slots(24, step=15)
        until = slots[-1].end + timedelta(minutes=15)

        assert (
            _tail(
                slots,
                [True] * 96,
                self._quarter_profile(),
                step=15,
                published_until=until,
            )
            is None
        )


class TestDstDay:
    """A tail over the fall-back day is 24 real hours."""

    def test_fall_back_day(self) -> None:
        """Sunday 2026-10-25 has 25 hours; 02:00 comes twice.

        The tail starts at Sunday 00:00 and ends 24 real hours later, at
        23:00 the same day.  Its night holds seven hourly slots.
        """
        saturday = datetime(2026, 10, 24, tzinfo=_TZ)
        profile = PriceProfile(
            any_day_hours={
                hour: (9.0 if hour == 2 else _shape(hour), 0.0) for hour in range(24)
            }
        )
        slots = _slots(24, start=saturday)
        # Saturday's own 02:00 is 1.0, the profile's 9.0: the median shift is 0.
        tail = _tail(slots, [True] * 24, profile)

        assert tail is not None
        assert tail.start == datetime(2026, 10, 25, tzinfo=_TZ)
        assert tail.slot_count == 24
        assert tail.end.astimezone(UTC) - tail.start.astimezone(UTC) == timedelta(
            hours=24
        )
        assert tail.end.astimezone(_TZ).hour == 23
        # 00, 01, 02, 02 again, 03, 04, 05.
        assert tail.night_import == pytest.approx((5 * _NIGHT + 2 * 9.0) / 7)


class TestBlend:
    """``V = (1 − w) × last-day value + w × tail value``."""

    @pytest.mark.parametrize("weight", [0.25, TERMINAL_TAIL_WEIGHT, 1.0])
    def test_value_is_the_weighted_mean(self, weight: float) -> None:
        tail = _tail(_slots(24), [True] * 24, _profile(), weight=weight)

        assert tail is not None
        assert tail.weight == pytest.approx(weight)
        assert tail.last_day_value_per_kwh == pytest.approx(_LAST_DAY_VALUE)
        assert tail.value_per_kwh == pytest.approx(
            (1.0 - weight) * _LAST_DAY_VALUE + weight * _value(0.0)
        )

    def test_weight_is_capped_at_one(self) -> None:
        tail = _tail(_slots(24), [True] * 24, _profile(), weight=3.0)

        assert tail is not None
        assert tail.weight == pytest.approx(1.0)
        assert tail.value_per_kwh == pytest.approx(_value(0.0))

    def test_the_default_weight_is_a_half(self) -> None:
        """Equal error one day ahead (issue #1452) earns equal weight."""
        assert pytest.approx(0.5) == TERMINAL_TAIL_WEIGHT


class TestFlagged:
    """The plan explanation says the end value used estimated prices."""

    def test_as_dict_marks_the_tail_as_estimated(self) -> None:
        tail = _tail(_slots(24), [True] * 24, _profile())
        assert tail is not None

        data = tail.as_dict()

        assert data["estimated"] is True
        assert data["start"] == tail.start.isoformat()
        assert data["end"] == tail.end.isoformat()
        assert data["slot_count"] == 24
        assert data["shifted"] is True
        assert data["weight"] == pytest.approx(0.5)
        assert data["value_per_kwh"] == pytest.approx(tail.value_per_kwh, abs=1e-4)
        assert data["tail_value_per_kwh"] == pytest.approx(_value(0.0), abs=1e-4)
        assert data["last_day_value_per_kwh"] == pytest.approx(_LAST_DAY_VALUE)
        assert data["peak_import_price"] == pytest.approx(_PEAK)
        assert data["night_import_price"] == pytest.approx(_NIGHT)

    def test_explanation_carries_the_tail_and_a_constraint(self) -> None:
        tail = _tail(_slots(24), [True] * 24, _profile())
        explanation = PlanExplanation()

        flag_terminal_price_tail(explanation, tail)

        assert explanation.terminal_price_tail is not None
        assert explanation.terminal_price_tail["estimated"] is True
        assert TERMINAL_TAIL_CONSTRAINT in explanation.constraints
        assert explanation.as_dict()["terminal_price_tail"]["estimated"] is True

    def test_explanation_is_untouched_without_a_tail(self) -> None:
        explanation = PlanExplanation()

        flag_terminal_price_tail(explanation, None)

        assert explanation.terminal_price_tail is None
        assert explanation.constraints == []
        assert explanation.as_dict()["terminal_price_tail"] is None


# ---------------------------------------------------------------------------
# The whole planner, on recorded cycles
# ---------------------------------------------------------------------------


def _recorded(path: Any) -> PlannerInput:
    """Return a recorded cycle with a profile 1.0 above its own first day."""
    recorded, _report = load_planner_input(path)
    profile = PriceProfile(
        any_day_quarters={
            (pp.hour, (pp.slot_in_day or 0) * 15 % 60): (
                (pp.import_price or 0.0) + 1.0,
                (pp.export_price or 0.0) + 1.0,
            )
            for pp in recorded.price_points
            if pp.day_offset == 0
        }
    )
    return replace(recorded, price_profile=profile)


def _same_plan(one: PlannerOutput, other: PlannerOutput) -> None:
    """Assert two runs produced the same horizon, prices, decisions and cost."""
    assert len(one.slots) == len(other.slots)
    for a, b in zip(one.slots, other.slots):
        assert a.start == b.start
        assert a.end == b.end
        assert a.price.import_price == pytest.approx(b.price.import_price)
        assert a.price.export_price == pytest.approx(b.price.export_price)
        assert a.recommendation == b.recommendation
        assert a.batteries_charged_kwh == pytest.approx(b.batteries_charged_kwh)
        assert a.batteries_discharged_kwh == pytest.approx(b.batteries_discharged_kwh)
        assert a.grid_import_kwh == pytest.approx(b.grid_import_kwh)
        assert a.grid_export_kwh == pytest.approx(b.grid_export_kwh)
    assert one.plan_cost is not None
    assert other.plan_cost is not None
    assert one.plan_cost.total_cost == pytest.approx(other.plan_cost.total_cost)
    assert one.plan_cost.score == pytest.approx(other.plan_cost.score)
    assert one.current_recommendation == other.current_recommendation


@_needs_scipy
@pytest.mark.parametrize("path", corpus_paths(), ids=lambda path: path.stem)
class TestPlannerRun:
    """``run_planner`` with and without the tail on every corpus cycle."""

    def test_off_by_default(self, path: Any) -> None:
        """A profile alone changes nothing: the weight defaults to zero."""
        recorded = _recorded(path)
        assert recorded.terminal_price_tail_weight == pytest.approx(0.0)

        output = run_planner(recorded)

        assert output.terminal_price_tail is None
        assert output.explanation.terminal_price_tail is None
        assert TERMINAL_TAIL_CONSTRAINT not in output.explanation.constraints
        _same_plan(output, run_planner(replace(recorded, price_profile=None)))

    @pytest.mark.parametrize("profile", [None, PriceProfile()])
    def test_an_empty_store_plans_exactly_as_before(
        self, path: Any, profile: PriceProfile | None
    ) -> None:
        """Tail enabled, no price history: horizon and plan are unchanged."""
        recorded, _report = load_planner_input(path)
        before = run_planner(replace(recorded, price_profile=profile))

        after = run_planner(
            replace(
                recorded,
                price_profile=profile,
                terminal_price_tail_weight=TERMINAL_TAIL_WEIGHT,
            )
        )

        assert after.terminal_price_tail is None
        assert after.explanation.terminal_price_tail is None
        _same_plan(after, before)

    def test_tail_changes_the_end_value_and_no_slot(self, path: Any) -> None:
        """Same horizon and slot prices; the tail is past the last slot."""
        recorded = _recorded(path)
        before = run_planner(recorded)

        after = run_planner(
            replace(recorded, terminal_price_tail_weight=TERMINAL_TAIL_WEIGHT)
        )

        tail = after.terminal_price_tail
        assert tail is not None
        assert len(after.slots) == len(before.slots)
        assert tail.start == after.slots[-1].end
        assert all(slot.end <= tail.start for slot in after.slots)
        for a, b in zip(after.slots, before.slots):
            assert a.start == b.start
            assert a.price.import_price == pytest.approx(b.price.import_price)
            assert a.price.export_price == pytest.approx(b.price.export_price)
        assert after.data_quality == before.data_quality
        assert after.explanation.terminal_price_tail == tail.as_dict()
        assert TERMINAL_TAIL_CONSTRAINT in after.explanation.constraints

    def test_invariants_hold_with_the_blended_value(self, path: Any) -> None:
        """Energy balance, SoC bounds, cost identity and terminal accounting."""
        recorded = replace(_recorded(path), terminal_price_tail_weight=1.0)

        output = run_planner(recorded)

        violations = check_invariants(recorded, output)
        assert not violations, format_violations(violations)
        tail = output.terminal_price_tail
        assert tail is not None
        assert output.plan_cost is not None
        # The term the plan was scored with is (E_0 − E_end) × the blended V.
        expected = sum(
            terminal_soc_value(
                slot.batteries_charged_kwh,
                slot.batteries_discharged_kwh,
                tail.value_per_kwh,
            )
            for slot in output.slots
            if slot.recommendation not in ("time_passed", "missing_input_entities")
        )
        assert output.plan_cost.terminal_soc_value == pytest.approx(expected, abs=1e-6)


@_needs_scipy
def test_a_dearer_tail_never_ends_the_horizon_emptier() -> None:
    """A higher end value cannot make the plan leave less energy.

    Both profiles are shifted to the published level, so only their shape
    differs: a night 2.0 below the day makes leftover energy cheap to
    replace, a flat day does not.
    """
    path = next(p for p in corpus_paths() if p.stem == "cycle-2026-09-24-1016")
    recorded, _report = load_planner_input(path)
    cheap_night = PriceProfile(
        any_day_hours={h: (1.0 if h < 6 else 3.0, 0.0) for h in range(24)}
    )
    flat = PriceProfile(any_day_hours={h: (3.0, 0.0) for h in range(24)})

    low = run_planner(
        replace(recorded, price_profile=cheap_night, terminal_price_tail_weight=1.0)
    )
    high = run_planner(
        replace(recorded, price_profile=flat, terminal_price_tail_weight=1.0)
    )

    assert low.terminal_price_tail is not None
    assert high.terminal_price_tail is not None
    assert (
        high.terminal_price_tail.value_per_kwh
        > low.terminal_price_tail.value_per_kwh + 0.5
    )
    assert high.battery_soc_at_end >= low.battery_soc_at_end - 1e-6


# ---------------------------------------------------------------------------
# The off path is the planner of origin/main
# ---------------------------------------------------------------------------

_GOLDEN = json.loads(
    (
        Path(__file__).parent.parent / "fixtures" / "terminal_tail_off_golden.json"
    ).read_text()
)


@_needs_scipy
@pytest.mark.parametrize("name", sorted(_GOLDEN["cycles"]))
@pytest.mark.parametrize("enabled_without_history", [False, True])
def test_off_path_equals_the_plan_recorded_from_main(
    name: str, enabled_without_history: bool
) -> None:
    """The cost and every slot match what ``origin/main`` planned.

    The fixture was recorded from ``origin/main`` before this feature, from
    the same corpus cycles.  With the tail off, and with it on but without a
    price history, the plan must be that plan.
    """
    golden = _GOLDEN["cycles"][name]
    path = next(p for p in corpus_paths() if p.stem == name)
    recorded, _report = load_planner_input(path)
    assert recorded.terminal_price_tail_weight == pytest.approx(0.0)
    if enabled_without_history:
        recorded = replace(
            recorded,
            price_profile=None,
            terminal_price_tail_weight=TERMINAL_TAIL_WEIGHT,
        )

    output = run_planner(recorded)

    assert output.terminal_price_tail is None
    assert output.plan_cost is not None
    assert output.winner_name == golden["winner_name"]
    assert output.plan_cost.total_cost == pytest.approx(golden["total_cost"], abs=1e-6)
    assert output.plan_cost.score == pytest.approx(golden["score"], abs=1e-6)
    assert output.plan_cost.terminal_soc_value == pytest.approx(
        golden["terminal_soc_value"], abs=1e-6
    )
    assert output.battery_soc_at_end == pytest.approx(
        golden["battery_soc_at_end"], abs=1e-6
    )
    assert len(output.slots) == len(golden["slots"])
    for slot, (label, charged, discharged, grid_import, grid_export) in zip(
        output.slots, golden["slots"]
    ):
        assert slot.recommendation == label, slot.start
        assert slot.batteries_charged_kwh == pytest.approx(charged, abs=1e-6)
        assert slot.batteries_discharged_kwh == pytest.approx(discharged, abs=1e-6)
        assert slot.grid_import_kwh == pytest.approx(grid_import, abs=1e-6)
        assert slot.grid_export_kwh == pytest.approx(grid_export, abs=1e-6)


class TestOffPathDoesNoWork:
    """With the tail off nothing is read and nothing is built."""

    @pytest.mark.parametrize("weight", [0.0, math.nan, -1.0])
    def test_resolve_returns_before_touching_its_inputs(self, weight: float) -> None:
        """A ``tsi`` of ``None`` would raise if a list were built from it."""
        inp = PlannerInput(
            now_iso="", price_profile=_profile(), terminal_price_tail_weight=weight
        )

        tail = resolve_terminal_price_tail(
            _slots(24), cast("Any", None), inp, _TZ, _LAST_DAY_VALUE, 4, _CYCLE_COST
        )

        assert tail is None


# ---------------------------------------------------------------------------
# How far the published prices reach, from the populator to the planner
# ---------------------------------------------------------------------------

_IMPORT = "sensor.import_price"
_EXPORT = "sensor.export_price"


def _day_prices(day: datetime) -> list[dict[str, Any]]:
    """Return hourly price entries for one local day."""
    return [
        {"start": (day + timedelta(hours=hour)).isoformat(), "price": 1.0 + hour / 100}
        for hour in range(24)
    ]


def _populate(
    hours: int, attributes: dict[str, Any], states: dict[str, Any] | None = None
) -> tuple[PlannerInput, ForecastCoverage]:
    """Populate a horizon of *hours* at Monday 09:00 and build the planner input."""
    cfg = SensorConfig()
    cfg.recommendation_interval_minutes = 60
    cfg.recommendation_interval_length = hours
    cfg.electricity_price_update_interval = 60
    cfg.import_electricity_price_sensor = _IMPORT
    cfg.export_electricity_price_sensor = _EXPORT
    now = _MONDAY.replace(hour=9)
    with patch("homeassistant.util.dt.now", return_value=now):
        recs = generate_recommendation_intervals(60, hours)
        coverage = populate_price_and_solcast_from_snapshot(
            recs,
            StateSnapshot(
                live=LiveState(),
                sensor_attributes=attributes,
                sensor_states=states or {},
            ),
            cfg,
        )
        planner_input = build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=recs,
            previous_winner_name=None,
            previous_winner_score=0.0,
            forecast_coverage=coverage,
        )
    return planner_input, coverage


class TestPublishedUntil:
    """The populator reports where the published import prices end."""

    @pytest.mark.parametrize("hours", [12, 24, 36, 48])
    def test_prices_past_the_horizon_count(self, hours: int) -> None:
        """Today and tomorrow are published: the reach is the end of tomorrow."""
        entries = _day_prices(_MONDAY) + _day_prices(_MONDAY + timedelta(days=1))
        planner_input, coverage = _populate(
            hours,
            {_IMPORT: {"prices_today": entries}, _EXPORT: {"prices_today": entries}},
        )

        assert coverage.import_published_until == _MONDAY + timedelta(days=2)
        assert planner_input.price_published_until == _MONDAY + timedelta(days=2)
        assert len(planner_input.price_points) == hours

    def test_only_today_published(self) -> None:
        entries = _day_prices(_MONDAY)
        planner_input, _coverage = _populate(
            48, {_IMPORT: {"prices_today": entries}, _EXPORT: {"prices_today": entries}}
        )

        assert planner_input.price_published_until == _MONDAY + timedelta(days=1)

    def test_predictions_do_not_count(self) -> None:
        """A ``forecast`` attribute holds predictions: nothing is published."""
        entries = _day_prices(_MONDAY)
        planner_input, coverage = _populate(
            24, {_IMPORT: {"forecast": entries}, _EXPORT: {"forecast": entries}}
        )

        assert coverage.import_published_until is None
        assert planner_input.price_published_until is None

    def test_a_flat_price_is_published_for_ever(self) -> None:
        planner_input, _coverage = _populate(24, {}, {_IMPORT: "2.5", _EXPORT: "0.5"})

        assert planner_input.price_published_until == datetime.max.replace(tzinfo=UTC)

    def test_without_coverage_nothing_is_known(self) -> None:
        planner_input = build_planner_input(
            cfg=SensorConfig(),
            live=LiveState(),
            hourly_recommendations=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
        )

        assert planner_input.price_published_until is None


@_needs_scipy
def test_the_planner_gives_no_tail_when_published_prices_reach_further() -> None:
    """``run_planner`` passes the populator's time on to the tail."""
    path = next(p for p in corpus_paths() if p.stem == "cycle-2026-09-24-1016")
    recorded = replace(_recorded(path), terminal_price_tail_weight=TERMINAL_TAIL_WEIGHT)
    horizon_end = run_planner(recorded).slots[-1].end

    at_the_end = run_planner(replace(recorded, price_published_until=horizon_end))
    past_the_end = run_planner(
        replace(recorded, price_published_until=horizon_end + timedelta(minutes=15))
    )

    assert at_the_end.terminal_price_tail is not None
    assert past_the_end.terminal_price_tail is None
    assert past_the_end.explanation.terminal_price_tail is None
    _same_plan(past_the_end, run_planner(_recorded(path)))


# ---------------------------------------------------------------------------
# The option
# ---------------------------------------------------------------------------


class TestOption:
    """``hsem_terminal_price_tail_enabled`` is opt-in and reaches the planner."""

    def test_default_is_off(self) -> None:
        assert DEFAULT_CONFIG_VALUES["hsem_terminal_price_tail_enabled"] is False
        assert PlannerInput(now_iso="").terminal_price_tail_weight == pytest.approx(0.0)

    @pytest.mark.parametrize(
        ("options", "expected"),
        [
            ({}, 0.0),
            ({"hsem_terminal_price_tail_enabled": False}, 0.0),
            ({"hsem_terminal_price_tail_enabled": True}, TERMINAL_TAIL_WEIGHT),
        ],
    )
    def test_the_option_sets_the_planner_weight(
        self, options: dict[str, Any], expected: float
    ) -> None:
        entry = MagicMock()
        entry.data = {}
        entry.options = options
        cfg = build_sensor_config(entry)

        planner_input = build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
        )

        assert cfg.terminal_price_tail_enabled is bool(
            options.get("hsem_terminal_price_tail_enabled", False)
        )
        assert planner_input.terminal_price_tail_weight == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_flow_offers_and_requires_it(self) -> None:
        schema = await get_battery_economics_step_schema(None)
        keys = {str(key.schema) for key in schema.schema}

        assert "hsem_terminal_price_tail_enabled" in keys
        errors = await validate_battery_economics_input({})
        assert errors["hsem_terminal_price_tail_enabled"] == "required"

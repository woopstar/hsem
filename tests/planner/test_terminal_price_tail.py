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

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES
from custom_components.hsem.coordinator_builder import build_planner_input
from custom_components.hsem.custom_sensors.config_reader import build_sensor_config
from custom_components.hsem.flows.battery_economics import (
    get_battery_economics_step_schema,
    validate_battery_economics_input,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.price_profile import PriceProfile
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


def _slots(hours: int, offset: float = 0.0) -> list[PlannedSlot]:
    """Return hourly slots from Monday 00:00 priced at the shape plus *offset*."""
    return [
        PlannedSlot(
            start=_MONDAY + timedelta(hours=hour),
            end=_MONDAY + timedelta(hours=hour + 1),
            price=SlotPrice(import_price=_shape(hour % 24) + offset, export_price=0.0),
        )
        for hour in range(hours)
    ]


def _tail(
    slots: list[PlannedSlot],
    published: list[bool],
    profile: PriceProfile | None,
    *,
    weight: float = 0.5,
    last_day_value: float | None = _LAST_DAY_VALUE,
) -> TerminalPriceTail | None:
    return terminal_price_tail(
        slots,
        published,
        profile,
        60,
        _TZ,
        last_day_value=last_day_value,
        weight=weight,
        top_n=4,
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

    def test_weight_zero_disables_it(self) -> None:
        assert _tail(_slots(24), [True] * 24, _profile(), weight=0.0) is None

    def test_a_negative_weight_disables_it(self) -> None:
        assert _tail(_slots(24), [True] * 24, _profile(), weight=-1.0) is None

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


class TestTailPrices:
    """The profile gives the shape; the level follows the 24-hour rule."""

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
        assert tail.shifted_slots == 24
        assert tail.peak_import == pytest.approx(_PEAK + 0.7)
        assert tail.night_import == pytest.approx(_NIGHT + 0.7)
        assert tail.tail_value_per_kwh == pytest.approx(_value(0.7))

    def test_a_tail_a_day_after_the_last_price_is_not_shifted(self) -> None:
        """48 h horizon, tomorrow unpublished: the tail starts 24 h too late."""
        published = [True] * 24 + [False] * 24
        tail = _tail(_slots(48, offset=0.7), published, _profile())

        assert tail is not None
        assert tail.shifted_slots == 0
        assert tail.tail_value_per_kwh == pytest.approx(_value(0.0))

    def test_the_shift_ends_24_hours_after_the_last_price(self) -> None:
        """36 h horizon, 24 h published: the first 12 tail hours are shifted."""
        published = [True] * 24 + [False] * 12
        tail = _tail(_slots(36, offset=0.7), published, _profile())

        assert tail is not None
        assert tail.start == _MONDAY + timedelta(hours=36)
        assert tail.shifted_slots == 12
        # Tuesday's peak (17-21) is shifted, Wednesday's night is not.
        assert tail.peak_import == pytest.approx(_PEAK + 0.7)
        assert tail.night_import == pytest.approx(_NIGHT)

    def test_outage_uses_the_last_known_shift_while_it_is_valid(self) -> None:
        """No published slot: the stored shift is used until it is too old."""
        until = _MONDAY + timedelta(days=1, hours=12)
        profile = _profile(import_level=(0.3, until))
        tail = _tail(_slots(24), [False] * 24, profile)

        assert tail is not None
        assert tail.shifted_slots == 12
        assert tail.night_import == pytest.approx(_NIGHT + 0.3)
        assert tail.peak_import == pytest.approx(_PEAK)

    def test_outage_without_a_stored_shift_uses_the_profile_as_recorded(self) -> None:
        tail = _tail(_slots(24, offset=0.7), [False] * 24, _profile())

        assert tail is not None
        assert tail.shifted_slots == 0
        assert tail.tail_value_per_kwh == pytest.approx(_value(0.0))

    def test_too_few_published_slots_give_no_shift(self) -> None:
        """Three published hours are below the 240 minutes a shift needs."""
        published = [True] * 3 + [False] * 21
        tail = _tail(_slots(24, offset=0.7), published, _profile())

        assert tail is not None
        assert tail.shifted_slots == 0


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
        assert data["shifted_slots"] == 24
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

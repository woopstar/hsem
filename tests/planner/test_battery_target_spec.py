"""Resolving and scoring the house-battery target (issue #1109).

Covers the pure pieces around the two-stage solve: which occurrence of the
daily target time is enforced, how the target percentage becomes model kWh,
how the shortfall price ``P`` is built, and how the selector scores it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner import battery_target
from custom_components.hsem.planner.battery_target import (
    PENALTY_EPSILON,
    BatteryTargetSpec,
    battery_target_penalty,
    next_target_slot_index,
    resolve_battery_target,
    summarize_battery_target,
    target_kwh_for_pct,
    target_penalty_per_kwh,
)
from custom_components.hsem.planner.cost_function import CostWeights, score_plan
from custom_components.hsem.planner.milp._battery_target_rows import (
    BatteryTargetRows,
    add_battery_target_row,
    cap_grid_import,
    grid_export_bounds,
    grid_import_bounds,
    lp_pin_flows,
)
from custom_components.hsem.planner.milp._layout import (
    build_milp_column_layout,
    derive_milp_offsets,
)
from custom_components.hsem.planner.milp._objective import ev_deadline_penalty_per_kwh
from custom_components.hsem.utils.prices import SlotPrice
from tests.planner.fixtures import make_summer_day_input

_TZ = ZoneInfo("Europe/Copenhagen")
_START = datetime(2026, 9, 14, 10, 0, tzinfo=_TZ)


def _slots(
    count: int,
    *,
    start: datetime = _START,
    minutes: int = 60,
    export_price: list[float] | None = None,
    import_price: float = 0.6,
) -> list[PlannedSlot]:
    """Build *count* consecutive slots with optional per-slot export prices."""
    slots: list[PlannedSlot] = []
    for i in range(count):
        slot_start = start + timedelta(minutes=minutes * i)
        slots.append(
            PlannedSlot(
                start=slot_start,
                end=slot_start + timedelta(minutes=minutes),
                price=SlotPrice(
                    import_price=import_price,
                    export_price=export_price[i] if export_price else 0.3,
                ),
            )
        )
    return slots


def _inp(**changes: object) -> PlannerInput:
    """A 10 kWh battery at 63 % with a 10 % end-of-discharge floor."""
    base = make_summer_day_input(
        now_iso="2026-09-14T10:00:00+02:00", battery_soc_pct=63.0
    )
    fields: dict[str, object] = {
        "battery_target_soc_enabled": True,
        "battery_target_soc_pct": 100.0,
        "battery_target_soc_time": "17:00:00",
        **changes,
    }
    return replace(base, **fields)  # type: ignore[arg-type]  # field overrides


def _ev(**changes: object) -> EVConfig:
    """An EV 6 kWh short of its target with a deadline."""
    base = EVConfig(
        enabled=True,
        initial_soc_kwh=42.0,
        target_kwh=48.0,
        capacity_kwh=60.0,
        max_charge_per_slot=3.0,
        charger_efficiency=0.92,
        charger_min_power_w=0.0,
        deadline_slot=6,
    )
    return replace(base, **changes)  # type: ignore[arg-type]  # field overrides


# ---------------------------------------------------------------------------
# Which occurrence is enforced
# ---------------------------------------------------------------------------


class TestNextOccurrence:
    """Only the next occurrence is enforced; one in the current slot rolls over."""

    def test_today_when_the_target_is_ahead(self) -> None:
        slots = _slots(24)

        resolved = next_target_slot_index(slots, _START, time(17, 0))

        assert resolved is not None
        occurrence, index = resolved
        assert occurrence == datetime(2026, 9, 14, 17, 0, tzinfo=_TZ)
        assert slots[index].end == occurrence
        assert index == 6

    def test_target_at_the_end_of_the_current_slot_is_enforced(self) -> None:
        slots = _slots(24)
        now = datetime(2026, 9, 14, 16, 30, tzinfo=_TZ)

        resolved = next_target_slot_index(slots, now, time(17, 0))

        assert resolved is not None
        assert resolved[0] == datetime(2026, 9, 14, 17, 0, tzinfo=_TZ)
        assert slots[resolved[1]].start == datetime(2026, 9, 14, 16, 0, tzinfo=_TZ)

    def test_target_inside_the_current_slot_rolls_to_the_next_day(self) -> None:
        slots = _slots(48)
        now = datetime(2026, 9, 14, 16, 30, tzinfo=_TZ)

        resolved = next_target_slot_index(slots, now, time(16, 45))

        assert resolved is not None
        occurrence, index = resolved
        assert occurrence == datetime(2026, 9, 15, 16, 45, tzinfo=_TZ)
        # Last slot ending at or before 16:45 is the 15:00-16:00 slot.
        assert slots[index].end == datetime(2026, 9, 15, 16, 0, tzinfo=_TZ)

    def test_target_already_passed_rolls_to_the_next_day(self) -> None:
        slots = _slots(48)
        now = datetime(2026, 9, 14, 17, 5, tzinfo=_TZ)

        resolved = next_target_slot_index(slots, now, time(17, 0))

        assert resolved is not None
        assert resolved[0] == datetime(2026, 9, 15, 17, 0, tzinfo=_TZ)
        assert slots[resolved[1]].end == resolved[0]

    def test_off_grid_target_uses_the_last_slot_ending_before_it(self) -> None:
        slots = _slots(96, minutes=15)

        resolved = next_target_slot_index(slots, _START, time(17, 10))

        assert resolved is not None
        assert slots[resolved[1]].end == datetime(2026, 9, 14, 17, 0, tzinfo=_TZ)

    def test_occurrence_beyond_the_horizon_is_not_enforced(self) -> None:
        assert next_target_slot_index(_slots(5), _START, time(17, 0)) is None

    def test_no_future_slots(self) -> None:
        now = _START + timedelta(days=3)
        assert next_target_slot_index(_slots(5), now, time(17, 0)) is None

    def test_occurrence_before_every_future_slot_end_after_rollover(self) -> None:
        """A single remaining slot ending after tomorrow's occurrence has no T."""
        slot = PlannedSlot(
            start=datetime(2026, 9, 14, 16, 30, tzinfo=_TZ),
            end=datetime(2026, 9, 15, 18, 0, tzinfo=_TZ),
            price=SlotPrice(import_price=0.5, export_price=0.3),
        )
        now = datetime(2026, 9, 14, 17, 30, tzinfo=_TZ)

        assert next_target_slot_index([slot], now, time(17, 0)) is None

    def test_dst_day_resolves_to_local_wall_clock_time(self) -> None:
        """The occurrence is 17:00 local on the 25-hour day DST ends."""
        start = datetime(2026, 10, 24, 18, 0, tzinfo=_TZ)
        slots = _slots(30, start=start)
        # Slot ends advance by elapsed time, like the planner's slot grid.
        for i, slot in enumerate(slots):
            begin = (start.astimezone(ZoneInfo("UTC")) + timedelta(hours=i)).astimezone(
                _TZ
            )
            slot.start, slot.end = (
                begin,
                (begin.astimezone(ZoneInfo("UTC")) + timedelta(hours=1)).astimezone(
                    _TZ
                ),
            )

        resolved = next_target_slot_index(slots, start, time(17, 0))

        assert resolved is not None
        occurrence, index = resolved
        assert occurrence.isoformat() == "2026-10-25T17:00:00+01:00"
        assert slots[index].end == occurrence
        assert index == 23


# ---------------------------------------------------------------------------
# Target percentage in model coordinates
# ---------------------------------------------------------------------------


class TestTargetKwh:
    """The percentage is absolute SoC; the model origin is the effective floor."""

    def test_full_target_is_the_usable_capacity(self) -> None:
        assert target_kwh_for_pct(_inp(), 100.0, 9.0) == pytest.approx(9.0)

    def test_partial_target_is_measured_from_the_floor(self) -> None:
        # 80 % absolute on a 10 kWh battery with a 10 % floor = 7 kWh in the model.
        assert target_kwh_for_pct(_inp(), 80.0, 9.0) == pytest.approx(7.0)

    def test_target_below_the_floor_is_zero(self) -> None:
        assert target_kwh_for_pct(_inp(), 5.0, 9.0) == pytest.approx(0.0)

    def test_target_above_max_soc_is_clamped(self) -> None:
        inp = _inp(battery_max_soc_pct=90.0)
        assert target_kwh_for_pct(inp, 100.0, 8.0) == pytest.approx(8.0)

    def test_dynamic_floor_does_not_move_the_origin(self) -> None:
        """The origin is the hardware floor with a dynamic floor too (#1188)."""
        plain = _inp()
        floored = _inp(dynamic_discharge_floor_pct=30.0)
        for target_pct in (100.0, 50.0):
            assert target_kwh_for_pct(floored, target_pct, 9.0) == pytest.approx(
                target_kwh_for_pct(plain, target_pct, 9.0)
            )

    def test_clamped_to_the_model_usable_capacity(self) -> None:
        assert target_kwh_for_pct(_inp(), 100.0, 6.5) == pytest.approx(6.5)

    def test_out_of_range_percentage_is_clamped(self) -> None:
        assert target_kwh_for_pct(_inp(), 250.0, 9.0) == pytest.approx(9.0)
        assert target_kwh_for_pct(_inp(), -20.0, 9.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Shortfall price P
# ---------------------------------------------------------------------------


class TestPenalty:
    """P outbids the best export before T and stays below EV deadline penalties."""

    def test_uses_the_best_export_price_up_to_the_target_only(self) -> None:
        slots = _slots(10, export_price=[0.2, 1.4, 0.3, 0.3, 0.3, 0.3, 0.3] + [5.0] * 3)

        penalty = target_penalty_per_kwh(
            slots,
            list(range(10)),
            6,
            charge_efficiency_pct=97.0,
            cycle_cost_per_kwh=0.05,
            ev_configs=None,
        )

        assert penalty == pytest.approx(1.4 / 0.97 + 0.05 + PENALTY_EPSILON)

    def test_non_finite_and_negative_export_prices_are_ignored(self) -> None:
        slots = _slots(3, export_price=[float("nan"), -0.5, float("inf")])

        penalty = target_penalty_per_kwh(
            slots,
            [0, 1, 2],
            2,
            charge_efficiency_pct=100.0,
            cycle_cost_per_kwh=-1.0,
            ev_configs=[],
        )

        assert penalty == pytest.approx(PENALTY_EPSILON)

    def test_capped_below_the_smallest_ev_deadline_penalty(self) -> None:
        slots = _slots(10, export_price=[40.0] * 10, import_price=0.2)
        small = _ev(initial_soc_kwh=47.5)  # needs 0.5 kWh → factor 1.0
        large = _ev(initial_soc_kwh=40.0)
        floor = ev_deadline_penalty_per_kwh(small, 0.2, 10)
        assert floor == pytest.approx(0.2 * 1.0 * 10.0)

        penalty = target_penalty_per_kwh(
            slots,
            list(range(10)),
            6,
            charge_efficiency_pct=97.0,
            cycle_cost_per_kwh=0.0,
            ev_configs=[large, small],
        )

        assert penalty == pytest.approx(2.0 - PENALTY_EPSILON)

    def test_evs_without_an_active_deadline_do_not_cap_it(self) -> None:
        slots = _slots(10, export_price=[40.0] * 10)
        inactive = [
            _ev(deadline_slot=None),
            _ev(initial_soc_kwh=48.0),
            _ev(charge_past_target=True),
        ]
        assert all(ev_deadline_penalty_per_kwh(ev, 0.6, 10) is None for ev in inactive)

        penalty = target_penalty_per_kwh(
            slots,
            list(range(10)),
            6,
            charge_efficiency_pct=100.0,
            cycle_cost_per_kwh=0.0,
            ev_configs=inactive,
        )

        assert penalty == pytest.approx(40.0 + PENALTY_EPSILON)

    def test_escalated_ev_deadline_raises_its_penalty(self) -> None:
        reachable = _ev(deadline_slot=6)
        unreachable = _ev(deadline_slot=0, initial_soc_kwh=10.0)

        normal = ev_deadline_penalty_per_kwh(reachable, 0.6, 10)
        escalated = ev_deadline_penalty_per_kwh(unreachable, 0.6, 10)

        assert normal == pytest.approx(0.6 * 6.0 * 10.0)
        assert unreachable.deadline_escalated(10)
        assert escalated == pytest.approx(0.6 * 38.0 * 10.0 * 5.0)


# ---------------------------------------------------------------------------
# resolve_battery_target
# ---------------------------------------------------------------------------


class TestResolve:
    """The engine's single entry point."""

    def test_disabled_resolves_to_nothing(self) -> None:
        inp = _inp(battery_target_soc_enabled=False)
        assert (
            resolve_battery_target(
                inp, _slots(24), _START, usable_kwh=9.0, cycle_cost_per_kwh=0.0
            )
            is None
        )

    def test_enabled_resolves_the_next_occurrence(self) -> None:
        slots = _slots(24, export_price=[1.4] + [0.3] * 23)

        spec = resolve_battery_target(
            _inp(battery_target_soc_pct=90.0),
            slots,
            _START,
            usable_kwh=9.0,
            cycle_cost_per_kwh=0.05,
            ev_configs=None,
        )

        assert spec is not None
        assert spec.target_time == datetime(2026, 9, 14, 17, 0, tzinfo=_TZ)
        assert spec.slot_end == slots[6].end
        assert spec.target_pct == pytest.approx(90.0)
        assert spec.target_kwh == pytest.approx(8.0)
        charge_eff = _inp().battery_charge_efficiency_pct / 100.0
        assert spec.penalty_per_kwh == pytest.approx(
            1.4 / charge_eff + 0.05 + PENALTY_EPSILON
        )

    def test_short_time_format_is_accepted(self) -> None:
        spec = resolve_battery_target(
            _inp(battery_target_soc_time="17:00"),
            _slots(24),
            _START,
            usable_kwh=9.0,
            cycle_cost_per_kwh=0.0,
        )
        assert spec is not None
        assert spec.target_time.hour == 17

    def test_invalid_time_is_ignored_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logged: list[str] = []
        monkeypatch.setattr(
            battery_target, "log_planner", lambda level, *_a: logged.append(level)
        )

        spec = resolve_battery_target(
            _inp(battery_target_soc_time="not-a-time"),
            _slots(24),
            _START,
            usable_kwh=9.0,
            cycle_cost_per_kwh=0.0,
        )

        assert spec is None
        assert logged == ["warning"]

    def test_no_usable_capacity_resolves_to_nothing(self) -> None:
        assert (
            resolve_battery_target(
                _inp(), _slots(24), _START, usable_kwh=0.0, cycle_cost_per_kwh=0.0
            )
            is None
        )

    def test_occurrence_beyond_the_horizon_resolves_to_nothing(self) -> None:
        assert (
            resolve_battery_target(
                _inp(), _slots(4), _START, usable_kwh=9.0, cycle_cost_per_kwh=0.0
            )
            is None
        )


# ---------------------------------------------------------------------------
# Selector score
# ---------------------------------------------------------------------------


def _scored_slots(capacity_at_target: float) -> list[PlannedSlot]:
    """Two future slots; the first one is the target slot."""
    slots = _slots(2)
    for slot in slots:
        slot.recommendation = None
        slot.estimated_battery_soc_pct = 50.0
        slot.grid_import_kwh = 1.0
    slots[0].estimated_battery_capacity_kwh = capacity_at_target
    slots[1].estimated_battery_capacity_kwh = 0.0
    return slots


def _target_spec(target_kwh: float = 9.0, penalty: float = 1.5) -> BatteryTargetSpec:
    return BatteryTargetSpec(
        target_time=_START + timedelta(hours=1),
        slot_end=_START + timedelta(hours=1),
        target_pct=100.0,
        target_kwh=target_kwh,
        penalty_per_kwh=penalty,
    )


class TestScore:
    """The penalty enters ``score`` for every candidate, never ``total_cost``."""

    def test_no_spec_no_penalty(self) -> None:
        assert battery_target_penalty(_scored_slots(1.0), None) == pytest.approx(0.0)

    def test_shortfall_is_priced_at_p(self) -> None:
        assert battery_target_penalty(
            _scored_slots(6.5), _target_spec()
        ) == pytest.approx(2.5 * 1.5)

    def test_target_met_or_within_rounding_costs_nothing(self) -> None:
        spec = _target_spec()
        assert battery_target_penalty(_scored_slots(9.0), spec) == pytest.approx(0.0)
        assert battery_target_penalty(_scored_slots(9.4), spec) == pytest.approx(0.0)
        assert battery_target_penalty(_scored_slots(8.9995), spec) == pytest.approx(0.0)

    def test_missing_target_slot_costs_nothing(self) -> None:
        spec = replace(_target_spec(), slot_end=_START + timedelta(days=9))
        assert battery_target_penalty(_scored_slots(1.0), spec) == pytest.approx(0.0)

    @pytest.mark.parametrize("discount", [1.0, 0.99])
    def test_score_includes_it_and_total_cost_does_not(self, discount: float) -> None:
        slots = _scored_slots(6.5)
        base_weights = CostWeights(time_discount_rate=discount, min_soc_pct=10.0)
        with_target = replace(base_weights, battery_target=_target_spec())

        plain = score_plan(slots, base_weights, now=_START)
        scored = score_plan(slots, with_target, now=_START)

        assert plain.battery_target_penalty == pytest.approx(0.0)
        assert scored.battery_target_penalty == pytest.approx(3.75)
        assert scored.total_cost == pytest.approx(plain.total_cost)
        # Undiscounted, like the MILP slack it mirrors.
        assert scored.score == pytest.approx(plain.score + 3.75)
        assert scored.total == pytest.approx(scored.score)


# ---------------------------------------------------------------------------
# Published diagnostics
# ---------------------------------------------------------------------------


class TestSummary:
    """One dict for the planner output and the working-mode sensor."""

    def test_disabled_publishes_nothing(self) -> None:
        assert summarize_battery_target(None, [], _scored_slots(1.0)) is None

    def test_without_a_milp_record_reports_the_selected_plan(self) -> None:
        summary = summarize_battery_target(
            _target_spec(),
            [SimpleNamespace(name="passive", diagnostics=None)],
            _scored_slots(6.5),
        )

        assert summary is not None
        assert summary["stage2_status"] == "milp_unavailable"
        assert summary["stage2_ran"] is False
        assert summary["target_kwh"] == pytest.approx(9.0)
        assert summary["penalty_per_kwh"] == pytest.approx(1.5)
        assert summary["selected_projected_kwh"] == pytest.approx(6.5)
        assert summary["selected_shortfall_kwh"] == pytest.approx(2.5)
        assert summary["target_time"] == "2026-09-14T11:00:00+02:00"

    def test_milp_record_is_merged_in(self) -> None:
        record = {
            "stage2_ran": True,
            "stage2_status": "solved",
            "projected_kwh": 9.0,
            "shortfall_kwh": 0.0,
            "max_import_delta_kwh": 0.0,
        }
        candidates = [
            SimpleNamespace(name="passive", diagnostics={"other": 1}),
            SimpleNamespace(name="milp", diagnostics={"battery_target": record}),
        ]

        summary = summarize_battery_target(_target_spec(), candidates, [])

        assert summary is not None
        assert summary["stage2_status"] == "solved"
        assert summary["stage2_ran"] is True
        assert summary["projected_kwh"] == pytest.approx(9.0)
        assert "selected_projected_kwh" not in summary


# ---------------------------------------------------------------------------
# LP rows and bounds
# ---------------------------------------------------------------------------


class TestRows:
    """The stage-2 additions are inert unless asked for."""

    def test_no_cap_returns_the_physical_bound_untouched(self) -> None:
        bound = np.array([1.0, 2.0])
        assert cap_grid_import(bound, None) is bound

    def test_cap_tightens_and_never_goes_negative(self) -> None:
        capped = cap_grid_import(np.array([1.0, 2.0, 3.0]), [0.5, 5.0, -1.0])
        assert capped.tolist() == pytest.approx([0.5, 2.0, 0.0])

    def test_bounds_without_a_floor_start_at_zero(self) -> None:
        assert grid_import_bounds([1.5, -0.2], None) == [(0.0, 1.5), (0.0, 0.0)]

    def test_floor_is_clamped_into_the_bound(self) -> None:
        bounds = grid_import_bounds([1.0, 1.0, 1.0], [0.4, 3.0, -2.0])
        assert bounds == [(0.4, 1.0), (1.0, 1.0), (0.0, 1.0)]

    def test_export_bounds_without_a_floor_start_at_zero(self) -> None:
        assert grid_export_bounds([1.5, -0.2], None) == [(0.0, 1.5), (0.0, 0.0)]

    def test_export_floor_is_clamped_into_the_bound(self) -> None:
        bounds = grid_export_bounds([1.0, 1.0, 1.0], [0.4, 3.0, -2.0])
        assert bounds == [(0.4, 1.0), (1.0, 1.0), (0.0, 1.0)]

    def test_lp_pin_flows_reports_import_and_battery_origin_export(self) -> None:
        """Battery export is AC and never more than the slot really exported."""
        layout = build_milp_column_layout(3, 0, fuse_active=False)
        offsets = derive_milp_offsets(layout, 0)
        solution = np.zeros(offsets.n_vars)
        solution[offsets.gi_off : offsets.gi_off + 3] = [0.7, 0.0, 0.0]
        solution[offsets.ge_off : offsets.ge_off + 3] = [0.0, 3.0, 0.5]
        # Slot 1: 2 kWh DC of battery export next to PV; slot 2: the declared
        # battery export exceeds what was exported.
        solution[offsets.battery_export_off : offsets.battery_export_off + 3] = [
            0.0,
            2.0,
            1.0,
        ]

        flows = lp_pin_flows(solution, 3, offsets, 0.9)

        assert flows["lp_grid_import_kwh"] == pytest.approx([0.7, 0.0, 0.0])
        assert flows["lp_battery_export_ac_kwh"] == pytest.approx([0.0, 1.8, 0.5])

    def test_layout_declares_the_slack_only_when_asked(self) -> None:
        plain = build_milp_column_layout(4, 0, fuse_active=True)
        staged = build_milp_column_layout(4, 0, fuse_active=True, battery_target=True)

        assert not plain.has("battery_target_penalty")
        assert staged.width("battery_target_penalty") == 1
        assert staged.column_count == plain.column_count + 1
        assert derive_milp_offsets(staged, 0).n_vars == staged.column_count

    def test_target_row_sums_net_charge_up_to_the_target_slot(self) -> None:
        layout = build_milp_column_layout(3, 0, fuse_active=False, battery_target=True)
        offsets = derive_milp_offsets(layout, 0)
        penalty_off = layout.offset("battery_target_penalty")
        rows = BatteryTargetRows(
            target_index=1,
            target_kwh=8.0,
            penalty_per_kwh=1.2,
            grid_import_floor=(0.0, 0.0, 0.0),
            grid_import_cap=(1.0, 1.0, 1.0),
            grid_export_floor=(0.0, 0.0, 0.0),
        )

        a_ub, b_ub = add_battery_target_row(
            np.zeros((2, offsets.n_vars)),
            np.zeros(2),
            rows,
            ec_off=offsets.ec_off,
            ed_off=offsets.ed_off,
            penalty_off=penalty_off,
            current_kwh=3.0,
        )

        assert a_ub.shape == (3, offsets.n_vars)
        row = a_ub[-1]
        assert row[offsets.ec_off : offsets.ec_off + 3].tolist() == [-1.0, -1.0, 0.0]
        assert row[offsets.ed_off : offsets.ed_off + 3].tolist() == [1.0, 1.0, 0.0]
        assert row[penalty_off] == pytest.approx(-1.0)
        assert b_ub[-1] == pytest.approx(3.0 - 8.0)
        assert np.count_nonzero(row) == 5

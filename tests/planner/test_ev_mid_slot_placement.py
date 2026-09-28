"""Deferrable EV energy stays put when the planner runs mid-slot (issue #1117).

A managed EV's charge is tied to whole-amp commands: a full-width slot delivers
``amps × one_amp_dc``, while a partly elapsed live slot delivers ``amps ×
one_amp_dc × remaining_fraction`` — a finer lattice. A deadline need between two
full-slot lattice points left a residual that only the live slot could close,
and at the deadline penalty (about ten times the dearest import price per kWh)
closing it was worth more than any real price spread. At a 100 % target the
capacity row also blocked the lattice point above the need, so the residual
could not be closed any other way. Every mid-slot replan therefore pulled
deferrable energy into the dearer live slot, and the next boundary replan
moved it back.

The spec guarantee under test (``docs/planner-spec.md``, "Full-slot executable
deadline need"): the deadline need is the smallest whole-amp energy full-width
slots deliver exactly, and a deadline EV may overshoot its headroom by the
target-cap activation quantum. Deferrable EV placement then no longer depends
on how much of the live slot has elapsed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import cache
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.milp._ev_amp_lattice import (
    EvAmpSpec,
    full_slot_executable_shortfall_dc,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available, solve_milp
from custom_components.hsem.utils.phase_power import (
    EV_TOPOLOGY_SINGLE_PHASE,
    EV_TOPOLOGY_THREE_PHASE_BALANCED,
)
from custom_components.hsem.utils.prices import SlotPrice

_TZ = ZoneInfo("Europe/Copenhagen")
_SLOT_MINUTES = 15
_SLOT_HOURS = _SLOT_MINUTES / 60.0
#: The reporter's 17:30 live slot (2026-09-27 log, main at ``c22127d7``).
_LIVE_SLOT_START = datetime(2026, 9, 27, 17, 30, tzinfo=_TZ)
_CHARGER_EFF = 0.92
#: DC energy of one one-phase amp over a full 15-minute slot.
_ONE_AMP_DC = 230.0 * _SLOT_HOURS * _CHARGER_EFF / 1000.0
_MID_SLOT_MINUTES = (1, 4, 8, 13)

_pytestmark_scipy = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)


def _slot(index: int, *, import_price: float, pv_kwh: float) -> PlannedSlot:
    """Build one 15-minute slot; house load dips while the PV is up."""
    start = _LIVE_SLOT_START + timedelta(minutes=_SLOT_MINUTES * index)
    slot = PlannedSlot(
        start=start,
        end=start + timedelta(minutes=_SLOT_MINUTES),
        # The reporter's tariff: import = 1.25 × (export + 0.399).
        price=SlotPrice(
            import_price=import_price,
            export_price=max(import_price / 1.25 - 0.399, 0.0),
        ),
    )
    house_kwh = 0.209 if pv_kwh > 0.0 else 0.430
    slot.avg_house_consumption_kwh = house_kwh
    slot.solcast_pv_estimate_kwh = pv_kwh
    slot.ev_planned_load_kwh = 0.0
    slot.ev_accounted_load_kwh = 0.0
    slot.ev_total_planned_load_kwh = 0.0
    slot.estimated_net_consumption_kwh = house_kwh - pv_kwh
    return slot


def _issue_slots() -> list[PlannedSlot]:
    """Return the 24-hour horizon from the issue's evidence table.

    The live slot imports at 1.79, the evening peak at 2.10, and tomorrow's
    12:00-15:00 window at 1.53 with a 0.103 kWh PV surplus per slot.
    """
    slots = []
    for i in range(96):
        start = _LIVE_SLOT_START + timedelta(minutes=_SLOT_MINUTES * i)
        tomorrow = start.day == 28
        if i == 0:
            price, pv_kwh = 1.79, 0.13
        elif tomorrow and 12 <= start.hour < 15:
            price, pv_kwh = 1.53, 0.312
        elif 17 <= start.hour < 21:
            price, pv_kwh = 2.10, 0.0
        else:
            price, pv_kwh = 1.95, 0.312 if tomorrow and 9 <= start.hour < 16 else 0.0
        slots.append(_slot(i, import_price=price, pv_kwh=pv_kwh))
    return slots


def _ev(
    *,
    need_dc: float,
    deadline_slot: int,
    topology: str = EV_TOPOLOGY_THREE_PHASE_BALANCED,
    rated_kw: float = 11.04,
    headroom_above_target: float = 0.0,
) -> EVConfig:
    """Return a managed EV *need_dc* short of its target (capacity by default)."""
    return EVConfig(
        enabled=True,
        initial_soc_kwh=86.5 - need_dc,
        target_kwh=86.5,
        capacity_kwh=86.5 + headroom_above_target,
        max_charge_per_slot=rated_kw * _SLOT_HOURS * _CHARGER_EFF,
        charger_efficiency=_CHARGER_EFF,
        charger_min_power_w=1380.0,
        deadline_slot=deadline_slot,
        charger_phase_topology=topology,
    )


def _solve(
    slots: list[PlannedSlot], ev: EVConfig, minutes: int
) -> tuple[list[PlannedSlot], dict]:
    """Solve with ``now`` *minutes* (plus three seconds) into the live slot.

    The battery, discount and replacement price match the reporter's
    ``[milp] solve_milp`` line.
    """
    now = _LIVE_SLOT_START + timedelta(minutes=minutes, seconds=3)
    result = solve_milp(
        slots,
        now,
        current_kwh=14.25,
        usable_kwh=14.25,
        max_charge_per_slot=1.225,
        max_discharge_per_slot=1.25,
        cycle_cost_per_kwh=0.093,
        charge_efficiency_pct=98.0,
        discharge_efficiency_pct=98.0,
        time_discount_rate=0.995,
        replacement_price_per_kwh=3.4,
        min_export_price=0.03,
        battery_export_min_price=0.093,
        ev_configs=[ev],
    )
    assert result is not None
    return result


def _ev_commands(out: list[PlannedSlot]) -> list[tuple[int, float]]:
    """Return ``(slot index, charger watts)`` for every slot that charges."""
    return [
        (i, slot.ev_charger_calculated_power)
        for i, slot in enumerate(out)
        if slot.ev_total_planned_load_kwh > 1e-6
    ]


@cache
def _issue_plan(minutes: int) -> tuple[list[PlannedSlot], dict]:
    """Solve the issue scenario once per elapsed-minutes value."""
    slots = _issue_slots()
    deadline = datetime(2026, 9, 28, 17, 0, tzinfo=_TZ)
    deadline_slot = max(i for i, s in enumerate(slots) if s.end <= deadline)
    return _solve(slots, _ev(need_dc=0.465, deadline_slot=deadline_slot), minutes)


# ---------------------------------------------------------------------------
# The reported scenario
# ---------------------------------------------------------------------------


@_pytestmark_scipy
@pytest.mark.parametrize("minutes", _MID_SLOT_MINUTES)
def test_mid_slot_replan_keeps_the_boundary_ev_plan(minutes: int) -> None:
    """A mid-slot replan publishes exactly the slot-boundary EV commands."""
    boundary, _ = _issue_plan(0)
    mid_slot, _ = _issue_plan(minutes)

    assert _ev_commands(mid_slot) == _ev_commands(boundary)


@_pytestmark_scipy
@pytest.mark.parametrize("minutes", (0, *_MID_SLOT_MINUTES))
def test_deferrable_ev_energy_waits_for_the_cheaper_pv_window(minutes: int) -> None:
    """The dearer live slot never charges; tomorrow's PV window does."""
    out, diag = _issue_plan(minutes)

    assert out[0].ev_total_planned_load_kwh == pytest.approx(0.0)
    assert out[0].ev_charger_calculated_power == pytest.approx(0.0)
    charging = _ev_commands(out)
    assert len(charging) == 1
    start = out[charging[0][0]].start
    assert (start.day, 12 <= start.hour < 15) == (28, True)
    # 6.3.x has no phase switching: a balanced three-phase charger's smallest
    # executable point is 6 A on all three phases (main: 9 A one-phase).
    assert charging[0][1] == pytest.approx(6 * 230.0 * 3)
    assert diag["ev"]["ev0"]["total_dc_kwh"] == pytest.approx(
        18 * _ONE_AMP_DC, abs=1e-4
    )
    assert diag["ev"]["ev0"]["deadline_met"] is True


@_pytestmark_scipy
def test_issue_plan_keeps_every_slot_in_energy_balance() -> None:
    """Every future slot satisfies the MILP site balance with the EV load.

    ``grid_import − grid_export = house − pv + ev_ac + charged/η − discharged·η``
    (no curtailment: every export price in the fixture is positive).
    """
    out, _ = _issue_plan(8)
    for slot in out:
        balance = (
            slot.avg_house_consumption_kwh
            - slot.solcast_pv_estimate_kwh
            + slot.ev_total_planned_load_kwh
            + slot.batteries_charged_kwh / 0.98
            - slot.batteries_discharged_kwh * 0.98
        )
        assert slot.grid_import_kwh - slot.grid_export_kwh == pytest.approx(
            balance, abs=5e-3
        ), slot.start


# ---------------------------------------------------------------------------
# Placement is independent of the elapsed minutes for every charger shape
# ---------------------------------------------------------------------------

#: Live slot 1.8, a two-slot PV window at 1.4 (slots 12-13), 2.2 elsewhere.
_SWEEP_PRICES = (1.8, *([2.2] * 11), 1.4, 1.4, *([2.2] * 10))


def _sweep_slots(prices: tuple[float, ...] = _SWEEP_PRICES) -> list[PlannedSlot]:
    """Return a six-hour horizon with PV only in the cheap window."""
    return [
        _slot(i, import_price=price, pv_kwh=0.31 if price < 1.5 else 0.0)
        for i, price in enumerate(prices)
    ]


@_pytestmark_scipy
@pytest.mark.parametrize(
    ("topology", "rated_kw"),
    [
        (EV_TOPOLOGY_SINGLE_PHASE, 7.36),
        (EV_TOPOLOGY_THREE_PHASE_BALANCED, 11.04),
        (EV_TOPOLOGY_THREE_PHASE_BALANCED, 11.04),
    ],
)
@pytest.mark.parametrize("need_dc", [0.12, 0.465, 1.3])
@pytest.mark.parametrize("headroom_above_target", [0.0, 20.0])
def test_placement_ignores_elapsed_minutes(
    topology: str, rated_kw: float, need_dc: float, headroom_above_target: float
) -> None:
    """Needs below, between and above the startup minimum, at and below capacity.

    On ``main`` before the fix most of these combinations charged in the live
    slot at one or more of the sampled minutes.
    """
    ev = _ev(
        need_dc=need_dc,
        deadline_slot=20,
        topology=topology,
        rated_kw=rated_kw,
        headroom_above_target=headroom_above_target,
    )
    boundary, _ = _solve(_sweep_slots(), ev, 0)
    for minutes in _MID_SLOT_MINUTES:
        out, diag = _solve(_sweep_slots(), ev, minutes)
        assert _ev_commands(out) == _ev_commands(boundary), minutes
        assert out[0].ev_total_planned_load_kwh == pytest.approx(0.0), minutes
        assert diag["ev"]["ev0"]["deadline_met"] is True, minutes


@_pytestmark_scipy
def test_capacity_overshoot_stays_within_one_activation_quantum() -> None:
    """A target at capacity overshoots the headroom by less than the startup step.

    3-phase 6 A over a full slot delivers 6 × 690 V × 0.25 h × 0.92 = 0.952 kWh
    DC, far above a 0.12 kWh need: the car ends the charge itself when full.
    """
    ev = _ev(need_dc=0.12, deadline_slot=20, topology=EV_TOPOLOGY_THREE_PHASE_BALANCED)
    _, diag = _solve(_sweep_slots(), ev, 8)
    activation_quantum_dc = 6 * 690.0 * _SLOT_HOURS * _CHARGER_EFF / 1000.0

    total_dc = diag["ev"]["ev0"]["total_dc_kwh"]
    assert 0.12 <= total_dc <= activation_quantum_dc + 1e-4


# ---------------------------------------------------------------------------
# The live slot still charges when it should
# ---------------------------------------------------------------------------


@_pytestmark_scipy
@pytest.mark.parametrize("minutes", (0, 1, 4, 8))
def test_a_genuinely_cheaper_live_slot_still_charges(minutes: int) -> None:
    """At 1.2 against a 1.4 PV window, the live slot wins on price.

    The fix removes the live slot's rounding advantage, not the slot: while
    it has the minutes left to deliver the need, it charges.
    """
    prices = (1.2, *_SWEEP_PRICES[1:])
    out, diag = _solve(
        _sweep_slots(prices), _ev(need_dc=0.465, deadline_slot=20), minutes
    )

    assert _ev_commands(out)[0][0] == 0
    assert out[0].ev_total_planned_load_kwh > 0.4
    assert diag["ev"]["ev0"]["deadline_met"] is True


@_pytestmark_scipy
@pytest.mark.parametrize("minutes", (1, 4, 8))
def test_a_deadline_inside_the_live_slot_still_charges_now(minutes: int) -> None:
    """With no full-width slot before the deadline the need is not snapped (#845)."""
    ev = _ev(need_dc=0.2, deadline_slot=0, headroom_above_target=10.0)
    out, diag = _solve(_sweep_slots(), ev, minutes)

    assert [i for i, _ in _ev_commands(out)] == [0]
    assert diag["ev"]["ev0"]["total_dc_kwh"] >= 0.2
    assert diag["ev"]["ev0"]["deadline_met"] is True


# ---------------------------------------------------------------------------
# full_slot_executable_shortfall_dc
# ---------------------------------------------------------------------------


def _spec(*, minimum: int = 6, rated: int = 32, managed: bool = True) -> EvAmpSpec:
    """Return a one-phase amp-lattice spec."""
    return EvAmpSpec(
        ev_idx=0,
        managed=managed,
        minimum_current_a=minimum,
        rated_current_a=rated,
        runnable=managed,
        discharge_cap_kwh=0.0,
        needs_on=False,
        has_live_session=False,
    )


def _snap(
    need_dc: float,
    *,
    spec: EvAmpSpec | None = None,
    rated: int = 32,
    available_hours: tuple[float, ...] = (_SLOT_HOURS,) * 4,
    max_overshoot_dc: float = 6 * _ONE_AMP_DC,
) -> float | None:
    """Call the helper for a one-phase charger with *rated* amps."""
    ev = _ev(
        need_dc=need_dc,
        deadline_slot=len(available_hours) - 1,
        topology=EV_TOPOLOGY_SINGLE_PHASE,
        rated_kw=rated * 230.0 / 1000.0,
    )
    return full_slot_executable_shortfall_dc(
        ev,
        _spec(rated=rated) if spec is None else spec,
        shortfall_dc=need_dc,
        d=len(available_hours) - 1,
        available_slot_hours=np.asarray(available_hours),
        slot_hours=_SLOT_HOURS,
        max_overshoot_dc=max_overshoot_dc,
    )


@pytest.mark.parametrize(
    ("need_amp_slots", "rated", "expected_amp_slots"),
    [
        (8.79, 32, 9),  # the reporter's 0.465 kWh: up to the next whole amp
        (8.0, 32, 8),  # already on the lattice: unchanged
        (2.27, 32, 6),  # below the startup minimum: up to 6 A
        (17.5, 16, 18),  # needs two slots: 2 × 6 ≤ 18 ≤ 2 × 16
        (10.5, 10, 12),  # 11 is not executable with a 6-10 A charger
    ],
)
def test_need_snaps_up_to_an_executable_full_slot_total(
    need_amp_slots: float, rated: int, expected_amp_slots: int
) -> None:
    """The need becomes the smallest total full slots can deliver exactly."""
    snapped = _snap(need_amp_slots * _ONE_AMP_DC, rated=rated)
    assert snapped == pytest.approx(expected_amp_slots * _ONE_AMP_DC)


def test_no_full_width_slot_before_the_deadline_keeps_the_need() -> None:
    """A deadline inside a partly elapsed live slot keeps its own lattice."""
    assert _snap(0.2, available_hours=(_SLOT_HOURS / 2,)) is None


def test_live_slot_does_not_count_as_a_full_slot() -> None:
    """Only the full-width slot counts: two slots of need do not fit in one."""
    need_dc = 40 * _ONE_AMP_DC  # two full 32 A slots, not one
    available = (_SLOT_HOURS / 2, _SLOT_HOURS)
    assert _snap(need_dc, available_hours=available) is None
    assert _snap(need_dc, available_hours=(_SLOT_HOURS,) * 2) == pytest.approx(need_dc)


def test_unmanaged_or_missing_spec_keeps_the_need() -> None:
    """A fixed-session EV has no amp lattice to snap to."""
    assert _snap(0.465, spec=_spec(managed=False)) is None
    ev = _ev(need_dc=0.465, deadline_slot=3)
    assert (
        full_slot_executable_shortfall_dc(
            ev,
            None,
            shortfall_dc=0.465,
            d=3,
            available_slot_hours=np.full(4, _SLOT_HOURS),
            slot_hours=_SLOT_HOURS,
            max_overshoot_dc=1.0,
        )
        is None
    )


def test_no_executable_total_within_the_overshoot_allowance_keeps_the_need() -> None:
    """Snapping never asks for more than the target-cap row allows."""
    assert _snap(8.5 * _ONE_AMP_DC, max_overshoot_dc=0.25 * _ONE_AMP_DC) is None


def test_need_beyond_what_the_window_can_deliver_keeps_the_need() -> None:
    """An unreachable need is left to the deadline penalty and escalation."""
    assert _snap(130 * _ONE_AMP_DC) is None


def test_non_positive_need_or_slot_width_keeps_the_need() -> None:
    """Degenerate inputs never snap."""
    assert _snap(0.0) is None
    ev = _ev(need_dc=0.465, deadline_slot=0)
    assert (
        full_slot_executable_shortfall_dc(
            ev,
            _spec(),
            shortfall_dc=0.465,
            d=0,
            available_slot_hours=np.zeros(1),
            slot_hours=0.0,
            max_overshoot_dc=1.0,
        )
        is None
    )

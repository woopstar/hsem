"""Tests for the current-slot EV charger power hold (issue #957).

Both the baseline EV planner path and the MILP write-out derive the
*current* slot's target power as allocated energy ÷ remaining slot time,
re-derived from the live clock on every solve. As the remaining time
collapses toward its floor near a slot's end, the ratio degenerates into
"run at rated power to deliver a trickle of energy in a fraction of a
second" — a value that can then stay published past the slot's actual end,
because the coordinator re-solves far more often than once per slot.

``_hold_current_slot_ev_power`` fixes this by capturing the current slot's
rate once — the first time the slot is seen as current, or the first time
its allocation goes from zero to non-zero (a session starting mid-slot) —
and holding it for the rest of the slot. Only a genuine plan change (a new
current slot, or the slot's allocation being retracted to zero) moves the
published value.

Test classes
------------
TestHoldCapturesOnce       — a slot re-solved multiple times mid-duration
                              keeps returning the same held rate
TestHoldDoesNotSpike       — a slot with only seconds remaining must not
                              spike, once a rate is already held
TestHoldSlotBoundary       — allocation transitions from positive to zero
                              (and vice versa) across a slot boundary
TestHoldSessionStartsMidSlot — a session starting mid-slot captures the
                              correct one-time rate for the time that
                              genuinely remains
TestHoldWiringThroughPlanner — PlannerInput/PlannerOutput round trip
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.engine_ev import _hold_current_slot_ev_power

_TZ = UTC
_SLOT_START = datetime(2024, 6, 15, 12, 30, tzinfo=_TZ)
_SLOT_END = datetime(2024, 6, 15, 12, 45, tzinfo=_TZ)
_NEXT_SLOT_END = datetime(2024, 6, 15, 13, 0, tzinfo=_TZ)


def _slots(*, current_power_w: float, next_power_w: float = 0.0) -> list[PlannedSlot]:
    """Return a two-slot list: the 12:30-12:45 slot and the 12:45-13:00 slot."""
    return [
        PlannedSlot(
            start=_SLOT_START,
            end=_SLOT_END,
            ev_charger_calculated_power=current_power_w,
        ),
        PlannedSlot(
            start=_SLOT_END,
            end=_NEXT_SLOT_END,
            ev_charger_calculated_power=next_power_w,
        ),
    ]


class TestHoldCapturesOnce:
    """A slot re-solved multiple times mid-duration returns the same held rate."""

    def test_repeated_resolve_holds_first_captured_rate(self):
        """Three re-solves within the same slot all return the first rate.

        The first solve (15 min left) is a fresh capture at ~730 W. Later
        re-solves recompute a wildly different "fresh" value (simulating
        the degenerate tail-of-slot blowup) — the hold must ignore it.
        """
        now1 = _SLOT_START  # slot just became current, full 15 min left
        slots1 = _slots(current_power_w=730.0)
        held_start, held_w = _hold_current_slot_ev_power(
            slots1, now1, None, 0.0, second=False
        )
        assert held_start == _SLOT_START
        assert held_w == 730.0
        assert slots1[0].ev_charger_calculated_power == 730.0

        # Second re-solve, 10 minutes later, same slot. The "fresh" value
        # this solve produced (as if freshly recomputed from a shrinking
        # clock) is wildly different — must be ignored in favour of the hold.
        now2 = _SLOT_START + timedelta(minutes=10)
        slots2 = _slots(current_power_w=9999.0)
        held_start2, held_w2 = _hold_current_slot_ev_power(
            slots2, now2, held_start, held_w, second=False
        )
        assert held_start2 == _SLOT_START
        assert held_w2 == 730.0
        assert slots2[0].ev_charger_calculated_power == 730.0

        # Third re-solve, seconds before the slot ends.
        now3 = _SLOT_END - timedelta(seconds=2)
        slots3 = _slots(current_power_w=11040.0)
        held_start3, held_w3 = _hold_current_slot_ev_power(
            slots3, now3, held_start2, held_w2, second=False
        )
        assert held_start3 == _SLOT_START
        assert held_w3 == 730.0
        assert slots3[0].ev_charger_calculated_power == 730.0


class TestHoldDoesNotSpike:
    """A slot with only seconds remaining must not spike once already held."""

    def test_seconds_remaining_does_not_republish_spike(self):
        """Held at 730 W early in the slot; a few seconds before the slot
        ends the raw (pre-hold) value has degenerated to rated max — the
        published value must stay at the held rate, not the spike."""
        held_start, held_w = _hold_current_slot_ev_power(
            _slots(current_power_w=730.0), _SLOT_START, None, 0.0, second=False
        )
        assert held_w == 730.0

        now_tail = _SLOT_END - timedelta(seconds=4)
        slots_tail = _slots(current_power_w=11040.0)  # degenerate rated-max spike
        _, tail_w = _hold_current_slot_ev_power(
            slots_tail, now_tail, held_start, held_w, second=False
        )
        assert tail_w == 730.0
        assert slots_tail[0].ev_charger_calculated_power == 730.0

    def test_fresh_capture_with_seconds_remaining_uses_the_fresh_value_once(self):
        """If nothing was held yet and only seconds remain, the first
        capture takes whatever was already (safely) computed upstream —
        this function does not itself compute a rate, it only decides
        whether to hold or accept the fresh one."""
        now_tail = _SLOT_END - timedelta(seconds=4)
        slots_tail = _slots(current_power_w=250.0)
        held_start, held_w = _hold_current_slot_ev_power(
            slots_tail, now_tail, None, 0.0, second=False
        )
        assert held_start == _SLOT_START
        assert held_w == 250.0


class TestHoldSlotBoundary:
    """Allocation transitions from positive to zero (and vice versa)."""

    def test_new_slot_with_zero_allocation_clears_the_hold(self):
        """Crossing into a new slot the plan does not want to charge:
        the held state must reset to (None, 0.0), not carry the old rate."""
        now_new_slot = _SLOT_END + timedelta(seconds=3)
        current_slots = [
            PlannedSlot(
                start=_SLOT_END, end=_NEXT_SLOT_END, ev_charger_calculated_power=0.0
            )
        ]
        held_start, held_w = _hold_current_slot_ev_power(
            current_slots, now_new_slot, _SLOT_START, 730.0, second=False
        )
        assert held_start is None
        assert held_w == 0.0
        assert current_slots[0].ev_charger_calculated_power == 0.0

    def test_new_slot_with_nonzero_allocation_captures_fresh(self):
        """Crossing into a new slot the plan DOES want to charge: a fresh
        rate is captured for the new slot, not the old held rate."""
        now_new_slot = _SLOT_END + timedelta(seconds=3)
        current_slots = [
            PlannedSlot(
                start=_SLOT_END, end=_NEXT_SLOT_END, ev_charger_calculated_power=2747.0
            )
        ]
        held_start, held_w = _hold_current_slot_ev_power(
            current_slots, now_new_slot, _SLOT_START, 730.0, second=False
        )
        assert held_start == _SLOT_END
        assert held_w == 2747.0

    def test_retraction_mid_slot_zeroes_immediately(self):
        """The plan retracts the current slot's charge mid-slot (e.g. the
        MILP re-ranks battery ahead of the EV) — must publish zero right
        away, never resurrect the previously held non-zero command."""
        now_mid = _SLOT_START + timedelta(minutes=7)
        slots = _slots(current_power_w=0.0)
        held_start, held_w = _hold_current_slot_ev_power(
            slots, now_mid, _SLOT_START, 730.0, second=False
        )
        assert held_start is None
        assert held_w == 0.0
        assert slots[0].ev_charger_calculated_power == 0.0

    def test_reselection_after_retraction_captures_fresh(self):
        """After a retraction cleared the hold, the slot becomes selected
        again later in the same slot — must capture fresh, not silently
        stay at zero and not resurrect the pre-retraction rate."""
        now_later = _SLOT_START + timedelta(minutes=12)
        slots = _slots(current_power_w=500.0)
        held_start, held_w = _hold_current_slot_ev_power(
            slots, now_later, None, 0.0, second=False
        )
        assert held_start == _SLOT_START
        assert held_w == 500.0


class TestHoldSessionStartsMidSlot:
    """A session starting mid-slot captures the correct one-time rate."""

    def test_mid_slot_session_start_uses_actual_remaining_time_rate(self):
        """Smart charging enabled 10 minutes into a 15-min slot: nothing
        was held before, so the fresh value computed for the remaining 5
        minutes is captured as-is (not the full-slot-width value)."""
        now_mid_start = _SLOT_START + timedelta(minutes=10)
        slots = _slots(current_power_w=9360.0)  # sized for 5 min remaining
        held_start, held_w = _hold_current_slot_ev_power(
            slots, now_mid_start, None, 0.0, second=False
        )
        assert held_start == _SLOT_START
        assert held_w == 9360.0

        # A later re-solve in the same slot must hold that captured rate.
        now_later = _SLOT_START + timedelta(minutes=13)
        slots_later = _slots(current_power_w=99999.0)
        _, later_w = _hold_current_slot_ev_power(
            slots_later, now_later, held_start, held_w, second=False
        )
        assert later_w == 9360.0


class TestHoldSecondEv:
    """The second EV's hold state is tracked independently of the primary."""

    def test_second_ev_uses_its_own_attribute_and_state(self):
        slots = [
            PlannedSlot(
                start=_SLOT_START,
                end=_SLOT_END,
                ev_charger_calculated_power=730.0,
                ev_second_charger_calculated_power=1500.0,
            )
        ]
        held_start, held_w = _hold_current_slot_ev_power(
            slots, _SLOT_START, None, 0.0, second=True
        )
        assert held_start == _SLOT_START
        assert held_w == 1500.0
        # Primary field is untouched by the second-EV call.
        assert slots[0].ev_charger_calculated_power == 730.0


class TestHoldNoCurrentSlot:
    """No matching current slot returns a cleared hold without raising."""

    def test_now_outside_all_slots_returns_none(self):
        slots = _slots(current_power_w=730.0)
        held_start, held_w = _hold_current_slot_ev_power(
            slots, _SLOT_START - timedelta(hours=1), _SLOT_START, 730.0, second=False
        )
        assert held_start is None
        assert held_w == 0.0


# ---------------------------------------------------------------------------
# TestHoldWiringThroughPlanner — PlannerInput/PlannerOutput round trip
# ---------------------------------------------------------------------------


def _make_planner_input(
    now_iso: str,
    *,
    ev_held_slot_start: datetime | None = None,
    ev_held_power_w: float = 0.0,
) -> PlannerInput:
    """Build a minimal PlannerInput with EV planned load enabled."""
    now = datetime.fromisoformat(now_iso)
    deadline = now + timedelta(hours=8)

    prices = [
        PricePoint(hour=h, import_price=0.10, export_price=0.05) for h in range(24)
    ]
    pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
    averages = [
        HourlyConsumptionAverage(
            hour=h, avg_1d=1.0, avg_3d=1.0, avg_7d=1.0, avg_14d=1.0
        )
        for h in range(24)
    ]

    return PlannerInput(
        now_iso=now_iso,
        interval_minutes=60,
        interval_length_hours=24,
        battery_soc_pct=50.0,
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=90.0,
        battery_max_charge_power_w=5000.0,
        battery_max_discharge_power_w=5000.0,
        battery_charge_efficiency_pct=95.0,
        battery_discharge_efficiency_pct=95.0,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=averages,
        price_points=prices,
        solcast_slots=pv,
        ev_planned_load_enabled=True,
        ev_planned_load_connected=True,
        ev_planned_load_smart_charging_enabled=True,
        ev_planned_load_current_soc_pct=50.0,
        ev_planned_load_target_soc_pct=80.0,
        ev_planned_load_battery_capacity_kwh=77.0,
        ev_planned_load_charger_power_kw=11.0,
        ev_planned_load_charger_efficiency_pct=100.0,
        ev_planned_load_deadline=deadline,
        ev_held_slot_start=ev_held_slot_start,
        ev_held_power_w=ev_held_power_w,
    )


class TestHoldWiringThroughPlanner:
    """PlannerInput → run_planner → PlannerOutput carries the hold state."""

    def test_output_reports_held_state_for_the_current_slot(self):
        """A fresh solve with nothing held returns held state that matches
        the current slot's published command."""
        now_iso = "2024-06-15T06:05:00+00:00"
        now = datetime.fromisoformat(now_iso)
        out = run_planner(_make_planner_input(now_iso))

        current = next((s for s in out.slots if s.start <= now < s.end), None)
        assert current is not None
        if current.ev_charger_calculated_power > 1e-9:
            assert out.ev_held_slot_start == current.start
            assert out.ev_held_power_w == current.ev_charger_calculated_power
        else:
            assert out.ev_held_slot_start is None
            assert out.ev_held_power_w == 0.0

    def test_held_input_overrides_freshly_computed_value(self):
        """Feeding back a synthetic held rate for the still-current slot
        makes the planner publish that exact rate, not a freshly derived
        one — proving the coordinator → PlannerInput → engine wiring is
        live, not just the output side."""
        now_iso = "2024-06-15T06:05:00+00:00"
        now = datetime.fromisoformat(now_iso)
        baseline_out = run_planner(_make_planner_input(now_iso))
        current = next((s for s in baseline_out.slots if s.start <= now < s.end), None)
        assert current is not None
        assert current.ev_charger_calculated_power > 1e-9, (
            "Test setup expects the current slot to be charging the EV; "
            "adjust the fixture if this assumption changes."
        )

        synthetic_w = current.ev_charger_calculated_power + 1234.0
        held_out = run_planner(
            _make_planner_input(
                now_iso,
                ev_held_slot_start=current.start,
                ev_held_power_w=synthetic_w,
            )
        )
        held_current = next((s for s in held_out.slots if s.start <= now < s.end), None)
        assert held_current is not None
        assert held_current.ev_charger_calculated_power == synthetic_w
        assert held_out.ev_held_power_w == synthetic_w

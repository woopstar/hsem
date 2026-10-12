"""Tests for the hindsight score of EV charging timing (issue #1271).

Three things are pinned: that the oracle places a shiftable load where it is
cheapest and delivers exactly its energy, what counts as a session whose
energy may be moved, and what the three hindsight runs (plug-and-charge,
oracle, joint oracle) say about a window.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.planner.hindsight_ev import (
    ChargerSlot,
    EvSession,
    ev_sessions,
    score_ev,
)
from custom_components.hsem.planner.hindsight_oracle import (
    HindsightBattery,
    HindsightSlot,
    ShiftableLoad,
    solve_hindsight_oracle,
)
from custom_components.hsem.planner.hindsight_scoring import (
    RealizedSlot,
    RealizedWindow,
    SiteLimits,
    realized_window,
    score_window,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_START = datetime(2026, 6, 9, 10, 0, tzinfo=UTC)
_STARTS = [_START + timedelta(hours=index) for index in range(24)]
_HOURS = [1.0] * 24
#: No battery to speak of: what is left is the EV alone.
_NO_BATTERY = HindsightBattery(
    min_stored_kwh=0.0, max_stored_kwh=0.0, max_charge_kw=0.0, max_discharge_kw=0.0
)
_SITE = SiteLimits(
    rated_kwh=10.0,
    min_soc_pct=5.0,
    max_soc_pct=100.0,
    max_charge_kw=5.0,
    max_discharge_kw=5.0,
    charge_efficiency=0.97,
    discharge_efficiency=0.97,
    battery_export_enabled=False,
)
#: Six ordinary hours, a dear evening, two ordinary hours, a cheap night.
_PRICES = [1.2] * 6 + [2.5] * 4 + [1.2] * 2 + [0.5] * 6 + [1.2] * 6
#: The car is plugged in from the evening (slot 6) to the morning (slot 19).
_PLUGGED = range(6, 20)


def _charger(
    ev_at: dict[int, float],
    *,
    plugged: range = _PLUGGED,
    deadline: datetime | None = None,
) -> list[ChargerSlot | None]:
    """Return a charger's day: plugged in over *plugged*, managed, 7 kW."""
    return [
        ChargerSlot(
            kwh=ev_at.get(index, 0.0),
            connected=index in plugged,
            managed=True,
            deadline=deadline,
            max_kw=7.0,
        )
        for index in range(24)
    ]


def _window(ev_at: dict[int, float], site: SiteLimits = _SITE) -> RealizedWindow:
    """Return a window with a 0.6 kWh house load and the EV charged as given."""
    slots = [
        RealizedSlot(
            hours=1.0,
            grid_import_kwh=0.6 + ev_at.get(index, 0.0),
            grid_export_kwh=0.0,
            charged_kwh=0.0,
            discharged_kwh=0.0,
            import_price=price,
            export_price=price - 0.3,
            pv_kwh=0.0,
        )
        for index, price in enumerate(_PRICES)
    ]
    return realized_window(slots, 4.0, site)


class TestShiftableLoad:
    """The oracle places a load itself, and delivers exactly its energy."""

    def _slots(self, prices: list[float]) -> list[HindsightSlot]:
        return [
            HindsightSlot(
                hours=1.0, net_load_kwh=0.5, import_price=price, export_price=0.0
            )
            for price in prices
        ]

    def test_the_load_goes_where_it_is_cheapest(self) -> None:
        slots = self._slots([3.0, 1.0, 2.0, 0.5])
        load = ShiftableLoad(energy_kwh=6.0, max_kwh=(4.0, 4.0, 4.0, 4.0))

        run = solve_hindsight_oracle(slots, _NO_BATTERY, 0.0, 0.0, shiftable=[load])

        assert run is not None
        assert run.shifted_kwh == (pytest.approx((0.0, 2.0, 0.0, 4.0), abs=1e-6),)
        assert sum(run.shifted_kwh[0]) == pytest.approx(6.0)
        # House load at every price, plus 4 kWh at 0.5 and 2 kWh at 1.0.
        assert run.cost == pytest.approx(0.5 * 6.5 + 4.0 * 0.5 + 2.0 * 1.0)

    def test_the_load_only_draws_where_it_may(self) -> None:
        """A cap of zero is a slot the car was not plugged in."""
        slots = self._slots([3.0, 1.0, 2.0, 0.5])
        load = ShiftableLoad(energy_kwh=3.0, max_kwh=(4.0, 0.0, 4.0, 0.0))

        run = solve_hindsight_oracle(slots, _NO_BATTERY, 0.0, 0.0, shiftable=[load])

        assert run is not None
        assert run.shifted_kwh[0] == pytest.approx((0.0, 0.0, 3.0, 0.0), abs=1e-6)

    def test_two_loads_are_placed_independently(self) -> None:
        slots = self._slots([3.0, 1.0, 2.0, 0.5])
        early = ShiftableLoad(energy_kwh=2.0, max_kwh=(4.0, 4.0, 0.0, 0.0))
        late = ShiftableLoad(energy_kwh=2.0, max_kwh=(0.0, 0.0, 4.0, 4.0))

        run = solve_hindsight_oracle(
            slots, _NO_BATTERY, 0.0, 0.0, shiftable=[early, late]
        )

        assert run is not None
        assert run.shifted_kwh[0] == pytest.approx((0.0, 2.0, 0.0, 0.0), abs=1e-6)
        assert run.shifted_kwh[1] == pytest.approx((0.0, 0.0, 0.0, 2.0), abs=1e-6)

    def test_a_load_that_does_not_fit_has_no_oracle(self) -> None:
        slots = self._slots([3.0, 1.0])
        load = ShiftableLoad(energy_kwh=9.0, max_kwh=(4.0, 4.0))

        assert (
            solve_hindsight_oracle(slots, _NO_BATTERY, 0.0, 0.0, shiftable=[load])
            is None
        )

    def test_the_import_limit_spreads_the_load(self) -> None:
        """A fuse of 3 kWh per slot leaves 2.5 for the car."""
        slots = self._slots([3.0, 1.0, 2.0, 0.5])
        load = ShiftableLoad(energy_kwh=5.0, max_kwh=(4.0, 4.0, 4.0, 4.0))

        run = solve_hindsight_oracle(
            slots,
            _NO_BATTERY,
            0.0,
            0.0,
            shiftable=[load],
            max_grid_import_kwh=[3.0] * 4,
        )

        assert run is not None
        assert run.shifted_kwh[0] == pytest.approx((0.0, 2.5, 0.0, 2.5), abs=1e-6)
        assert max(run.grid_import_kwh) <= 3.0 + 1e-9

    def test_without_loads_nothing_changes(self) -> None:
        slots = self._slots([3.0, 1.0, 2.0, 0.5])

        run = solve_hindsight_oracle(slots, _NO_BATTERY, 0.0, 0.0)

        assert run is not None
        assert run.shifted_kwh == ()
        assert run.cost == pytest.approx(0.5 * 6.5)


class TestSessions:
    """A session is a run of slots whose EV energy may be moved."""

    def test_the_plugged_in_stretch_is_one_session(self) -> None:
        (session,) = ev_sessions(_charger({6: 7.0, 7: 3.0}), _STARTS, _HOURS)

        assert session.energy_kwh == pytest.approx(10.0)
        assert session.realized_kwh[6] == pytest.approx(7.0)
        assert [index for index, cap in enumerate(session.max_kwh) if cap > 0] == list(
            _PLUGGED
        )
        assert set(session.max_kwh[6:20]) == {7.0}

    @pytest.mark.parametrize(
        "broken",
        [
            {"connected": False},
            {"connected": None},
            {"managed": False},
            {"kwh": None},
        ],
    )
    def test_a_slot_that_cannot_move_ends_the_session(
        self, broken: dict[str, object]
    ) -> None:
        """Unplugged, unknown, unmanaged or unobserved: energy there is fixed."""
        slots = _charger({6: 7.0, 7: 3.0, 14: 2.0, 15: 2.0})
        slot = slots[10]
        assert slot is not None
        slots[10] = replace(slot, **broken)  # type: ignore[arg-type]

        first, second = ev_sessions(slots, _STARTS, _HOURS)

        assert first.energy_kwh == pytest.approx(10.0)
        assert first.max_kwh[10] == pytest.approx(0.0)
        assert first.max_kwh[11] == pytest.approx(0.0)
        assert second.energy_kwh == pytest.approx(4.0)
        assert second.max_kwh[9] == pytest.approx(0.0)

    def test_a_missing_record_ends_the_session_too(self) -> None:
        slots = _charger({6: 7.0, 7: 3.0, 14: 2.0, 15: 2.0})
        slots[10] = None

        assert len(ev_sessions(slots, _STARTS, _HOURS)) == 2

    def test_without_a_connected_sensor_there_is_no_session(self) -> None:
        slots = [
            None if slot is None else replace(slot, connected=None)
            for slot in _charger({6: 7.0, 7: 3.0})
        ]

        assert ev_sessions(slots, _STARTS, _HOURS) == []

    def test_an_unmanaged_charger_has_no_session(self) -> None:
        """Smart charging off or "force charge now": the user decided when."""
        slots = [
            None if slot is None else replace(slot, managed=False)
            for slot in _charger({6: 7.0, 7: 3.0})
        ]

        assert ev_sessions(slots, _STARTS, _HOURS) == []

    def test_a_session_that_delivered_nothing_is_left_out(self) -> None:
        assert ev_sessions(_charger({}), _STARTS, _HOURS) == []

    def test_a_single_slot_has_nowhere_to_move_to(self) -> None:
        slots = _charger({6: 7.0})
        for index in range(7, 20):
            slot = slots[index]
            assert slot is not None
            slots[index] = replace(slot, connected=False)

        assert ev_sessions(slots, _STARTS, _HOURS) == []

    def test_the_cap_is_the_configured_charger_power(self) -> None:
        slots = [
            None if slot is None else replace(slot, max_kw=11.0)
            for slot in _charger({6: 7.0, 7: 3.0})
        ]

        (session,) = ev_sessions(slots, _STARTS, _HOURS)

        assert session.max_kwh[8] == pytest.approx(11.0)

    def test_without_a_configured_power_the_sessions_own_peak_is_the_cap(self) -> None:
        slots = [
            None if slot is None else replace(slot, max_kw=None)
            for slot in _charger({6: 6.2, 7: 3.0})
        ]

        (session,) = ev_sessions(slots, _STARTS, _HOURS)

        assert session.max_kwh[8] == pytest.approx(6.2)

    def test_the_cap_is_never_below_what_was_delivered(self) -> None:
        """A reading above the configured power stays feasible."""
        (session,) = ev_sessions(_charger({6: 7.4, 7: 3.0}), _STARTS, _HOURS)

        assert session.max_kwh[6] == pytest.approx(7.4)
        assert session.max_kwh[7] == pytest.approx(7.0)

    def test_quarter_hour_slots_scale_the_cap(self) -> None:
        starts = [_START + timedelta(minutes=15 * index) for index in range(8)]
        slots: list[ChargerSlot | None] = [
            ChargerSlot(kwh=1.0, connected=True, managed=True, max_kw=7.0)
            for _ in range(8)
        ]

        (session,) = ev_sessions(slots, starts, [0.25] * 8)

        assert set(session.max_kwh) == {1.75}


class TestDeadline:
    """Energy delivered before the deadline may not be moved past it."""

    def test_a_deadline_inside_the_session_cuts_it(self) -> None:
        deadline = _STARTS[14]
        slots = _charger({6: 7.0, 7: 3.0, 16: 2.0, 17: 2.0}, deadline=deadline)

        before, after = ev_sessions(slots, _STARTS, _HOURS)

        assert before.energy_kwh == pytest.approx(10.0)
        assert [i for i, cap in enumerate(before.max_kwh) if cap > 0] == list(
            range(6, 14)
        )
        assert after.energy_kwh == pytest.approx(4.0)
        assert [i for i, cap in enumerate(after.max_kwh) if cap > 0] == list(
            range(14, 20)
        )

    def test_the_slot_the_deadline_falls_in_belongs_to_the_later_part(self) -> None:
        """Half past: the slot from 14 to 15 cannot take energy due by 14:30."""
        deadline = _STARTS[14] + timedelta(minutes=30)
        slots = _charger({6: 7.0, 7: 3.0, 16: 2.0, 17: 2.0}, deadline=deadline)

        before, _after = ev_sessions(slots, _STARTS, _HOURS)

        assert before.max_kwh[13] > 0.0
        assert before.max_kwh[14] == pytest.approx(0.0)

    def test_a_deadline_after_the_session_does_not_cut_it(self) -> None:
        slots = _charger({6: 7.0, 7: 3.0}, deadline=_STARTS[22])

        assert len(ev_sessions(slots, _STARTS, _HOURS)) == 1

    def test_a_deadline_before_the_session_does_not_cut_it(self) -> None:
        slots = _charger({6: 7.0, 7: 3.0}, deadline=_STARTS[2])

        assert len(ev_sessions(slots, _STARTS, _HOURS)) == 1

    def test_the_joint_oracle_respects_the_deadline(self) -> None:
        """The cheap night comes after the deadline: the energy cannot go there."""
        ev_at = {6: 7.0, 7: 7.0, 8: 6.0}
        window = _window(ev_at)
        free = score_ev(window, ev_sessions(_charger(ev_at), _STARTS, _HOURS))
        bound = score_ev(
            window,
            ev_sessions(_charger(ev_at, deadline=_STARTS[12]), _STARTS, _HOURS),
        )

        assert free.joint_oracle_cost is not None
        assert bound.joint_oracle_cost is not None
        assert bound.joint_oracle_cost > free.joint_oracle_cost + 5.0


class TestPlugAndCharge:
    def test_full_power_from_the_first_slot(self) -> None:
        session = EvSession(
            realized_kwh=(0.0, 0.0, 3.0, 0.0, 7.0, 6.0),
            max_kwh=(0.0, 7.0, 7.0, 7.0, 7.0, 7.0),
        )

        assert session.plug_and_charge_kwh() == pytest.approx(
            (0.0, 7.0, 7.0, 2.0, 0.0, 0.0)
        )

    def test_it_delivers_the_same_energy(self) -> None:
        (session,) = ev_sessions(_charger({12: 7.0, 13: 7.0, 14: 6.0}), _STARTS, _HOURS)

        schedule = session.plug_and_charge_kwh()

        assert sum(schedule) == pytest.approx(session.energy_kwh)
        assert schedule[6:9] == pytest.approx((7.0, 7.0, 6.0))


def _efficiency(ev_at: dict[int, float], plugged: range = _PLUGGED) -> float:
    """Return the EV charging efficiency of a day charged as given."""
    window = _window(ev_at)
    stage1 = score_window(window)
    ev = score_ev(
        window, ev_sessions(_charger(ev_at, plugged=plugged), _STARTS, _HOURS)
    )
    assert stage1 is not None
    assert ev.joint_oracle_cost is not None and ev.plug_and_charge_cost is not None
    missed = stage1.oracle_cost - ev.joint_oracle_cost
    potential = ev.plug_and_charge_cost - ev.joint_oracle_cost
    assert missed >= -1e-6
    return 1.0 - missed / potential


class TestWhatTheEvNumbersMean:
    def test_charging_at_plug_in_through_the_dear_evening_captures_nothing(
        self,
    ) -> None:
        assert _efficiency({6: 7.0, 7: 7.0, 8: 6.0}) == pytest.approx(0.0, abs=1e-6)

    def test_charging_in_the_cheap_night_captures_everything(self) -> None:
        assert _efficiency({12: 7.0, 13: 7.0, 14: 6.0}) == pytest.approx(1.0, abs=1e-6)

    def test_half_in_the_evening_and_half_at_night_is_in_between(self) -> None:
        share = _efficiency({6: 7.0, 7: 3.0, 13: 7.0, 14: 3.0})

        assert 0.3 < share < 0.9

    def test_worse_than_plug_and_charge_is_negative(self) -> None:
        """Plugged in two ordinary hours before the peak, charged at the peak."""
        share = _efficiency({6: 7.0, 7: 7.0, 8: 6.0}, plugged=range(4, 20))

        # Plugging in and charging would have put 14 kWh into the ordinary
        # hours; waiting for the peak was worse than no control at all.
        assert share < -0.2

    def test_the_parts_add_up_with_the_battery_and_solar_losses(self) -> None:
        """Realized − joint oracle = battery loss + solar loss + EV missed."""
        ev_at = {6: 7.0, 7: 3.0, 13: 7.0, 14: 3.0}
        window = _window(ev_at)
        stage = score_window(window, breakdown=True)
        ev = score_ev(window, ev_sessions(_charger(ev_at), _STARTS, _HOURS))

        assert stage is not None and ev.joint_oracle_cost is not None
        assert stage.battery_loss is not None and stage.solar_loss is not None
        ev_missed = stage.oracle_cost - ev.joint_oracle_cost
        assert stage.realized_cost - ev.joint_oracle_cost == pytest.approx(
            stage.battery_loss + stage.solar_loss + ev_missed
        )
        assert ev_missed > 1.0

    def test_the_costs_are_ordered(self) -> None:
        ev_at = {6: 7.0, 7: 3.0, 13: 7.0, 14: 3.0}
        window = _window(ev_at)
        stage = score_window(window)
        ev = score_ev(window, ev_sessions(_charger(ev_at), _STARTS, _HOURS))

        assert stage is not None
        assert ev.joint_oracle_cost is not None and ev.plug_and_charge_cost is not None
        assert ev.joint_oracle_cost <= stage.oracle_cost + 1e-6
        assert ev.joint_oracle_cost <= ev.plug_and_charge_cost + 1e-6
        assert ev.shiftable_kwh == pytest.approx(20.0)

    def test_no_session_means_nothing_is_solved(self) -> None:
        ev = score_ev(_window({}), [])

        assert ev.shiftable_kwh == pytest.approx(0.0)
        assert ev.joint_oracle_cost is None
        assert ev.plug_and_charge_cost is None

    def test_two_chargers_are_scored_together(self) -> None:
        """They share the grid and the battery; the energy of both may move."""
        first = {6: 7.0, 7: 3.0}
        second = {8: 5.0, 9: 5.0}
        ev_at = {**first, **second}
        window = _window(ev_at)
        sessions = [
            *ev_sessions(_charger(first), _STARTS, _HOURS),
            *ev_sessions(_charger(second), _STARTS, _HOURS),
        ]

        ev = score_ev(window, sessions)

        assert len(sessions) == 2
        assert ev.shiftable_kwh == pytest.approx(20.0)
        assert ev.joint_oracle_cost is not None

    def test_a_fuse_does_not_take_the_baseline_away(self) -> None:
        """Plug-and-charge at 7 kW on a 4 kW fuse still has a cost."""
        ev_at = {12: 3.0, 13: 3.0, 14: 3.0, 15: 3.0}
        window = _window(ev_at, replace(_SITE, max_grid_import_kw=4.0))

        ev = score_ev(window, ev_sessions(_charger(ev_at), _STARTS, _HOURS))

        assert ev.plug_and_charge_cost is not None
        assert ev.joint_oracle_cost is not None
        assert ev.plug_and_charge_cost > ev.joint_oracle_cost

    def test_a_solve_out_of_time_leaves_the_costs_unknown(self) -> None:
        ev_at = {6: 7.0, 7: 3.0}

        ev = score_ev(
            _window(ev_at),
            ev_sessions(_charger(ev_at), _STARTS, _HOURS),
            time_limit_s=0.0,
        )

        assert ev.shiftable_kwh == pytest.approx(10.0)
        assert ev.joint_oracle_cost is None
        assert ev.plug_and_charge_cost is None

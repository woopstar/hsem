"""Tests for the wait-mode self-consumption reserve (issue #914).

``calculate_required_battery_for_plan()`` derives the reserve used to gate
``batteries_wait_mode`` self-consumption from the *selected* plan's own
simulated SoC trajectory (``slot.estimated_battery_capacity_kwh`` /
``slot.batteries_charged_kwh``), instead of scanning raw forecast net
consumption until the first slot with *any* PV surplus
(``calculate_required_battery_until_solar``, unaffected by this change and
covered by a regression test below).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.discharge_scheduler import (
    WAIT_MODE_RESERVE_DECAY_HOURS,
    calculate_required_battery_for_plan,
    calculate_required_battery_until_solar,
)
from custom_components.hsem.utils.prices import SlotPrice

_NOW = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)


def _slot(
    offset_hours: float,
    *,
    estimated_net_consumption_kwh: float = 0.0,
    estimated_battery_capacity_kwh: float = 0.0,
    batteries_charged_kwh: float = 0.0,
    batteries_discharged_kwh: float = 0.0,
) -> PlannedSlot:
    """Build a minimal already-simulated PlannedSlot anchored at NOW + offset."""
    start = _NOW + timedelta(hours=offset_hours)
    return PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=0.20, export_price=0.05),
        solcast_pv_estimate_kwh=0.0,
        avg_house_consumption_kwh=0.5,
        estimated_net_consumption_kwh=estimated_net_consumption_kwh,
        estimated_battery_capacity_kwh=estimated_battery_capacity_kwh,
        batteries_charged_kwh=batteries_charged_kwh,
        batteries_discharged_kwh=batteries_discharged_kwh,
    )


class TestSmallForecastSurplusDoesNotEndReserveEarly:
    """A short/small forecast PV-surplus slot must not truncate the reserve.

    Regression scenario from issue #914: at ~0.5 kWh reserve remaining
    (as ``calculate_required_battery_until_solar`` would compute, stopping
    at the first forecast-surplus slot), the plan itself does not actually
    charge from that small surplus (``batteries_charged_kwh == 0``) and
    still expects a much larger discharge later during an expensive period.
    The new reserve must protect the full later dip. Both slots sit inside
    the issue #954 time-decay window so the protected dip is still visible
    (a scenario placed beyond the window would decay to 0 regardless of
    depth — see ``TestWaitModeReserveTimeDecay``).
    """

    def test_reserve_protects_later_discharge_past_small_surplus(self) -> None:
        slots = [
            # Small forecast PV surplus (negative net consumption) that the
            # plan does NOT actually charge from — old function would have
            # stopped scanning here.
            _slot(
                0.25,
                estimated_net_consumption_kwh=-0.2,
                estimated_battery_capacity_kwh=1.8,
                batteries_charged_kwh=0.0,
            ),
            # Later, deeper planned discharge during an expensive period —
            # 1h away, inside the decay window, so ~half the full 1.7 kWh
            # dip is protected right now (issue #954).
            _slot(
                1.0,
                estimated_net_consumption_kwh=1.5,
                estimated_battery_capacity_kwh=0.3,
                batteries_discharged_kwh=1.5,
            ),
        ]

        reserve = calculate_required_battery_for_plan(slots, _NOW, current_capacity=2.0)
        assert reserve is not None

        # full dip = 2.0 - 0.3 = 1.7 kWh; decayed by (1 - 1.0/2.0) = 0.5.
        assert reserve == 0.85

        old_reserve = calculate_required_battery_until_solar(
            slots, _NOW, usable_capacity=2.0, discharge_buffer_pct=0.0
        )
        assert reserve > old_reserve


class TestReliableRechargeStopsTheScan:
    """A genuine planned charge (grid/solar) ends the reserve requirement."""

    def test_reserve_stops_at_first_actual_planned_charge(self) -> None:
        slots = [
            # Small dip before the plan actually recharges.
            _slot(
                0.5,
                estimated_battery_capacity_kwh=1.5,
            ),
            # Genuine planned recharge — the plan relies on this, not on
            # today's stored energy, to cover anything past this point.
            # 1h away, inside the issue #954 decay window.
            _slot(
                1.0,
                estimated_battery_capacity_kwh=3.0,
                batteries_charged_kwh=1.5,
            ),
            # Deep dip AFTER the recharge — must not inflate the reserve
            # computed for "now", since it will be served by the recharge.
            _slot(
                5,
                estimated_battery_capacity_kwh=0.1,
                batteries_discharged_kwh=2.9,
            ),
        ]

        reserve = calculate_required_battery_for_plan(slots, _NOW, current_capacity=2.0)

        # full dip = 2.0 - 1.5 = 0.5 kWh; decayed by (1 - 1.0/2.0) = 0.5.
        assert reserve == 0.25


class TestReserveStopsAtFirstScheduledAction:
    """The scan stops at the plan's next discharge, not just its next charge
    (issue #942 follow-up).

    Regression scenario reported against #949: a run of Wait-mode slots
    (flat ``estimated_battery_capacity_kwh``, no discharge modelled — see
    ``soc_simulation.py``) precedes a small scheduled discharge, which is in
    turn followed by further discharge slots before the next genuine charge.
    Before this fix, the scan accumulated through *all* of those later
    discharge slots, inflating the reserve computed right now to nearly the
    full current capacity and starving the SoC-floor discharge-cap gate of
    any surplus for the entire Wait span. The reserve must now protect only
    the plan's very next committed action — timed inside the issue #954
    decay window here so the protected dip is still visible against the
    (larger) cumulative overnight total a pre-#950 scan would have produced.
    """

    def test_reserve_does_not_accumulate_past_the_next_discharge(self) -> None:
        slots = [
            # Short Wait-mode run: capacity stays flat, nothing committed yet.
            _slot(0.25, estimated_battery_capacity_kwh=9.5),
            _slot(0.5, estimated_battery_capacity_kwh=9.5),
            # First scheduled discharge — small, matches the issue's 0.139 kWh.
            _slot(
                0.75,
                estimated_battery_capacity_kwh=9.361,
                batteries_discharged_kwh=0.139,
            ),
            # Further overnight discharge slots before any recharge — must
            # NOT inflate the reserve computed above.
            _slot(
                1.0,
                estimated_battery_capacity_kwh=4.0,
                batteries_discharged_kwh=5.361,
            ),
            _slot(
                2.0,
                estimated_battery_capacity_kwh=0.2,
                batteries_discharged_kwh=3.8,
            ),
        ]

        reserve = calculate_required_battery_for_plan(slots, _NOW, current_capacity=9.5)

        # Full dip through the first discharge slot only (9.5 -> 9.361 =
        # 0.139 kWh), decayed by (1 - 0.75/2.0) = 0.625 -> 0.087. Far below
        # what a pre-#950 cumulative-overnight-total scan would have given
        # (9.5 - 0.2 = 9.3 kWh, i.e. nearly the entire current capacity).
        assert reserve == 0.087

    def test_reserve_still_stops_at_charge_when_charge_comes_first(self) -> None:
        """A charge slot before any discharge still ends the scan (unchanged)."""
        slots = [
            _slot(1, estimated_battery_capacity_kwh=9.5),
            _slot(
                2,
                estimated_battery_capacity_kwh=9.8,
                batteries_charged_kwh=0.3,
            ),
            # Deep discharge after the recharge must not inflate "now"'s reserve.
            _slot(
                6,
                estimated_battery_capacity_kwh=0.1,
                batteries_discharged_kwh=9.7,
            ),
        ]

        reserve = calculate_required_battery_for_plan(slots, _NOW, current_capacity=9.5)

        assert reserve == 0.0


class TestWaitModeReserveTimeDecay:
    """The reserve ramps up as the next committed action approaches (issue #954).

    Reported scenario: a large discharge scheduled hours away (e.g. an
    evening peak) locked up nearly the entire current capacity for
    self-consumption immediately, even though the battery wasn't needed for
    hours — 100% SoC, reserve well below capacity, yet the applier still
    wrote 0 W. Confirmed directly: a discharge 5h away consumed 5.9 of a
    6.4 kWh capacity as "reserve" right away. The full energy needed for the
    next action is now only protected once it's imminent; farther out, the
    reserve decays linearly to 0 over ``WAIT_MODE_RESERVE_DECAY_HOURS``.
    """

    def test_action_beyond_decay_window_has_zero_reserve(self) -> None:
        """A discharge needing most of the battery, 5h away, protects nothing yet."""
        slots = [
            _slot(1, estimated_battery_capacity_kwh=6.4),
            _slot(3, estimated_battery_capacity_kwh=6.4),
            _slot(
                5,
                estimated_battery_capacity_kwh=0.5,
                batteries_discharged_kwh=5.9,
            ),
        ]

        reserve = calculate_required_battery_for_plan(slots, _NOW, current_capacity=6.4)

        assert reserve == 0.0

    def test_action_at_decay_window_boundary_has_zero_reserve(self) -> None:
        """Exactly at the decay window boundary, the reserve is still 0."""
        slots = [
            _slot(
                WAIT_MODE_RESERVE_DECAY_HOURS,
                estimated_battery_capacity_kwh=0.5,
                batteries_discharged_kwh=5.9,
            ),
        ]

        reserve = calculate_required_battery_for_plan(slots, _NOW, current_capacity=6.4)

        assert reserve == 0.0

    def test_imminent_action_has_full_reserve(self) -> None:
        """An action starting now (or already in progress) is fully protected."""
        slots = [
            _slot(
                0.0,
                estimated_battery_capacity_kwh=0.5,
                batteries_discharged_kwh=5.9,
            ),
        ]

        reserve = calculate_required_battery_for_plan(slots, _NOW, current_capacity=6.4)

        assert reserve == 5.9

    def test_reserve_ramps_linearly_inside_the_decay_window(self) -> None:
        """Halfway through the decay window, half the full reserve is protected."""
        halfway = WAIT_MODE_RESERVE_DECAY_HOURS / 2
        slots = [
            _slot(
                halfway,
                estimated_battery_capacity_kwh=0.5,
                batteries_discharged_kwh=5.9,
            ),
        ]

        reserve = calculate_required_battery_for_plan(slots, _NOW, current_capacity=6.4)

        assert reserve == pytest.approx(5.9 / 2, abs=0.01)

    def test_decay_also_applies_to_a_far_future_charge(self) -> None:
        """A far-future recharge decays the same way as a far-future discharge."""
        slots = [
            _slot(1, estimated_battery_capacity_kwh=1.0),
            _slot(5, estimated_battery_capacity_kwh=6.4, batteries_charged_kwh=5.4),
        ]

        reserve = calculate_required_battery_for_plan(slots, _NOW, current_capacity=6.4)

        assert reserve == 0.0

    def test_no_future_action_in_horizon_is_unaffected_by_decay(self) -> None:
        """No committed action anywhere -> decay never engages (unchanged)."""
        slots = [
            _slot(1, estimated_battery_capacity_kwh=1.0),
            _slot(10, estimated_battery_capacity_kwh=0.4),
        ]

        reserve = calculate_required_battery_for_plan(slots, _NOW, current_capacity=2.0)

        assert reserve == 1.6


class TestNoFutureRechargeInHorizon:
    """No planned charge anywhere in the horizon forces (near) full reserve."""

    def test_reserve_covers_entire_horizon_when_no_recharge_planned(self) -> None:
        slots = [
            _slot(1, estimated_battery_capacity_kwh=1.0),
            _slot(2, estimated_battery_capacity_kwh=0.4),
            _slot(3, estimated_battery_capacity_kwh=0.0),
        ]

        reserve = calculate_required_battery_for_plan(slots, _NOW, current_capacity=2.0)

        # The whole current capacity must be protected — this naturally
        # forces strict Wait behaviour downstream (surplus <= 0), without
        # needing a special-cased fallback for this scenario.
        assert reserve == 2.0

    def test_none_returned_when_no_future_slots_exist(self) -> None:
        """No future slots at all -> undefined reserve -> fall back to strict Wait."""
        # offset=-2 -> start = now-2h, end = now-1h, i.e. entirely in the past.
        past_slot = _slot(-2, estimated_battery_capacity_kwh=1.0)

        reserve = calculate_required_battery_for_plan(
            [past_slot], _NOW, current_capacity=2.0
        )

        assert reserve is None


class TestApplyExcessExportRegressionUnaffected:
    """calculate_required_battery_until_solar() behaviour must be unchanged."""

    def test_until_solar_still_stops_at_first_surplus_slot(self) -> None:
        """The original until-solar scan still stops at the first surplus slot,
        regardless of what the plan actually schedules there — this is the
        exact (unchanged) behaviour ``apply_excess_export`` continues to rely
        on."""
        slots = [
            _slot(1, estimated_net_consumption_kwh=0.6),
            _slot(2, estimated_net_consumption_kwh=-0.1),
            _slot(3, estimated_net_consumption_kwh=1.5),
        ]

        result = calculate_required_battery_until_solar(
            slots, _NOW, usable_capacity=5.0, discharge_buffer_pct=0.0
        )

        # Only the first (pre-surplus) slot's positive net consumption is
        # accumulated; the later 1.5 kWh slot past the surplus is ignored.
        assert result == 0.6

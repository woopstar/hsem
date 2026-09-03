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

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.discharge_scheduler import (
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
    The new reserve must protect the full later dip.
    """

    def test_reserve_protects_later_discharge_past_small_surplus(self) -> None:
        slots = [
            # Small forecast PV surplus (negative net consumption) that the
            # plan does NOT actually charge from — old function would have
            # stopped scanning here.
            _slot(
                1,
                estimated_net_consumption_kwh=-0.2,
                estimated_battery_capacity_kwh=1.8,
                batteries_charged_kwh=0.0,
            ),
            # Later, deeper planned discharge during an expensive period.
            _slot(
                3,
                estimated_net_consumption_kwh=1.5,
                estimated_battery_capacity_kwh=0.3,
                batteries_discharged_kwh=1.5,
            ),
        ]

        reserve = calculate_required_battery_for_plan(slots, _NOW, current_capacity=2.0)
        assert reserve is not None

        # Old function would only have reserved down to the first surplus
        # slot (~0.2 kWh dip); the new one must protect the deeper 1.7 kWh
        # dip the plan actually relies on before any real recharge.
        assert reserve == 1.7

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
                1,
                estimated_battery_capacity_kwh=1.5,
            ),
            # Genuine planned recharge — the plan relies on this, not on
            # today's stored energy, to cover anything past this point.
            _slot(
                2,
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

        assert reserve == 0.5


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

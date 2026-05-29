"""Tests for mid-hour / partial current-slot planner behavior (issue #377).

These tests verify how the planner handles ``now_iso`` values at different
positions within the current slot: at slot start, mid-slot, and near slot end.

Invariant 13 from ``docs/hsem-planner-spec.md`` requires that a partial
current slot uses only the remaining duration for energy and cost
calculations.  This is currently NOT implemented — the planner uses full
slot values for the in-progress slot.  This file documents the current
behaviour and marks the ideal behaviour as ``xfail``.

Acceptance criteria
-------------------
- Tests cover ``now_iso`` at slot start (00:00) — normal case.
- Tests cover ``now_iso`` halfway through a slot (00:30).
- Tests cover ``now_iso`` near slot end (00:55).
- Verification that load, PV, price, cost, and SoC do not over-count
  energy in a partially elapsed current slot.

All tests are synchronous and import nothing from Home Assistant's runtime.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planner_inputs import (
    HourlyConsumptionAverage,
    PlannerInput,
    PricePoint,
    SolcastSlot,
)
from custom_components.hsem.models.planner_outputs import PlannerOutput
from custom_components.hsem.planner import run_planner
from custom_components.hsem.utils.recommendations import Recommendations
from tests.planner.fixtures import make_summer_day_input

_TZ = ZoneInfo("Europe/Copenhagen")

# Hour-0 reference values from _HOUSE_CONSUMPTION and _SPOT_PRICES_SUMMER (fixtures.py)
_H0_CONSUMPTION = 0.4  # kWh — full-hour consumption for hour 0
_H0_PV = 0.0  # kWh — night time, no solar
_H0_IMPORT_PRICE = 0.08  # currency/kWh
_H0_EXPORT_PRICE = 0.06  # currency/kWh

# Battery constants (default fixture values)
_RATED_CAPACITY = 10.0  # kWh
_EOD_SOC = 10.0  # %
_USABLE = _RATED_CAPACITY * (100.0 - _EOD_SOC) / 100.0  # 9.0 kWh
_START_SOC_PCT = 50.0  # %
_START_CURRENT_KWH = min(
    _RATED_CAPACITY * (_START_SOC_PCT - _EOD_SOC) / 100.0, _USABLE
)  # 4.0 kWh


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_future_slot(result: PlannerOutput) -> Any:
    """Return the first slot not marked as TimePassed."""
    return next(
        s for s in result.slots if s.recommendation != Recommendations.TimePassed.value
    )


def _time_passed_count(result: PlannerOutput) -> int:
    """Count slots marked as TimePassed."""
    return sum(
        1 for s in result.slots if s.recommendation == Recommendations.TimePassed.value
    )


def _make_flat_uniform_input(
    *,
    now_iso: str = "2024-06-15T00:00:00+02:00",
    load_kwh_per_hour: float = 0.5,
    pv_kwh_per_hour: float = 0.0,
    import_price: float = 0.20,
    battery_soc_pct: float = 50.0,
    battery_rated_capacity_kwh: float = 10.0,
    battery_end_of_discharge_soc_pct: float = 10.0,
) -> PlannerInput:
    """Build a simple PlannerInput with flat prices, no schedules, and uniform load/PV.

    This avoids unwanted grid-charge or discharge-schedule interactions
    so that hand calculations for SoC and cost are straightforward.
    """
    prices = [
        PricePoint(hour=h, import_price=import_price, export_price=import_price * 0.25)
        for h in range(24)
    ]
    solar = [SolcastSlot(hour=h, pv_estimate=pv_kwh_per_hour) for h in range(24)]
    consumption = [
        HourlyConsumptionAverage(
            hour=h,
            avg_1d=load_kwh_per_hour,
            avg_3d=load_kwh_per_hour,
            avg_7d=load_kwh_per_hour,
            avg_14d=load_kwh_per_hour,
        )
        for h in range(24)
    ]
    return PlannerInput(
        now_iso=now_iso,
        interval_minutes=60,
        interval_length_hours=24,
        battery_soc_pct=battery_soc_pct,
        battery_rated_capacity_kwh=battery_rated_capacity_kwh,
        battery_end_of_discharge_soc_pct=battery_end_of_discharge_soc_pct,
        battery_max_soc_pct=100.0,
        battery_max_charge_power_w=5000.0,
        battery_purchase_price=0.0,
        battery_expected_cycles=6000,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=consumption,
        price_points=prices,
        solcast_slots=solar,
        battery_schedules=[],
        excess_export_enabled=False,
        excess_export_discharge_buffer_pct=10.0,
        excess_export_price_threshold=0.10,
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        house_power_includes_ev=True,
        is_read_only=True,
        time_discount_rate=1.0,
    )


# ===========================================================================
# 1. now_iso at slot start (00:00) — normal planning start
# ===========================================================================


class TestNowAtSlotStart:
    """Planning starts exactly at the beginning of a slot (00:00).

    This is the normal case — the full slot duration is available and no
    prorating is needed.  All tests in this class must pass.
    """

    def test_no_slots_marked_time_passed(self):
        """At 00:00, no slots should have elapsed."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:00:00+02:00")
        result = run_planner(inp)
        assert _time_passed_count(result) == 0

    def test_first_slot_boundaries(self):
        """The first slot starts at 00:00 and ends at 01:00."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:00:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        assert first.start == datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)
        assert first.end == datetime(2024, 6, 15, 1, 0, tzinfo=_TZ)

    def test_consumption_is_full_hour_value(self):
        """At slot start, consumption uses the full hourly value (0.4 kWh)."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:00:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        assert first.avg_house_consumption_kwh == pytest.approx(
            _H0_CONSUMPTION, abs=0.01
        )

    def test_pv_is_full_hour_value(self):
        """At slot start, PV uses the full hourly value (0.0 kWh at night)."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:00:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        assert first.solcast_pv_estimate_kwh == pytest.approx(_H0_PV, abs=0.01)

    def test_net_consumption_equals_load_minus_pv(self):
        """Net consumption = load - PV for the first slot."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:00:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        expected = round(_H0_CONSUMPTION - _H0_PV, 3)
        assert first.estimated_net_consumption_kwh == pytest.approx(expected, abs=0.01)

    def test_price_populated(self):
        """Prices are correctly populated for hour 0."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:00:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        assert first.price is not None
        assert first.price.import_price == pytest.approx(_H0_IMPORT_PRICE, abs=1e-6)
        assert first.price.export_price == pytest.approx(_H0_EXPORT_PRICE, abs=1e-6)

    def test_cost_calculated_from_net_and_import_price(self):
        """Estimated cost = net * import_price (when net >= 0)."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:00:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        expected_cost = round((_H0_CONSUMPTION - _H0_PV) * _H0_IMPORT_PRICE, 4)
        assert first.estimated_cost_currency == pytest.approx(expected_cost, abs=1e-4)

    def test_soc_starts_from_initial_capacity(self):
        """SoC simulation starts from the initial usable capacity.

        The SoC formula used by ``simulate_soc`` is absolute relative to rated
        capacity (not usable-range):
            absolute_kwh = cap + rated_kwh * eod_pct / 100
            soc_pct = absolute_kwh / rated_kwh * 100
        """
        inp = make_summer_day_input(
            now_iso="2024-06-15T00:00:00+02:00",
            battery_soc_pct=_START_SOC_PCT,
            battery_rated_capacity_kwh=_RATED_CAPACITY,
        )
        result = run_planner(inp)
        first = _first_future_slot(result)
        # Data-driven check: verify the SoC formula correctness
        cap = first.estimated_battery_capacity_kwh
        expected_soc = round(
            (cap + _RATED_CAPACITY * _EOD_SOC / 100) / _RATED_CAPACITY * 100, 2
        )
        assert first.estimated_battery_soc_pct == pytest.approx(expected_soc, abs=0.01)
        assert 0 <= first.estimated_battery_soc_pct <= 100

    def test_soc_bounded_across_all_slots(self):
        """SoC stays in [0, 100] for all slots."""
        inp = make_summer_day_input(
            now_iso="2024-06-15T00:00:00+02:00",
            battery_soc_pct=_START_SOC_PCT,
        )
        result = run_planner(inp)
        for slot in result.slots:
            assert 0 <= slot.estimated_battery_soc_pct <= 100


# ===========================================================================
# 2. now_iso at mid-slot (00:30) — 30 min remaining
# ===========================================================================


class TestNowAtMidSlot:
    """Planning starts half-way through the first slot (00:30).

    30 out of 60 minutes have elapsed.  The current slot (00:00–01:00) is
    NOT marked as TimePassed because ``slot.end > now``.

    Current behaviour (documented): the planner uses full slot values for
    load, PV, net consumption, and cost — the remaining duration is NOT
    factored in.

    Ideal behaviour (xfail): load, PV, net, and cost should be scaled to
    50 % of the full slot value.
    """

    def test_current_slot_not_marked_time_passed(self):
        """Slot 00:00–01:00 is NOT TimePassed at 00:30 (end=01:00 > 00:30)."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:30:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        assert first.start == datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)
        assert first.end == datetime(2024, 6, 15, 1, 0, tzinfo=_TZ)

    def test_no_slots_marked_time_passed_at_midnight_start(self):
        """At 00:30, no slots end before 00:30 (slot 0 ends at 01:00)."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:30:00+02:00")
        result = run_planner(inp)
        # Slot 0 ends at 01:00 > 00:30, so nothing is TimePassed
        assert _time_passed_count(result) == 0

    def test_consumption_is_full_slot_value(self):
        """Current behaviour: full-slot consumption (0.4 kWh), not prorated."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:30:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        assert first.avg_house_consumption_kwh == pytest.approx(
            _H0_CONSUMPTION, abs=0.01
        )

    def test_pv_is_full_slot_value(self):
        """Current behaviour: full-slot PV (0.0 kWh), not prorated."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:30:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        assert first.solcast_pv_estimate_kwh == pytest.approx(_H0_PV, abs=0.01)

    def test_net_consumption_is_full_slot_value(self):
        """Current behaviour: full-slot net consumption."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:30:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        expected_net = round(_H0_CONSUMPTION - _H0_PV, 3)
        assert first.estimated_net_consumption_kwh == pytest.approx(
            expected_net, abs=0.01
        )

    def test_price_populated(self):
        """Prices are populated for the current slot even when mid-slot."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:30:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        assert first.price is not None
        assert first.price.import_price == pytest.approx(_H0_IMPORT_PRICE, abs=1e-6)

    def test_cost_is_full_slot_value(self):
        """Current behaviour: cost uses full-slot net consumption."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:30:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        expected_cost = round((_H0_CONSUMPTION - _H0_PV) * _H0_IMPORT_PRICE, 4)
        assert first.estimated_cost_currency == pytest.approx(expected_cost, abs=1e-4)

    def test_soc_starts_from_initial_capacity(self):
        """SoC simulation starts from the initial battery capacity.

        The current slot path in ``populate_battery_capacity`` uses
        ``current_capacity`` (not ``previous_capacity``) when
        ``slot_start <= now < slot_end``.

        Verified via the absolute-SoC formula from ``simulate_soc``:
            absolute_kwh = cap + rated_kwh * eod_pct / 100
            soc_pct = absolute_kwh / rated_kwh * 100
        """
        inp = make_summer_day_input(
            now_iso="2024-06-15T00:30:00+02:00",
            battery_soc_pct=_START_SOC_PCT,
            battery_rated_capacity_kwh=_RATED_CAPACITY,
        )
        result = run_planner(inp)
        first = _first_future_slot(result)
        # Data-driven check: SoC formula must be internally consistent
        cap = first.estimated_battery_capacity_kwh
        expected_soc = round(
            (cap + _RATED_CAPACITY * _EOD_SOC / 100) / _RATED_CAPACITY * 100, 2
        )
        assert first.estimated_battery_soc_pct == pytest.approx(expected_soc, abs=0.01)
        assert 0 <= first.estimated_battery_soc_pct <= 100

    def test_soc_bounded_across_all_slots(self):
        """SoC stays in [0, 100] for all slots with mid-slot planning."""
        inp = make_summer_day_input(
            now_iso="2024-06-15T00:30:00+02:00",
            battery_soc_pct=_START_SOC_PCT,
        )
        result = run_planner(inp)
        for slot in result.slots:
            if slot.recommendation == Recommendations.TimePassed.value:
                continue
            assert 0 <= slot.estimated_battery_soc_pct <= 100

    def test_24_slots_generated(self):
        """Still exactly 24 slots — mid-slot start does not change count."""
        inp = make_summer_day_input(
            now_iso="2024-06-15T00:30:00+02:00",
        )
        result = run_planner(inp)
        assert len(result.slots) == 24

    def test_slot_boundaries_correct(self):
        """Slot boundaries are correct — slots run 00:00–01:00, 01:00–02:00, etc."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:30:00+02:00")
        result = run_planner(inp)
        assert result.slots[0].start == datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)
        assert result.slots[0].end == datetime(2024, 6, 15, 1, 0, tzinfo=_TZ)
        assert result.slots[1].start == datetime(2024, 6, 15, 1, 0, tzinfo=_TZ)

    # ------------------------------------------------------------------
    # Ideal partial-slot behaviour (xfail — not yet implemented)
    # ------------------------------------------------------------------

    @pytest.mark.xfail(
        reason=(
            "Partial-slot duration not yet implemented. "
            "The planner uses full slot energy for the current in-progress slot "
            "rather than scaling to the remaining fraction. "
            "See hsem-planner-spec.md invariant 13."
        ),
        strict=True,
    )
    def test_consumption_should_be_prorated_to_remaining_duration(self):
        """At 00:30, consumption should be 50 % of the full value (0.2 kWh)."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:30:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        assert first.avg_house_consumption_kwh == pytest.approx(0.2, abs=0.01)

    @pytest.mark.xfail(
        reason=(
            "Partial-slot duration not yet implemented. "
            "See hsem-planner-spec.md invariant 13."
        ),
        strict=True,
    )
    def test_net_consumption_should_be_prorated(self):
        """At 00:30, net consumption should be 50 % of the full value (0.2 kWh)."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:30:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        assert first.estimated_net_consumption_kwh == pytest.approx(0.2, abs=0.01)

    @pytest.mark.xfail(
        reason=(
            "Partial-slot duration not yet implemented. "
            "See hsem-planner-spec.md invariant 13."
        ),
        strict=True,
    )
    def test_cost_should_be_prorated(self):
        """At 00:30, estimated cost should be 50 % of full (0.016)."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:30:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        expected = round(0.2 * _H0_IMPORT_PRICE, 4)  # 0.016
        assert first.estimated_cost_currency == pytest.approx(expected, abs=1e-4)

    @pytest.mark.xfail(
        reason=(
            "Partial-slot duration not yet implemented. "
            "See hsem-planner-spec.md invariant 13."
        ),
        strict=True,
    )
    def test_soc_should_reflect_partial_net_consumption(self):
        """At 00:30, SoC should reflect only 0.2 kWh net consumption."""
        inp = make_summer_day_input(
            now_iso="2024-06-15T00:30:00+02:00",
            battery_soc_pct=_START_SOC_PCT,
            battery_rated_capacity_kwh=_RATED_CAPACITY,
        )
        result = run_planner(inp)
        first = _first_future_slot(result)
        # Remaining net = 0.2 kWh; cap = max(4.0 - 0.2, 0) = 3.8 kWh
        # SoC = 3.8 / 9.0 * 100 = 42.22 %
        expected_soc = round((_START_CURRENT_KWH - 0.2) / _USABLE * 100, 2)
        assert first.estimated_battery_soc_pct == pytest.approx(expected_soc, abs=0.5)


# ===========================================================================
# 3. now_iso near slot end (00:55) — 5 min remaining
# ===========================================================================


class TestNowNearSlotEnd:
    """Planning starts near the end of the first slot (00:55).

    Only 5 out of 60 minutes remain.  The error from using full-slot values
    vs. remaining-duration scaling is largest here.

    Current behaviour (documented): full-slot values used.
    Ideal behaviour (xfail): ~8.3 % of full slot values (5/60).
    """

    def test_current_slot_not_marked_time_passed(self):
        """Slot 00:00–01:00 is NOT TimePassed at 00:55 (end=01:00 > 00:55)."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:55:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        assert first.start == datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)

    def test_no_slots_marked_time_passed(self):
        """At 00:55, no slots end before 00:55."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:55:00+02:00")
        result = run_planner(inp)
        assert _time_passed_count(result) == 0

    def test_consumption_is_full_slot_value(self):
        """Current behaviour: full-slot consumption even at 00:55."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:55:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        assert first.avg_house_consumption_kwh == pytest.approx(
            _H0_CONSUMPTION, abs=0.01
        )

    def test_cost_is_full_slot_value(self):
        """Current behaviour: full-slot cost even at 00:55."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:55:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        expected_cost = round((_H0_CONSUMPTION - _H0_PV) * _H0_IMPORT_PRICE, 4)
        assert first.estimated_cost_currency == pytest.approx(expected_cost, abs=1e-4)

    def test_soc_starts_from_initial_capacity(self):
        """SoC starts from initial capacity even near slot end.

        Verified via the absolute-SoC formula from ``simulate_soc``:
            absolute_kwh = cap + rated_kwh * eod_pct / 100
            soc_pct = absolute_kwh / rated_kwh * 100
        """
        inp = make_summer_day_input(
            now_iso="2024-06-15T00:55:00+02:00",
            battery_soc_pct=_START_SOC_PCT,
            battery_rated_capacity_kwh=_RATED_CAPACITY,
        )
        result = run_planner(inp)
        first = _first_future_slot(result)
        cap = first.estimated_battery_capacity_kwh
        expected_soc = round(
            (cap + _RATED_CAPACITY * _EOD_SOC / 100) / _RATED_CAPACITY * 100, 2
        )
        assert first.estimated_battery_soc_pct == pytest.approx(expected_soc, abs=0.01)

    def test_24_slots_generated(self):
        """Near-slot-end planning still produces 24 slots."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:55:00+02:00")
        result = run_planner(inp)
        assert len(result.slots) == 24

    # ------------------------------------------------------------------
    # Ideal partial-slot behaviour (xfail — not yet implemented)
    # ------------------------------------------------------------------

    @pytest.mark.xfail(
        reason=(
            "Partial-slot duration not yet implemented. "
            "See hsem-planner-spec.md invariant 13."
        ),
        strict=True,
    )
    def test_consumption_should_be_prorated_to_remaining(self):
        """At 00:55, consumption should be 5/60 ≈ 0.033 kWh."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:55:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        expected = round(_H0_CONSUMPTION * 5.0 / 60.0, 3)  # ~0.033
        assert first.avg_house_consumption_kwh == pytest.approx(expected, abs=0.005)

    @pytest.mark.xfail(
        reason=(
            "Partial-slot duration not yet implemented. "
            "See hsem-planner-spec.md invariant 13."
        ),
        strict=True,
    )
    def test_cost_should_be_prorated_to_remaining(self):
        """At 00:55, cost should be 5/60 of the full value ≈ 0.0027."""
        inp = make_summer_day_input(now_iso="2024-06-15T00:55:00+02:00")
        result = run_planner(inp)
        first = _first_future_slot(result)
        net = round(_H0_CONSUMPTION * 5.0 / 60.0, 3)
        expected_cost = round(net * _H0_IMPORT_PRICE, 4)
        assert first.estimated_cost_currency == pytest.approx(expected_cost, abs=1e-4)

    @pytest.mark.xfail(
        reason=(
            "Partial-slot duration not yet implemented. "
            "See hsem-planner-spec.md invariant 13."
        ),
        strict=True,
    )
    def test_soc_should_reflect_partial_net(self):
        """At 00:55, SoC should reflect only 5 min of net consumption ≈ 0.033 kWh."""
        inp = make_summer_day_input(
            now_iso="2024-06-15T00:55:00+02:00",
            battery_soc_pct=_START_SOC_PCT,
            battery_rated_capacity_kwh=_RATED_CAPACITY,
        )
        result = run_planner(inp)
        first = _first_future_slot(result)
        net_partial = round(_H0_CONSUMPTION * 5.0 / 60.0, 3)
        expected_soc = round((_START_CURRENT_KWH - net_partial) / _USABLE * 100, 2)
        assert first.estimated_battery_soc_pct == pytest.approx(expected_soc, abs=0.5)


# ===========================================================================
# 4. Cross-scenario comparisons — verify mid-slot vs. slot-start differences
# ===========================================================================


class TestCrossScenarioComparison:
    """Compare results across different ``now_iso`` values.

    These tests verify that mid-slot / near-end planning differs from
    slot-start planning in observable ways (or documents where they
    currently do not differ).
    """

    def test_consumption_identical_across_now_values(self):
        """Current behaviour: consumption is identical regardless of now_iso.

        Because the planner does not prorate, mid-slot and slot-start
        produce the same per-slot values for the current slot.
        """
        inp_start = make_summer_day_input(now_iso="2024-06-15T00:00:00+02:00")
        inp_mid = make_summer_day_input(now_iso="2024-06-15T00:30:00+02:00")

        result_start = run_planner(inp_start)
        result_mid = run_planner(inp_mid)

        h0_start = _first_future_slot(result_start)
        h0_mid = _first_future_slot(result_mid)

        assert h0_start.avg_house_consumption_kwh == pytest.approx(
            h0_mid.avg_house_consumption_kwh, abs=0.01
        )

    def test_net_consumption_identical_across_now_values(self):
        """Current behaviour: net consumption is identical regardless of now_iso."""
        inp_start = make_summer_day_input(now_iso="2024-06-15T00:00:00+02:00")
        inp_mid = make_summer_day_input(now_iso="2024-06-15T00:30:00+02:00")

        result_start = run_planner(inp_start)
        result_mid = run_planner(inp_mid)

        h0_start = _first_future_slot(result_start)
        h0_mid = _first_future_slot(result_mid)

        assert h0_start.estimated_net_consumption_kwh == pytest.approx(
            h0_mid.estimated_net_consumption_kwh, abs=0.01
        )

    def test_soc_differs_between_mid_and_start(self):
        """SoC differs because the planner simulates from the same starting capacity.

        Even though consumption is identical, the SoC after the current slot
        is the same regardless of now_iso (same net, same starting capacity).
        This test documents that partial-slot SoC does NOT currently differ.
        """
        inp_start = make_summer_day_input(
            now_iso="2024-06-15T00:00:00+02:00",
            battery_soc_pct=_START_SOC_PCT,
            battery_rated_capacity_kwh=_RATED_CAPACITY,
        )
        inp_mid = make_summer_day_input(
            now_iso="2024-06-15T00:30:00+02:00",
            battery_soc_pct=_START_SOC_PCT,
            battery_rated_capacity_kwh=_RATED_CAPACITY,
        )

        result_start = run_planner(inp_start)
        result_mid = run_planner(inp_mid)

        h0_start = _first_future_slot(result_start)
        h0_mid = _first_future_slot(result_mid)

        # Both should have the same SoC — no prorating yet
        assert h0_start.estimated_battery_soc_pct == pytest.approx(
            h0_mid.estimated_battery_soc_pct, abs=0.5
        )

    def test_all_scenarios_produce_24_slots(self):
        """All three now_iso values produce exactly 24 slots."""
        for now in (
            "2024-06-15T00:00:00+02:00",
            "2024-06-15T00:30:00+02:00",
            "2024-06-15T00:55:00+02:00",
        ):
            inp = make_summer_day_input(now_iso=now)
            result = run_planner(inp)
            assert len(result.slots) == 24, f"Failed for now={now}"

    def test_all_scenarios_have_no_missing_inputs(self):
        """All three now_iso values produce no missing inputs with full fixture."""
        for now in (
            "2024-06-15T00:00:00+02:00",
            "2024-06-15T00:30:00+02:00",
            "2024-06-15T00:55:00+02:00",
        ):
            inp = make_summer_day_input(now_iso=now)
            result = run_planner(inp)
            assert result.missing_inputs == [], f"Failed for now={now}"

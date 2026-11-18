"""Tests for the charge-timing term (issue #1420).

With a time discount and one export price, the plan used to export PV
surplus first and fill the battery from PV forecast for later in the day.
A forecast that came in short then left the battery empty at peak price.

Coverage
--------
- The helper formulas shared by the MILP objective and ``score_plan``.
- One export price: the battery fills before any PV is exported.
- A real export-price difference still decides the order.
- Energy balance and SoC bounds hold for the solved plan.
- ``score_plan`` reports the term, applies it to ``score`` only, and
  disables it without a discount.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.cost_function import CostWeights, score_plan
from custom_components.hsem.planner.cost_helpers import (
    charge_timing_cost,
    charge_timing_rate,
)
from custom_components.hsem.planner.milp_optimizer import solve_milp
from custom_components.hsem.utils.prices import SlotPrice

_TZ = ZoneInfo("Europe/Madrid")
_NOW = datetime(2026, 11, 13, 8, 0, tzinfo=_TZ)

_USABLE_KWH = 4.5
_START_KWH = 0.05
_CYCLE_COST = 0.02
_DISCOUNT = 0.995
_LOAD_KWH = 0.4
_EFFICIENCY = 0.97

#: PV per hour of day (kWh). The surplus over the day exceeds the battery.
_PV_BY_HOUR = {
    8: 0.2,
    9: 0.6,
    10: 1.0,
    11: 1.4,
    12: 1.6,
    13: 1.6,
    14: 1.3,
    15: 0.9,
    16: 0.4,
    17: 0.1,
}


def _import_price(hour: int) -> float:
    """Return the Spanish 2.0TD import price for an hour of day."""
    if 10 <= hour < 14 or 18 <= hour < 22:
        return 0.311
    if hour < 8:
        return 0.165
    return 0.197


def _make_slots(
    export_price_by_hour: dict[int, float] | None = None,
) -> list[PlannedSlot]:
    """Build 24 hourly slots from 08:00 with PV on the first day only."""
    slots: list[PlannedSlot] = []
    for i in range(24):
        start = _NOW + timedelta(hours=i)
        export_price = (export_price_by_hour or {}).get(start.hour, 0.035)
        slot = PlannedSlot(
            start=start,
            end=start + timedelta(hours=1),
            price=SlotPrice(
                import_price=_import_price(start.hour), export_price=export_price
            ),
        )
        pv_kwh = _PV_BY_HOUR.get(start.hour, 0.0) if i < 12 else 0.0
        slot.avg_house_consumption_kwh = _LOAD_KWH
        slot.solcast_pv_estimate_kwh = pv_kwh
        slot.ev_planned_load_kwh = 0.0
        slot.estimated_net_consumption_kwh = _LOAD_KWH - pv_kwh
        slots.append(slot)
    return slots


def _solve(slots: list[PlannedSlot]) -> list[PlannedSlot]:
    """Solve the MILP with the production time discount."""
    result = solve_milp(
        slots,
        _NOW,
        _START_KWH,
        _USABLE_KWH,
        2.5,
        2.5,
        cycle_cost_per_kwh=_CYCLE_COST,
        time_discount_rate=_DISCOUNT,
        replacement_price_per_kwh=0.165,
    )
    assert result is not None
    return result[0]


def _first_index(slots: list[PlannedSlot], field: str) -> int:
    """Return the first slot index with a material value in ``field``."""
    return next(i for i, s in enumerate(slots) if getattr(s, field) > 1e-3)


def _last_index(slots: list[PlannedSlot], field: str) -> int:
    """Return the last first-day slot index with a material value in ``field``."""
    return max(i for i, s in enumerate(slots[:12]) if getattr(s, field) > 1e-3)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_rate_exceeds_cycle_cost_plus_export_price() -> None:
    """The rate is above the break-even so an early charge wins a tie."""
    assert charge_timing_rate(0.02, 0.035) == pytest.approx(1.1 * 0.055)
    assert charge_timing_rate(0.02, 0.035) > 0.02 + 0.035


def test_rate_ignores_negative_inputs() -> None:
    """Negative prices and cycle costs never make the rate negative."""
    assert charge_timing_rate(-1.0, -0.5) == pytest.approx(0.0)
    assert charge_timing_rate(0.02, -0.5) == pytest.approx(1.1 * 0.02)


def test_cost_is_zero_without_discount_and_grows_with_time() -> None:
    """The term is zero for an undiscounted slot and larger for a later one."""
    assert charge_timing_cost(2.0, 0.06, 1.0) == pytest.approx(0.0)
    near = charge_timing_cost(2.0, 0.06, 0.995**1)
    far = charge_timing_cost(2.0, 0.06, 0.995**6)
    assert 0.0 < near < far
    assert far == pytest.approx(2.0 * 0.06 * (1.0 - 0.995**6))


# ---------------------------------------------------------------------------
# MILP behaviour
# ---------------------------------------------------------------------------


def test_flat_export_price_fills_battery_before_exporting() -> None:
    """With one export price the battery fills first and the sale comes last."""
    out = _solve(_make_slots())

    first_export = _first_index(out, "grid_export_kwh")
    last_charge = _last_index(out, "batteries_charged_kwh")
    assert first_export >= last_charge

    # The 09:00 and 10:00 surplus is stored, not sold.
    for slot in out[1:3]:
        assert slot.batteries_charged_kwh > 0.1
        assert slot.grid_export_kwh == pytest.approx(0.0, abs=2e-3)


def test_higher_early_export_price_still_sells_early() -> None:
    """A real export-price difference decides, not the timing term."""
    out = _solve(_make_slots({9: 0.20, 10: 0.20}))

    for slot in out[1:3]:
        assert slot.grid_export_kwh > 0.1
        assert slot.batteries_charged_kwh == pytest.approx(0.0, abs=2e-3)


def test_negative_late_export_price_defers_the_charge() -> None:
    """PV is sold while the price is positive and stored when it is negative."""
    # The negative-price surplus alone fills the battery.
    out = _solve(_make_slots(dict.fromkeys(range(11, 16), -0.05)))

    for slot in out[1:3]:
        assert slot.grid_export_kwh > 0.1
        assert slot.batteries_charged_kwh == pytest.approx(0.0, abs=2e-3)
    for slot in out[3:8]:
        assert slot.grid_export_kwh == pytest.approx(0.0, abs=2e-3)
    assert sum(s.batteries_charged_kwh for s in out[3:8]) > 4.0


def test_solved_plan_keeps_energy_balance_and_soc_bounds() -> None:
    """Every slot balances and the stored energy stays within the battery."""
    out = _solve(_make_slots())

    stored_kwh = _START_KWH
    for slot in out:
        supply = (
            slot.solcast_pv_estimate_kwh
            + slot.grid_import_kwh
            + slot.batteries_discharged_kwh * _EFFICIENCY
        )
        demand = (
            slot.avg_house_consumption_kwh
            + slot.grid_export_kwh
            + slot.batteries_charged_kwh / _EFFICIENCY
        )
        # PV may be curtailed, never created. Published fields are rounded.
        assert supply >= demand - 1e-3
        if slot.solcast_pv_estimate_kwh <= 1e-9:
            assert supply == pytest.approx(demand, abs=1e-3)

        stored_kwh += slot.batteries_charged_kwh - slot.batteries_discharged_kwh
        assert -1e-3 <= stored_kwh <= _USABLE_KWH + 1e-3


# ---------------------------------------------------------------------------
# score_plan
# ---------------------------------------------------------------------------


def _weights(discount: float) -> CostWeights:
    """Return cost weights with the scenario's cycle cost."""
    return CostWeights(cycle_cost_per_kwh=_CYCLE_COST, time_discount_rate=discount)


def test_score_reports_the_term_and_keeps_total_cost_money_only() -> None:
    """The term enters ``score`` and never ``total_cost``."""
    out = _solve(_make_slots())

    with_term = score_plan(out, _weights(_DISCOUNT), now=_NOW)
    rate = charge_timing_rate(_CYCLE_COST, 0.035)
    expected = sum(
        charge_timing_cost(s.batteries_charged_kwh, rate, _DISCOUNT ** (i + 0.5))
        for i, s in enumerate(out)
    )
    assert expected > 0.0
    assert with_term.charge_timing_bias == pytest.approx(expected, abs=1e-6)
    assert with_term.total_cost == pytest.approx(
        with_term.import_cost - with_term.export_revenue + with_term.cycle_cost,
        abs=1e-6,
    )


def test_score_prefers_the_early_charge_at_one_export_price() -> None:
    """Two plans that differ only in charge order: the early charge scores lower."""
    early = _make_slots()[:4]
    late = _make_slots()[:4]
    for slots, charge_idx, export_idx in ((early, 1, 2), (late, 2, 1)):
        slots[charge_idx].batteries_charged_kwh = 0.2
        slots[export_idx].grid_export_kwh = 0.2 / _EFFICIENCY

    weights = _weights(_DISCOUNT)
    assert (
        score_plan(early, weights, now=_NOW).score
        < score_plan(late, weights, now=_NOW).score
    )


@pytest.mark.parametrize(
    ("discount", "now"),
    [(1.0, _NOW), (_DISCOUNT, None)],
)
def test_score_term_is_disabled_without_a_discount(
    discount: float, now: datetime | None
) -> None:
    """No discount, or no clock to discount from, disables the term."""
    out = _solve(_make_slots())

    assert score_plan(out, _weights(discount), now=now).charge_timing_bias == (
        pytest.approx(0.0)
    )

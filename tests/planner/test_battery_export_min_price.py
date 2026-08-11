"""Regression tests for the battery_export_min_price floor (issue #752).

Covers the optional per-slot hard floor for *intentional* battery-to-grid
export.  When ``battery_export_min_price > 0`` and a slot's RAW export
price is strictly below this floor, the planner must forbid labelling
that slot ``force_batteries_discharge``.  Above the floor the optimizer
is still free to decide whether exporting is worthwhile (reaching the
threshold does NOT auto-trigger export).  The guard applies only to
intentional battery-to-grid export — it does not affect normal battery
self-consumption, PV export, or PV charging.  Default ``0.0`` is fully
backward compatible.

Test surfaces:

- MILP path: ``solve_milp(..., battery_export_min_price=...)`` caps
  ``ed[t]`` on blocked slots via the ``battery_export_blocked`` mask.
- Non-MILP path: ``apply_excess_export(..., battery_export_min_price=...)``
  requires ``export_price >= max(export_min_price, recommended_threshold,
  battery_export_min_price)`` for any slot it would otherwise tag
  ``ForceBatteriesDischarge``.
- Cost function: ``CostWeights.battery_export_min_price`` zeroes
  battery-destined export revenue on blocked slots so scored costs match
  the optimisation assumptions.
- Backward compatibility: ``battery_export_min_price = 0`` is identical to
  the pre-#752 behaviour (no slot is blocked by the floor).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.cost_function import score_plan
from custom_components.hsem.planner.cost_types import CostWeights
from custom_components.hsem.planner.discharge_scheduler import apply_excess_export
from custom_components.hsem.planner.milp_optimizer import (
    is_scipy_available,
    solve_milp,
)
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations

_TZ = UTC
_NOW = datetime(2024, 6, 15, 12, 0, tzinfo=_TZ)


# ---------------------------------------------------------------------------
# Slot helpers
# ---------------------------------------------------------------------------


def _make_milp_slot(
    *,
    hour: int,
    import_price: float = 0.50,
    export_price: float = 0.40,
    pv_kwh: float = 0.0,
    consumption_kwh: float = 0.3,
) -> PlannedSlot:
    """Build a minimal PlannedSlot anchored at 2024-06-15 hour *hour*."""
    start = datetime(2024, 6, 15, hour, 0, tzinfo=_TZ)
    s = PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=import_price, export_price=export_price),
    )
    s.avg_house_consumption_kwh = consumption_kwh
    s.solcast_pv_estimate_kwh = pv_kwh
    s.ev_planned_load_kwh = 0.0
    s.estimated_net_consumption_kwh = consumption_kwh - pv_kwh
    return s


def _make_excess_slot(
    *,
    hour: int,
    export_price: float = 0.50,
    import_price: float = 0.20,
    estimated_net_consumption_kwh: float = 0.5,
    recommendation: str | None = None,
) -> PlannedSlot:
    """Build a PlannedSlot for apply_excess_export tests."""
    start = datetime(2024, 6, 15, hour, 0, tzinfo=_TZ)
    return PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=import_price, export_price=export_price),
        solcast_pv_estimate_kwh=0.0,
        avg_house_consumption_kwh=0.5,
        estimated_net_consumption_kwh=estimated_net_consumption_kwh,
        recommendation=recommendation,
    )


# ===========================================================================
# MILP — solve_milp battery_export_min_price paths
# ===========================================================================


@pytest.mark.skipif(not is_scipy_available(), reason="scipy not available")
class TestMilpBatteryExportMinPrice:
    """MILP enforces the floor via the battery_export_blocked mask."""

    def test_disabled_does_not_block_any_slot(self) -> None:
        """With battery_export_min_price=0 the behaviour matches pre-#752."""
        # Battery full, expensive prices, near-equal import/export — the
        # LP would happily dump battery energy to the grid.
        slots = [
            _make_milp_slot(hour=h, import_price=3.0, export_price=2.9)
            for h in range(12, 16)
        ]

        floor0 = solve_milp(
            slots,
            _NOW,
            current_kwh=9.0,
            usable_kwh=9.0,
            max_charge_per_slot=5.0,
            max_discharge_per_slot=5.0,
            battery_export_min_price=0.0,
        )
        baseline = solve_milp(
            slots,
            _NOW,
            current_kwh=9.0,
            usable_kwh=9.0,
            max_charge_per_slot=5.0,
            max_discharge_per_slot=5.0,
            # Omit battery_export_min_price entirely — exercises default.
        )
        assert floor0 is not None
        assert baseline is not None
        f0_slots, _ = floor0
        base_slots, _ = baseline

        # Total grid export must match — default and explicit 0 share behaviour.
        assert sum(float(s.grid_export_kwh) for s in f0_slots) == pytest.approx(
            sum(float(s.grid_export_kwh) for s in base_slots), rel=1e-6
        )
        # Default allows intentional battery-to-grid export on these slots.
        assert sum(float(s.grid_export_kwh) for s in f0_slots) > 0.1

    def test_blocks_intentional_export_below_floor(self) -> None:
        """Below the floor the battery can serve house load but cannot export."""
        # Two slots, both at export_price = 0.10.  floor = 0.15 must block
        # both.  Battery is full; prices high enough that without the guard
        # the LP would dump battery energy to the grid.
        slots = [
            _make_milp_slot(hour=h, import_price=3.0, export_price=0.10)
            for h in range(12, 14)
        ]

        result = solve_milp(
            slots,
            _NOW,
            current_kwh=9.0,
            usable_kwh=9.0,
            max_charge_per_slot=5.0,
            max_discharge_per_slot=5.0,
            battery_export_min_price=0.15,
        )
        assert result is not None
        out, _diag = result
        # Battery may still cover house load (0.3 kWh AC / 0.97 eff ≈ 0.309).
        for s in out:
            assert s.batteries_discharged_kwh <= 0.3 / 0.97 + 1e-6
            # No battery-destined export may be scheduled on these slots.
            # PV export is unrestricted but no PV is present here.
            assert s.grid_export_kwh <= 1e-6

    def test_above_floor_still_export_allowed(self) -> None:
        """At export_price >= floor the LP is free to export (no auto-trigger)."""
        # floor = 0.15; one slot at 0.16 (>= floor), 0 PV, house load tiny.
        # The LP must be ALLOWED to export — but it must NOT export at
        # 0.16 because exporting that kWh is dominated by holding it for
        # a later higher-price opportunity.  (We instead verify a high
        # export-price slot DOES export.)
        slots_high = [
            _make_milp_slot(hour=h, import_price=3.0, export_price=2.0)
            for h in range(12, 14)
        ]
        res_high = solve_milp(
            slots_high,
            _NOW,
            current_kwh=9.0,
            usable_kwh=9.0,
            max_charge_per_slot=5.0,
            max_discharge_per_slot=5.0,
            battery_export_min_price=0.15,
        )
        assert res_high is not None
        out_high, _ = res_high
        # Above the floor the LP may export.  Sanity: at €2 export it
        # should do so (battery full, no PV).
        assert sum(float(s.grid_export_kwh) for s in out_high) > 0.1

    def test_mixed_high_and_low_slots(self) -> None:
        """Only slots below the floor are blocked — high slots export."""
        # Slot at 0.10 (blocked by floor=0.15) and slot at 0.50 (allowed).
        slots = [
            _make_milp_slot(hour=12, import_price=3.0, export_price=0.10),
            _make_milp_slot(hour=13, import_price=3.0, export_price=0.50),
        ]

        result = solve_milp(
            slots,
            _NOW,
            current_kwh=9.0,
            usable_kwh=9.0,
            max_charge_per_slot=5.0,
            max_discharge_per_slot=5.0,
            battery_export_min_price=0.15,
        )
        assert result is not None
        out, _ = result
        # The blocked slot must contribute zero battery-destined export.
        assert out[0].grid_export_kwh <= 1e-6
        # The high-price slot is free to export — verify it exports.
        assert out[1].grid_export_kwh > 0.1

    def test_pv_export_unrestricted_below_floor(self) -> None:
        """PV surplus must still export even on battery-blocked slots."""
        # 2 kWh PV surplus slots, export_price below floor.  PV must be
        # free to export — the floor only blocks battery-to-grid export.
        slots = [
            _make_milp_slot(hour=12, pv_kwh=3.0, export_price=0.10),
            _make_milp_slot(hour=13, pv_kwh=3.0, export_price=0.10),
        ]

        result = solve_milp(
            slots,
            _NOW,
            current_kwh=0.5,
            usable_kwh=9.0,
            max_charge_per_slot=5.0,
            max_discharge_per_slot=5.0,
            battery_export_min_price=0.15,
        )
        assert result is not None
        out, _ = result
        total_export = sum(float(s.grid_export_kwh) for s in out)
        # Each slot has ~3 kWh PV surplus minus 0.3 kWh house load;
        # the battery is too full to absorb much.  PV export must be > 0.
        assert total_export > 0.5
        # Battery must NOT be the source — no discharge > house load.
        for s in out:
            assert s.batteries_discharged_kwh <= 1e-6

    def test_floor_evaluated_on_raw_export_price(self) -> None:
        """Floor uses raw export_price, not the min_export_price-clamped one.

        If the floor were evaluated AFTER the min_export_price clamp
        (which sets p_exp < min_export_price to 0), slots with
        0 < export_price < battery_export_min_price would NOT be blocked
        because min_export_price=0 doesn't clamp them.  This test ensures
        the floor is independent of that clamp.
        """
        # min_export_price is 0 (disabled), but battery_export_min_price
        # is 0.15.  Slot export_price = 0.10 must still be blocked.
        slot = _make_milp_slot(hour=12, import_price=3.0, export_price=0.10)

        result = solve_milp(
            [slot],
            _NOW,
            current_kwh=9.0,
            usable_kwh=9.0,
            max_charge_per_slot=5.0,
            max_discharge_per_slot=5.0,
            min_export_price=0.0,
            battery_export_min_price=0.15,
        )
        assert result is not None
        out, _ = result
        assert out[0].grid_export_kwh <= 1e-6


# ===========================================================================
# Non-MILP — apply_excess_export battery_export_min_price path
# ===========================================================================


class TestApplyExcessExportFloor:
    """apply_excess_export honours the floor for non-MILP candidates."""

    def test_below_floor_slot_not_marked_force_discharge(self) -> None:
        """Slot whose export_price < floor is NOT tagged ForceBatteriesDischarge."""
        slot = _make_excess_slot(
            hour=1,
            export_price=0.10,
            import_price=0.05,
            estimated_net_consumption_kwh=-1.5,  # PV surplus, exportable
        )
        warnings: list[str] = []

        apply_excess_export(
            slots=[slot],
            now=datetime(2024, 6, 15, 0, 0, tzinfo=_TZ),
            current_capacity=1.0,
            required_capacity=0.0,
            export_price_threshold=0.10,
            warnings=warnings,
            # Both min_export_price and recommended_threshold are below
            # the slot's price, so without the new floor this would
            # mark the slot as ForceBatteriesDischarge.
            export_min_price=0.0,
            recommended_threshold=0.0,
            battery_export_min_price=0.15,
        )

        assert slot.recommendation != Recommendations.ForceBatteriesDischarge.value
        assert warnings == []

    def test_at_floor_slot_still_may_be_marked(self) -> None:
        """At export_price >= floor the candidate is eligible (no auto-trigger).

        The slot at exactly 0.15 (== floor) is eligible to be tagged
        ForceBatteriesDischarge when the optimizer decides to do so.  We
        don't assert that the optimizer *must* mark it (that depends on
        the budget logic), just that the floor itself doesn't refuse it.
        """
        slot = _make_excess_slot(
            hour=1,
            export_price=0.15,
            import_price=0.05,
            estimated_net_consumption_kwh=-1.5,
        )
        warnings: list[str] = []

        apply_excess_export(
            slots=[slot],
            now=datetime(2024, 6, 15, 0, 0, tzinfo=_TZ),
            current_capacity=1.0,
            required_capacity=0.0,
            export_price_threshold=0.10,
            warnings=warnings,
            export_min_price=0.0,
            recommended_threshold=0.0,
            battery_export_min_price=0.15,
        )

        # The slot is eligible (>= floor) AND surplus AND exportable.
        # apply_excess_export will mark it.
        assert slot.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert len(warnings) == 1

    def test_floor_takes_precedence_over_lower_thresholds(self) -> None:
        """Floor > recommended_threshold > 0 — floor decides.

        A slot at export_price=0.12 is above recommended_threshold=0.10
        but below battery_export_min_price=0.15.  Without the floor, the
        pre-#752 logic (export_price >= max(export_min_price,
        recommended_threshold)) would mark it.  With the floor it must
        NOT be marked.
        """
        slot = _make_excess_slot(
            hour=1,
            export_price=0.12,
            import_price=0.05,
            estimated_net_consumption_kwh=-1.5,
        )
        warnings: list[str] = []

        apply_excess_export(
            slots=[slot],
            now=datetime(2024, 6, 15, 0, 0, tzinfo=_TZ),
            current_capacity=1.0,
            required_capacity=0.0,
            export_price_threshold=0.10,
            warnings=warnings,
            export_min_price=0.0,
            recommended_threshold=0.10,
            battery_export_min_price=0.15,
        )

        assert slot.recommendation != Recommendations.ForceBatteriesDischarge.value
        assert warnings == []

    def test_disabled_floor_matches_baseline(self) -> None:
        """With battery_export_min_price=0 the behaviour matches pre-#752."""
        slot_baseline = _make_excess_slot(
            hour=1,
            export_price=0.50,
            import_price=0.20,
            estimated_net_consumption_kwh=-1.0,
        )
        slot_floor0 = _make_excess_slot(
            hour=1,
            export_price=0.50,
            import_price=0.20,
            estimated_net_consumption_kwh=-1.0,
        )
        warnings_baseline: list[str] = []
        warnings_floor0: list[str] = []

        apply_excess_export(
            slots=[slot_baseline],
            now=datetime(2024, 6, 15, 0, 0, tzinfo=_TZ),
            current_capacity=1.0,
            required_capacity=0.0,
            export_price_threshold=0.10,
            warnings=warnings_baseline,
        )
        # Explicit 0 must match omitted (default).
        apply_excess_export(
            slots=[slot_floor0],
            now=datetime(2024, 6, 15, 0, 0, tzinfo=_TZ),
            current_capacity=1.0,
            required_capacity=0.0,
            export_price_threshold=0.10,
            warnings=warnings_floor0,
            battery_export_min_price=0.0,
        )

        assert slot_baseline.recommendation == slot_floor0.recommendation
        assert len(warnings_floor0) == len(warnings_baseline)


# ===========================================================================
# Cost function — CostWeights.battery_export_min_price consistency
# ===========================================================================


def _slot_with_export(
    *,
    hour: int,
    export_price: float,
    grid_export_kwh: float,
    pv_kwh: float = 0.0,
    batteries_discharged_kwh: float = 0.0,
) -> PlannedSlot:
    """Build a finished slot with explicit grid_export and battery discharge."""
    start = datetime(2024, 6, 15, hour, 0, tzinfo=_TZ)
    s = PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=0.50, export_price=export_price),
    )
    s.avg_house_consumption_kwh = 0.5
    s.solcast_pv_estimate_kwh = pv_kwh
    s.ev_planned_load_kwh = 0.0
    s.estimated_net_consumption_kwh = 0.5 - pv_kwh
    s.grid_export_kwh = grid_export_kwh
    s.grid_import_kwh = 0.0
    s.batteries_charged_kwh = 0.0
    s.batteries_discharged_kwh = batteries_discharged_kwh
    s.estimated_battery_soc_pct = 50.0
    s.estimated_battery_capacity_kwh = 10.0
    return s


class TestCostFunctionFloor:
    """score_plan zeroes battery-destined export revenue on blocked slots."""

    def test_battery_destined_export_zeroed_below_floor(self) -> None:
        """Below the floor, a battery-destined export earns nothing in score.

        A slot with PV=0 (so export can only be battery-destined), export
        1.0 kWh at 0.10, and battery discharge 1.05 kWh.  With floor=0.15,
        the cost function must value that export at 0 (it can never be
        realised).  Without the floor the export revenue would be 0.10.
        """
        slot = _slot_with_export(
            hour=12,
            export_price=0.10,
            grid_export_kwh=1.0,
            pv_kwh=0.0,
            batteries_discharged_kwh=1.05,
        )

        weights = CostWeights(
            export_min_price=0.0,
            battery_export_min_price=0.15,
        )
        bd = score_plan([slot], initial_battery_kwh=10.0, weights=weights)
        assert bd.export_revenue == pytest.approx(0.0, abs=1e-9)

        # Without the floor (default 0), export revenue should be 0.10.
        weights_no_floor = CostWeights(
            export_min_price=0.0,
            battery_export_min_price=0.0,
        )
        bd2 = score_plan(
            [slot],
            initial_battery_kwh=10.0,
            weights=weights_no_floor,
        )
        assert bd2.export_revenue == pytest.approx(0.10, rel=1e-6)

    def test_pv_destined_export_not_zeroed_below_floor(self) -> None:
        """A slot with PV surplus exporting below the floor is NOT zeroed.

        With pv_kwh = 1.2 kWh and house load 0.5, the 1.0 kWh export is
        PV-destined.  The floor only blocks battery-destined export.
        """
        slot = _slot_with_export(
            hour=12,
            export_price=0.10,
            grid_export_kwh=1.0,
            pv_kwh=1.5,
            batteries_discharged_kwh=0.0,
        )

        weights = CostWeights(
            export_min_price=0.0,
            battery_export_min_price=0.15,
        )
        bd = score_plan(
            [slot],
            initial_battery_kwh=10.0,
            weights=weights,
        )
        assert bd.export_revenue == pytest.approx(0.10, rel=1e-6)

    def test_above_floor_export_revenue_retained(self) -> None:
        """At export_price >= floor, the cost function keeps export revenue."""
        slot = _slot_with_export(
            hour=12,
            export_price=0.20,
            grid_export_kwh=1.0,
            pv_kwh=0.0,
            batteries_discharged_kwh=1.05,
        )

        weights = CostWeights(
            export_min_price=0.0,
            battery_export_min_price=0.15,
        )
        bd = score_plan(
            [slot],
            initial_battery_kwh=10.0,
            weights=weights,
        )
        assert bd.export_revenue == pytest.approx(0.20, rel=1e-6)

    def test_disabled_floor_matches_baseline(self) -> None:
        """battery_export_min_price = 0 is identical to the pre-#752 cost function."""
        slot = _slot_with_export(
            hour=12,
            export_price=0.10,
            grid_export_kwh=1.0,
            pv_kwh=0.0,
            batteries_discharged_kwh=1.05,
        )

        weights_zero = CostWeights(
            export_min_price=0.0,
            battery_export_min_price=0.0,
        )
        weights_default = CostWeights(
            export_min_price=0.0,
            # Default battery_export_min_price is 0.0 in dataclass.
        )

        bd_zero = score_plan(
            [slot],
            initial_battery_kwh=10.0,
            weights=weights_zero,
        )
        bd_default = score_plan(
            [slot],
            initial_battery_kwh=10.0,
            weights=weights_default,
        )

        assert bd_zero.total_cost == pytest.approx(bd_default.total_cost, rel=1e-9)

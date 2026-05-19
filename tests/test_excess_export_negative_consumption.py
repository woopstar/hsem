"""Tests for battery-discharge-budget accounting in excess-export scheduling.

``apply_excess_export`` tracks a ``battery_discharge_budget_kwh`` — the kWh the
battery holds *beyond* what is needed to cover future house load.  The budget
represents stored battery energy only; solar is a separate flow.

Budget drain rule: ``battery_discharge_budget_kwh -= max(estimated_net_consumption_kwh, 0.0)``

- **Positive** net consumption (house > solar): battery covers the shortfall →
  budget decreases.
- **Negative** net consumption (solar surplus > house): solar handles everything,
  battery is not drawn on for house load → budget drain is zero (``max(-x, 0) = 0``).

A solar-surplus slot is still eligible for ``ForceBatteriesDischarge`` (the battery
discharges to export), but it does NOT consume the budget: the budget measures only
how many kWh the battery needs for *load-covering*, not for solar-backed export.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.planner.charge_scheduler import apply_excess_export
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_slot(
    offset_hours: int,
    export_price: float = 0.50,
    import_price: float = 0.20,
    estimated_net_consumption_kwh: float = 0.5,
    recommendation: str | None = None,
) -> PlannedSlot:
    """Build a minimal PlannedSlot anchored at *now + offset_hours*."""
    now = datetime(2024, 6, 15, 0, 0, tzinfo=UTC)
    start = now + timedelta(hours=offset_hours)
    return PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=import_price, export_price=export_price),
        solcast_pv_estimate_kwh=0.0,
        avg_house_consumption_kwh=0.5,
        estimated_net_consumption_kwh=estimated_net_consumption_kwh,
        recommendation=recommendation,
    )


def _now() -> datetime:
    return datetime(2024, 6, 15, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Positive net consumption drains the battery discharge budget
# ---------------------------------------------------------------------------


class TestPositiveConsumptionDrainsBudget:
    """House-load slots reduce the battery discharge budget by their net consumption."""

    def test_single_consuming_slot_drains_budget(self) -> None:
        """A positive-consumption slot is assigned and drains the budget correctly.

        budget = 1.0 kWh; slot net = 0.5 → budget after = 0.5 (still positive, no more candidates).
        """
        slot = _make_slot(1, estimated_net_consumption_kwh=0.5, export_price=0.60)
        warnings: list[str] = []

        apply_excess_export(
            slots=[slot],
            now=_now(),
            current_capacity=1.0,
            required_capacity=0.0,
            export_price_threshold=0.10,
            warnings=warnings,
        )

        assert slot.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert len(warnings) == 1

    def test_budget_exhausted_stops_assignment(self) -> None:
        """Budget is drained slot-by-slot; once exhausted no further slots are assigned.

        budget = 1.0; slot_a net = 0.6 → budget = 0.4; slot_b net = 0.6 → budget = -0.2 ≤ 0
        → loop breaks before slot_c.
        """
        slot_a = _make_slot(1, estimated_net_consumption_kwh=0.6, export_price=0.60)
        slot_b = _make_slot(2, estimated_net_consumption_kwh=0.6, export_price=0.55)
        slot_c = _make_slot(3, estimated_net_consumption_kwh=0.6, export_price=0.50)
        warnings: list[str] = []

        apply_excess_export(
            slots=[slot_a, slot_b, slot_c],
            now=_now(),
            current_capacity=1.0,
            required_capacity=0.0,
            export_price_threshold=0.10,
            warnings=warnings,
        )

        assert slot_a.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert slot_b.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert slot_c.recommendation is None

    def test_zero_budget_skips_all_slots(self) -> None:
        """When current_capacity == required_capacity the budget is zero → early return."""
        slot = _make_slot(1, estimated_net_consumption_kwh=0.5)
        warnings: list[str] = []

        apply_excess_export(
            slots=[slot],
            now=_now(),
            current_capacity=1.0,
            required_capacity=1.0,
            export_price_threshold=0.10,
            warnings=warnings,
        )

        assert slot.recommendation is None


# ---------------------------------------------------------------------------
# Solar-surplus slots: budget unchanged (drain = 0), still assigned
# ---------------------------------------------------------------------------


class TestSolarSurplusDoesNotDrainBudget:
    """Solar-surplus slots contribute zero budget drain (max(net, 0) = 0).

    The battery is still discharged during surplus slots (ForceBatteriesDischarge),
    but the budget represents battery load-covering energy only — solar handles its
    own export flow independently.  This means the budget is NOT inflated by surplus,
    which was the original bug.
    """

    def test_surplus_slot_assigned_but_budget_unchanged(self) -> None:
        """A surplus slot is tagged but does not reduce the budget.

        budget = 0.5; slot net = -1.5 → max(-1.5, 0) = 0 → budget stays 0.5.
        Only one candidate: slot gets assigned.
        """
        slot = _make_slot(1, estimated_net_consumption_kwh=-1.5, export_price=0.60)
        warnings: list[str] = []

        apply_excess_export(
            slots=[slot],
            now=_now(),
            current_capacity=0.5,
            required_capacity=0.0,
            export_price_threshold=0.10,
            warnings=warnings,
        )

        assert slot.recommendation == Recommendations.ForceBatteriesDischarge.value

    def test_two_surplus_slots_both_assigned_budget_never_grows(self) -> None:
        """Multiple surplus slots are all assigned; budget stays constant (not growing).

        budget = 0.5; slot_a net = -2.0 → drain = 0 → budget = 0.5.
        slot_b net = -2.0 → drain = 0 → budget = 0.5.
        Both assigned (budget never hits zero), loop ends naturally.
        """
        slot_a = _make_slot(1, estimated_net_consumption_kwh=-2.0, export_price=0.60)
        slot_b = _make_slot(2, estimated_net_consumption_kwh=-2.0, export_price=0.55)
        warnings: list[str] = []

        apply_excess_export(
            slots=[slot_a, slot_b],
            now=_now(),
            current_capacity=0.5,
            required_capacity=0.0,
            export_price_threshold=0.10,
            warnings=warnings,
        )

        assert slot_a.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert slot_b.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert len(warnings) == 2

    def test_surplus_cannot_start_loop_when_budget_zero(self) -> None:
        """A zero initial budget triggers the early return; surplus slots are ignored."""
        slot = _make_slot(1, estimated_net_consumption_kwh=-2.0, export_price=0.60)
        warnings: list[str] = []

        apply_excess_export(
            slots=[slot],
            now=_now(),
            current_capacity=1.0,
            required_capacity=1.0,  # budget = 0.0 → early return
            export_price_threshold=0.10,
            warnings=warnings,
        )

        assert slot.recommendation is None

    def test_mixed_surplus_then_consuming_drains_budget_correctly(self) -> None:
        """A surplus slot contributes 0 drain; subsequent consuming slots drain normally.

        budget = 0.8 kWh.
        slot_a: net = -1.0 → drain = max(-1.0, 0) = 0 → budget = 0.8.
        slot_b: net = 0.5  → drain = 0.5 → budget = 0.3.
        slot_c: net = 0.4  → drain = 0.4 → budget = -0.1 ≤ 0 → slot_d skipped.
        """
        slot_a = _make_slot(1, estimated_net_consumption_kwh=-1.0, export_price=0.70)
        slot_b = _make_slot(2, estimated_net_consumption_kwh=0.5, export_price=0.65)
        slot_c = _make_slot(3, estimated_net_consumption_kwh=0.4, export_price=0.60)
        slot_d = _make_slot(4, estimated_net_consumption_kwh=0.4, export_price=0.55)
        warnings: list[str] = []

        apply_excess_export(
            slots=[slot_a, slot_b, slot_c, slot_d],
            now=_now(),
            current_capacity=0.8,
            required_capacity=0.0,
            export_price_threshold=0.10,
            warnings=warnings,
        )

        # All three slots (slot_a surplus + slot_b + slot_c consuming) are assigned;
        # slot_d is skipped because budget hits -0.1 after slot_c.
        assert slot_a.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert slot_b.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert slot_c.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert slot_d.recommendation is None

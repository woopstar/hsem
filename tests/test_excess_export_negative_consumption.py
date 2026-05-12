"""Tests for correct signed net-consumption accounting in excess-export scheduling.

``apply_excess_export`` drains ``excess`` using ``estimated_net_consumption``
directly (preserving the sign):

- **Positive** net consumption (house load > solar): the slot consumes battery
  energy → ``excess`` decreases.
- **Negative** net consumption (solar surplus > house load): the battery can
  discharge even more into the grid → ``excess`` increases, unlocking additional
  high-price discharge slots.

This correctly models the physical reality: a solar-surplus slot acts as an
"energy source" for the grid export budget, not a drain.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.planner.charge_scheduler import apply_excess_export
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_slot(
    offset_hours: int,
    export_price: float = 0.50,
    import_price: float = 0.20,
    estimated_net_consumption: float = 0.5,
    recommendation: str | None = None,
) -> PlannedSlot:
    """Build a minimal PlannedSlot anchored at *now + offset_hours*."""
    now = datetime(2024, 6, 15, 0, 0, tzinfo=UTC)
    start = now + timedelta(hours=offset_hours)
    return PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        import_price=import_price,
        export_price=export_price,
        solcast_pv_estimate=0.0,
        avg_house_consumption=0.5,
        estimated_net_consumption=estimated_net_consumption,
        recommendation=recommendation,
    )


def _now() -> datetime:
    return datetime(2024, 6, 15, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Positive net consumption drains excess (house uses more than solar)
# ---------------------------------------------------------------------------


class TestPositiveConsumptionDrainsExcess:
    """Slots where house load exceeds solar should reduce the available excess."""

    def test_single_consuming_slot_drains_excess(self) -> None:
        """A single positive-consumption slot is assigned and reduces excess correctly."""
        # excess = 1.0 - 0.0 = 1.0; slot drains 0.5 → excess becomes 0.5.
        slot = _make_slot(1, estimated_net_consumption=0.5, export_price=0.60)
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

    def test_excess_exhausted_after_two_consuming_slots(self) -> None:
        """Two consuming slots drain excess; third slot is skipped.

        excess = 1.0; each slot drains 0.6 kWh.
        After slot_a: 1.0 - 0.6 = 0.4 → slot_b assigned.
        After slot_b: 0.4 - 0.6 = -0.2 ≤ 0 → loop breaks before slot_c.
        """
        slot_a = _make_slot(1, estimated_net_consumption=0.6, export_price=0.60)
        slot_b = _make_slot(2, estimated_net_consumption=0.6, export_price=0.55)
        slot_c = _make_slot(3, estimated_net_consumption=0.6, export_price=0.50)
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

    def test_zero_excess_skips_all_slots(self) -> None:
        """When current_capacity == required_capacity no slots are assigned."""
        slot = _make_slot(1, estimated_net_consumption=0.5)
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
# Negative net consumption (solar surplus) increases export budget
# ---------------------------------------------------------------------------


class TestSolarSurplusIncreasesExcess:
    """Slots where solar exceeds house load should *grow* the available excess.

    A surplus slot means solar already covers the house — the battery can
    export even more into the grid during that hour.  Subtracting a negative
    ``estimated_net_consumption`` correctly increases ``excess``, enabling
    the algorithm to schedule additional high-price discharge slots downstream.
    """

    def test_surplus_slot_unlocks_extra_downstream_discharge(self) -> None:
        """A solar-surplus slot grows excess, allowing more slots to be assigned.

        excess = 0.4 kWh (barely positive).
        slot_a: net = -1.0 → excess = 0.4 - (-1.0) = 1.4.
        slot_b: net = 0.8  → excess = 1.4 - 0.8 = 0.6.
        slot_c: net = 0.7  → excess = 0.6 - 0.7 = -0.1 ≤ 0 → slot_d skipped.
        """
        slot_a = _make_slot(1, estimated_net_consumption=-1.0, export_price=0.70)
        slot_b = _make_slot(2, estimated_net_consumption=0.8, export_price=0.65)
        slot_c = _make_slot(3, estimated_net_consumption=0.7, export_price=0.60)
        slot_d = _make_slot(4, estimated_net_consumption=0.4, export_price=0.55)
        warnings: list[str] = []

        apply_excess_export(
            slots=[slot_a, slot_b, slot_c, slot_d],
            now=_now(),
            current_capacity=0.4,
            required_capacity=0.0,
            export_price_threshold=0.10,
            warnings=warnings,
        )

        assert slot_a.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert slot_b.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert slot_c.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert slot_d.recommendation is None

    def test_surplus_only_slots_all_assigned(self) -> None:
        """All solar-surplus candidates are assigned when initial excess > 0.

        excess = 0.5.
        slot_a: net = -2.0 → excess = 0.5 + 2.0 = 2.5 → slot_b assigned.
        slot_b: net = -2.0 → excess = 2.5 + 2.0 = 4.5 → loop ends (no more).
        Both slots correctly tagged ForceBatteriesDischarge.
        """
        slot_a = _make_slot(1, estimated_net_consumption=-2.0, export_price=0.60)
        slot_b = _make_slot(2, estimated_net_consumption=-2.0, export_price=0.55)
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

    def test_surplus_cannot_start_loop_when_initial_excess_zero(self) -> None:
        """If initial excess is zero the early-return fires; surplus cannot override it."""
        slot = _make_slot(1, estimated_net_consumption=-2.0, export_price=0.60)
        warnings: list[str] = []

        apply_excess_export(
            slots=[slot],
            now=_now(),
            current_capacity=1.0,
            required_capacity=1.0,  # excess = 0.0 → early return
            export_price_threshold=0.10,
            warnings=warnings,
        )

        assert slot.recommendation is None

    def test_mixed_surplus_then_consuming_uses_expanded_budget(self) -> None:
        """A surplus slot expands the budget; subsequent consuming slots draw it down.

        excess = 0.3.
        slot_a: net = -0.5 → excess = 0.3 + 0.5 = 0.8.
        slot_b: net = 0.6  → excess = 0.8 - 0.6 = 0.2.
        slot_c: net = 0.3  → excess = 0.2 - 0.3 = -0.1 ≤ 0 → slot_d skipped.
        """
        slot_a = _make_slot(1, estimated_net_consumption=-0.5, export_price=0.70)
        slot_b = _make_slot(2, estimated_net_consumption=0.6, export_price=0.65)
        slot_c = _make_slot(3, estimated_net_consumption=0.3, export_price=0.60)
        slot_d = _make_slot(4, estimated_net_consumption=0.4, export_price=0.55)
        warnings: list[str] = []

        apply_excess_export(
            slots=[slot_a, slot_b, slot_c, slot_d],
            now=_now(),
            current_capacity=0.3,
            required_capacity=0.0,
            export_price_threshold=0.10,
            warnings=warnings,
        )

        assert slot_a.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert slot_b.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert slot_c.recommendation == Recommendations.ForceBatteriesDischarge.value
        assert slot_d.recommendation is None

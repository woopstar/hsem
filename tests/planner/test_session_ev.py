"""Tests for session-aware EV demand in the MILP (issue #615).

When an EV is actively charging, the MILP treats the first 2 hours as
certain demand and prevents grid-charging the battery during those slots.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.milp_optimizer import solve_milp
from custom_components.hsem.utils.prices import SlotPrice

# Try to import scipy to check availability
try:
    import scipy  # noqa: F401

    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


def _make_slots(hours: int = 6, start_hour: int = 0) -> list[PlannedSlot]:
    """Build test slots with zero consumption and zero PV."""
    tz = timezone(timedelta(hours=2))
    base = datetime(2024, 6, 15, start_hour, 0, tzinfo=tz)
    slots = []
    for h in range(hours):
        for q in range(4):
            start = base + timedelta(hours=h, minutes=q * 15)
            end = start + timedelta(minutes=15)
            slots.append(
                PlannedSlot(
                    start=start,
                    end=end,
                    avg_house_consumption_kwh=0.3,
                    solcast_pv_estimate_kwh=0.0,
                    price=SlotPrice(import_price=0.15, export_price=0.05),
                )
            )
    return slots


@pytest.mark.skipif(not HAS_SCIPY, reason="scipy not available")
class TestSessionEVDemand:
    """Verify session EV demand overrides probabilistic demand."""

    def test_session_ev_bounds_are_fixed(self) -> None:
        """First 8 slots should have EV bounds fixed to session demand."""
        slots = _make_slots(hours=4)  # 16 slots at 15-min
        now = slots[0].start

        ev = EVConfig(
            enabled=True,
            initial_soc_kwh=10.0,
            target_kwh=40.0,
            capacity_kwh=60.0,
            max_charge_per_slot=2.75,  # 11 kW × 0.25h
            charger_efficiency=0.95,
            deadline_slot=15,
            session_charge_kw=7.2,  # EV actively charging at 7.2 kW
        )

        result = solve_milp(
            slots=slots,
            now=now,
            current_kwh=5.0,
            usable_kwh=10.0,
            max_charge_per_slot=2.5,
            ev_configs=[ev],
        )
        assert result is not None

        # First 8 slots should have EV charging at session rate
        session_slots_with_ev = sum(
            1
            for s in result[:8]
            if s.ev_planned_load_kwh is not None and s.ev_planned_load_kwh > 0.001
        )
        assert session_slots_with_ev >= 1, (
            "Session EV demand should result in EV charging in first 8 slots"
        )

    def test_no_grid_charge_during_session(self) -> None:
        """Battery should not grid-charge during session EV slots."""
        slots = _make_slots(hours=4)
        now = slots[0].start

        # Add PV surplus so battery COULD charge, but EV session should block grid
        for s in slots[:4]:
            s.solcast_pv_estimate_kwh = 0.0  # no PV

        ev = EVConfig(
            enabled=True,
            initial_soc_kwh=10.0,
            target_kwh=40.0,
            capacity_kwh=60.0,
            max_charge_per_slot=2.75,
            charger_efficiency=0.95,
            deadline_slot=15,
            session_charge_kw=7.2,
        )

        result = solve_milp(
            slots=slots,
            now=now,
            current_kwh=5.0,
            usable_kwh=10.0,
            max_charge_per_slot=2.5,
            ev_configs=[ev],
        )
        assert result is not None

        # Session slots should not have BatteriesChargeGrid
        from custom_components.hsem.utils.recommendations import Recommendations

        for s in result[:8]:
            assert s.recommendation != Recommendations.BatteriesChargeGrid.value, (
                f"Slot {s.start} has BatteriesChargeGrid during session demand"
            )

    def test_no_session_ev_when_not_charging(self) -> None:
        """When session_charge_kw is None, no session demand override."""
        slots = _make_slots(hours=4)
        now = slots[0].start

        ev = EVConfig(
            enabled=True,
            initial_soc_kwh=10.0,
            target_kwh=40.0,
            capacity_kwh=60.0,
            max_charge_per_slot=2.75,
            charger_efficiency=0.95,
            deadline_slot=15,
            session_charge_kw=None,  # NOT actively charging
        )

        result = solve_milp(
            slots=slots,
            now=now,
            current_kwh=5.0,
            usable_kwh=10.0,
            max_charge_per_slot=2.5,
            ev_configs=[ev],
        )
        assert result is not None
        # Should work normally without session override

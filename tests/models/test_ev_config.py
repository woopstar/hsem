"""Tests for EVConfig deadline safety margin / escalation properties (issue #845)."""

from __future__ import annotations

import pytest

from custom_components.hsem.models.ev_config import EVConfig


def test_effective_deadline_target_kwh_adds_margin() -> None:
    """The effective target adds the configured margin on top of target_kwh."""
    ev = EVConfig(
        initial_soc_kwh=50.0,
        target_kwh=55.0,
        capacity_kwh=80.0,
        deadline_margin_kwh=0.5,
        deadline_slot=9,
        max_charge_per_slot=10.0,
    )
    assert ev.effective_deadline_target_kwh == pytest.approx(55.5)


def test_effective_deadline_target_kwh_capped_at_capacity() -> None:
    """The margined target never exceeds the EV's nameplate capacity."""
    ev = EVConfig(
        initial_soc_kwh=50.0,
        target_kwh=55.0,
        capacity_kwh=57.0,
        deadline_margin_kwh=10.0,  # would overshoot capacity without the cap
        deadline_slot=9,
        max_charge_per_slot=10.0,
    )
    assert ev.effective_deadline_target_kwh == pytest.approx(57.0)


def test_deadline_escalated_false_without_deadline() -> None:
    """No deadline means no escalation, regardless of margin/reachability."""
    ev = EVConfig(
        initial_soc_kwh=0.0,
        target_kwh=50.0,
        capacity_kwh=60.0,
        deadline_margin_kwh=5.0,
        deadline_slot=None,
        max_charge_per_slot=1.0,
    )
    assert ev.deadline_escalated(48) is False


def test_deadline_escalated_false_when_reachable() -> None:
    """Max-power charging over the remaining slots comfortably reaches the margined target."""
    ev = EVConfig(
        initial_soc_kwh=50.0,
        target_kwh=55.0,
        capacity_kwh=80.0,
        deadline_margin_kwh=0.5,
        deadline_slot=9,  # 10 slots
        max_charge_per_slot=10.0,  # 100 kWh reachable, far above 55.5
    )
    assert ev.deadline_escalated(48) is False


def test_deadline_escalated_true_when_unreachable_at_max_power() -> None:
    """Even max-power charging can't reach the margined target before the deadline."""
    ev = EVConfig(
        initial_soc_kwh=10.0,
        target_kwh=50.0,  # shortfall 40
        capacity_kwh=80.0,
        deadline_margin_kwh=4.0,  # effective target 54
        deadline_slot=3,  # 4 slots
        max_charge_per_slot=10.0,  # reachable = 10 + 10*4 = 50 < 54
    )
    assert ev.deadline_escalated(48) is True


def test_deadline_escalated_clamps_deadline_slot_to_horizon() -> None:
    """A deadline_slot built against a longer horizon than the current solve
    must be clamped to ``m - 1``, exactly like
    ``planner/milp/_ev_constraints.py``'s ``d = max(0, min(d, m - 1))``,
    instead of using the raw out-of-range index directly (issue #864).
    """
    # deadline_slot=95 would come from e.g. a 96-slot horizon, but this
    # solve only covers m=4 slots (indices 0..3).
    ev = EVConfig(
        initial_soc_kwh=10.0,
        target_kwh=50.0,  # shortfall 40
        capacity_kwh=80.0,
        deadline_margin_kwh=4.0,  # effective target 54
        deadline_slot=95,
        max_charge_per_slot=10.0,
    )
    # Clamped to d = min(95, 4 - 1) = 3 -> reachable = 10 + 10*4 = 50 < 54.
    assert ev.deadline_escalated(4) is True
    # Matches what the same clamp applied to the constraint math would give.
    assert ev.deadline_slot is not None
    d = max(0, min(ev.deadline_slot, 4 - 1))
    max_reachable = ev.initial_soc_kwh + ev.max_charge_per_slot * (d + 1)
    assert max_reachable < ev.effective_deadline_target_kwh - 1e-9

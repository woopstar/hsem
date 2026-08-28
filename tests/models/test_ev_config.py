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
    assert ev.deadline_escalated is False


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
    assert ev.deadline_escalated is False


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
    assert ev.deadline_escalated is True

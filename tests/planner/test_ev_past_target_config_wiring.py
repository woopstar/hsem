"""Tests for wiring the avoided-future-import valuation into EVConfig (issue #630).

Verifies that :func:`_build_ev_configs_for_milp` in ``engine_core.py``
correctly attaches ``future_value_per_kwh`` to charge-past-target EVs,
using the per-EV ``past_target_confidence_factor`` and the 24h average
forecast import price.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.engine_core import _build_ev_configs_for_milp
from custom_components.hsem.utils.prices import SlotPrice

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2024, 6, 15, 14, 0, tzinfo=_TZ)


def _slots(n: int, import_price: float) -> list[PlannedSlot]:
    slots = []
    for i in range(n):
        start = _NOW + timedelta(hours=i)
        slots.append(
            PlannedSlot(
                start=start,
                end=start + timedelta(hours=1),
                price=SlotPrice(import_price=import_price, export_price=0.10),
            )
        )
    return slots


_BASE_INPUT = PlannerInput(
    now_iso=_NOW.isoformat(),
    interval_minutes=60,
    interval_length_hours=24,
    ev_planned_load_enabled=True,
    ev_planned_load_connected=True,
    ev_planned_load_smart_charging_enabled=True,
    ev_planned_load_current_soc_pct=80.0,
    ev_planned_load_target_soc_pct=80.0,  # already at target
    ev_planned_load_battery_capacity_kwh=50.0,
    ev_planned_load_charger_power_kw=7.0,
    ev_planned_load_charger_efficiency_pct=100.0,
    ev_planned_allow_charge_past_target_soc=True,
    ev_past_target_confidence_factor=0.9,
)


def _base_input(**overrides: object) -> PlannerInput:
    return dataclasses.replace(_BASE_INPUT, **overrides)  # type: ignore[arg-type]


class TestBuildEvConfigsFutureValue:
    def test_charge_past_target_gets_future_value_per_kwh(self):
        """When past-target charging is enabled, future_value_per_kwh is set
        to confidence_factor * 24h average forecast import price."""
        slots = _slots(24, import_price=2.0)
        inp = _base_input(ev_past_target_confidence_factor=0.9)

        configs = _build_ev_configs_for_milp(inp, slots, _NOW)

        assert configs is not None
        assert len(configs) == 1
        ev = configs[0]
        assert ev.charge_past_target is True
        assert ev.future_value_per_kwh == pytest.approx(0.9 * 2.0, rel=1e-6)

    def test_confidence_factor_scales_the_value(self):
        """A different confidence_factor produces a proportionally different value."""
        slots = _slots(24, import_price=1.0)
        inp = _base_input(ev_past_target_confidence_factor=0.5)

        configs = _build_ev_configs_for_milp(inp, slots, _NOW)

        assert configs is not None
        assert configs[0].future_value_per_kwh == pytest.approx(0.5, rel=1e-6)

    def test_no_future_value_when_allow_past_target_disabled(self):
        """When allow_charge_past_target_soc=False, the EV is excluded from
        the MILP entirely once at target — no config is built for it."""
        slots = _slots(24, import_price=2.0)
        inp = _base_input(ev_planned_allow_charge_past_target_soc=False)

        configs = _build_ev_configs_for_milp(inp, slots, _NOW)

        assert configs is None

    def test_future_value_none_when_below_target(self):
        """When the EV is below target (normal deadline mode), charge_past_target
        is False and future_value_per_kwh stays None."""
        slots = _slots(24, import_price=2.0)
        inp = _base_input(
            ev_planned_load_current_soc_pct=20.0,
            ev_planned_load_target_soc_pct=80.0,
            ev_planned_load_deadline=_NOW + timedelta(hours=10),
        )

        configs = _build_ev_configs_for_milp(inp, slots, _NOW)

        assert configs is not None
        ev = configs[0]
        assert ev.charge_past_target is False
        assert ev.future_value_per_kwh is None

    def test_second_ev_uses_its_own_confidence_factor(self):
        """The second EV's future_value_per_kwh uses its own confidence factor,
        independent of the primary EV's."""
        slots = _slots(24, import_price=2.0)
        inp = _base_input(
            ev_past_target_confidence_factor=0.9,
            ev_second_planned_load_enabled=True,
            ev_second_planned_load_connected=True,
            ev_second_planned_load_smart_charging_enabled=True,
            ev_second_planned_load_current_soc_pct=80.0,
            ev_second_planned_load_target_soc_pct=80.0,
            ev_second_planned_load_battery_capacity_kwh=40.0,
            ev_second_planned_load_charger_power_kw=7.0,
            ev_second_planned_load_charger_efficiency_pct=100.0,
            ev_second_allow_charge_past_target_soc=True,
            ev_second_past_target_confidence_factor=0.5,
        )

        configs = _build_ev_configs_for_milp(inp, slots, _NOW)

        assert configs is not None
        assert len(configs) == 2
        assert configs[0].future_value_per_kwh == pytest.approx(0.9 * 2.0, rel=1e-6)
        assert configs[1].future_value_per_kwh == pytest.approx(0.5 * 2.0, rel=1e-6)

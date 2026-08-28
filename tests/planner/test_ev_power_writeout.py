"""Tests for EV write-out below-minimum-power forward redistribution (issue #845).

Before this fix, a slot whose derived AC power fell below
``charger_min_power_w`` had its energy silently discarded. Now that energy
is first offered to later pre-deadline slots with headroom.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.milp._ev_power_writeout import (
    _write_ev_power_fields_to_slots,
)

_TZ = ZoneInfo("Europe/Copenhagen")


def _make_out_slots(n: int, start: datetime) -> list[PlannedSlot]:
    slots = []
    for i in range(n):
        s = PlannedSlot(
            start=start + timedelta(hours=i), end=start + timedelta(hours=i + 1)
        )
        slots.append(s)
    return slots


def test_below_minimum_power_slot_redistributed_forward() -> None:
    """A below-minimum-power slot moves its energy to the next slot with headroom.

    Slot 0 has a tiny 0.1 kWh fragment (100 W over a 1h slot, below the
    1380 W default minimum). Slot 1 already has 4.9 kWh solved (out of a
    5.0 kWh/slot cap), leaving exactly 0.1 kWh of headroom — enough to
    absorb slot 0's fragment in full instead of discarding it.
    """
    now = datetime(2024, 6, 15, 12, 0, tzinfo=_TZ)
    start = datetime(2024, 6, 15, 14, 0, tzinfo=_TZ)  # all slots are future
    out_slots = _make_out_slots(3, start)
    future_idx = [0, 1, 2]
    m = 3

    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=10.0,
        target_kwh=20.0,
        capacity_kwh=50.0,
        max_charge_per_slot=5.0,
        charger_efficiency=1.0,
        charger_min_power_w=1380.0,
        deadline_slot=2,
    )

    executable_x = np.array([0.1, 4.9, 0.0])

    _write_ev_power_fields_to_slots(
        out_slots,
        future_idx,
        now,
        executable_x,
        m,
        1.0,  # full_slot_hours
        [ev],
        [0],  # ev_var_offsets
        1e-4,
    )

    # Slot 0's fragment was moved forward, not delivered locally.
    assert out_slots[0].ev_total_planned_load_kwh == pytest.approx(0.0)
    assert out_slots[0].ev_charger_calculated_power == pytest.approx(0.0)

    # Slot 1 absorbed the full 5.0 kWh (4.9 solved + 0.1 redistributed).
    assert out_slots[1].ev_total_planned_load_kwh == pytest.approx(5.0)
    assert out_slots[1].ev_charger_calculated_power == pytest.approx(5000.0)

    # Total EV energy delivered across the plan is conserved (5.0 kWh),
    # not silently reduced by the dropped fragment.
    total = sum(s.ev_total_planned_load_kwh for s in out_slots)
    assert total == pytest.approx(5.0)


def test_below_minimum_power_slot_discarded_when_no_headroom_anywhere() -> None:
    """Falls back to today's discard behavior when nothing has headroom."""
    now = datetime(2024, 6, 15, 12, 0, tzinfo=_TZ)
    start = datetime(2024, 6, 15, 14, 0, tzinfo=_TZ)
    out_slots = _make_out_slots(2, start)
    future_idx = [0, 1]
    m = 2

    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=10.0,
        target_kwh=20.0,
        capacity_kwh=50.0,
        max_charge_per_slot=5.0,
        charger_efficiency=1.0,
        charger_min_power_w=1380.0,
        deadline_slot=1,
    )

    # Slot 1 is already at its per-slot cap — no headroom to absorb slot 0.
    executable_x = np.array([0.1, 5.0])
    initial_import = 3.0
    out_slots[0].grid_import_kwh = initial_import

    _write_ev_power_fields_to_slots(
        out_slots,
        future_idx,
        now,
        executable_x,
        m,
        1.0,
        [ev],
        [0],
        1e-4,
    )

    assert out_slots[0].ev_total_planned_load_kwh == pytest.approx(0.0)
    assert out_slots[1].ev_total_planned_load_kwh == pytest.approx(5.0)
    # Discarded energy reduces the donor slot's grid import, as before.
    assert out_slots[0].grid_import_kwh == pytest.approx(initial_import - 0.1)

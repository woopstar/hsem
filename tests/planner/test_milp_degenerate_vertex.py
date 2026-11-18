"""Tests for the simultaneous charge+discharge resolution in the MILP write-out.

The LP may return a degenerate vertex with both ``ec`` and ``ed`` above the
action threshold (issue #662). ``_write_milp_results_to_slots`` nets the two
and clamps the result to the SoC headroom of the resolved trajectory.

These branches used to be reached only incidentally, through LP ties. The
charge-timing term (issue #1420) breaks those ties, so they are tested here
directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.milp._write_results import (
    _write_milp_results_to_slots,
)
from custom_components.hsem.utils.prices import SlotPrice

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)
_SLOT_START = datetime(2024, 6, 15, 12, 0, tzinfo=_TZ)
_USABLE_KWH = 10.0


def _write(
    *,
    ec_kwh: float,
    ed_kwh: float,
    current_kwh: float,
    discharge_reserve_kwh: float = 0.0,
) -> PlannedSlot:
    """Write one degenerate slot and return the published result."""
    slot = PlannedSlot(
        start=_SLOT_START,
        end=_SLOT_START + timedelta(hours=1),
        price=SlotPrice(import_price=0.20, export_price=0.05),
    )
    slot.discharge_reserve_kwh = discharge_reserve_kwh

    out = _write_milp_results_to_slots(
        [slot],
        future_idx=[0],
        now=_NOW,
        ec_sol=np.array([ec_kwh]),
        ed_sol=np.array([ed_kwh]),
        result_x=np.array([0.0]),
        m=1,
        battery_export_off=0,
        active_evs=[],
        ev_var_offsets=[],
        pv_avail=np.array([0.0]),
        base_load=np.array([1.0]),
        charge_eff=1.0,
        discharge_eff=1.0,
        p_exp=np.array([0.05]),
        min_export_price=0.0,
        _has_session_demand=False,
        session_slots_set=set(),
        current_kwh=current_kwh,
        usable_kwh=_USABLE_KWH,
        curt_sol_full=np.array([0.0]),
    )
    return out[0]


def test_net_charge_is_clamped_to_ceiling_headroom() -> None:
    """A net charge keeps only what fits below the ceiling."""
    slot = _write(ec_kwh=1.0, ed_kwh=0.2, current_kwh=9.5)

    assert slot.batteries_charged_kwh == pytest.approx(0.5, abs=1e-3)
    assert slot.batteries_discharged_kwh == pytest.approx(0.0)


def test_net_charge_on_a_full_battery_is_dropped() -> None:
    """A net charge with no ceiling headroom becomes no action."""
    slot = _write(ec_kwh=1.0, ed_kwh=0.2, current_kwh=_USABLE_KWH)

    assert slot.batteries_charged_kwh == pytest.approx(0.0)
    assert slot.batteries_discharged_kwh == pytest.approx(0.0)


def test_net_discharge_is_clamped_to_floor_headroom() -> None:
    """A net discharge keeps only what lies above the slot's reserve."""
    slot = _write(ec_kwh=0.2, ed_kwh=1.0, current_kwh=2.5, discharge_reserve_kwh=2.0)

    assert slot.batteries_charged_kwh == pytest.approx(0.0)
    assert slot.batteries_discharged_kwh == pytest.approx(0.5, abs=1e-3)


def test_net_discharge_at_the_reserve_is_dropped() -> None:
    """A net discharge with no floor headroom becomes no action."""
    slot = _write(ec_kwh=0.2, ed_kwh=1.0, current_kwh=2.0, discharge_reserve_kwh=2.0)

    assert slot.batteries_charged_kwh == pytest.approx(0.0)
    assert slot.batteries_discharged_kwh == pytest.approx(0.0)


def test_a_wash_vertex_becomes_no_action() -> None:
    """Equal charge and discharge net to zero and are both dropped."""
    slot = _write(ec_kwh=0.5, ed_kwh=0.5, current_kwh=5.0)

    assert slot.batteries_charged_kwh == pytest.approx(0.0)
    assert slot.batteries_discharged_kwh == pytest.approx(0.0)

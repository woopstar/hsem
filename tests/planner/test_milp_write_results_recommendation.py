"""Tests for MILP charge-slot recommendation classification (issue #913).

Before this fix, ``_write_milp_results_to_slots`` labelled any charging slot
``batteries_charge_solar`` as long as *some* forecast PV surplus existed in
the slot, even if most of the planned charge (``ec_kwh``) had to come from
grid import. The applier only configures PV self-consumption charging for
``batteries_charge_solar`` — it never enables grid charging — so the
grid-funded portion of the plan was silently dropped and the real battery
SoC diverged from the planned trajectory.

The fix requires forecast PV surplus to cover the *entire* planned charge
before assigning ``batteries_charge_solar``; otherwise the slot is labelled
``batteries_charge_grid`` so the applier opens a grid-charge window.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.milp._write_results import (
    _write_milp_results_to_slots,
)
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)
_SLOT_START = datetime(2024, 6, 15, 12, 0, tzinfo=_TZ)


def _make_single_charge_slot() -> PlannedSlot:
    return PlannedSlot(
        start=_SLOT_START,
        end=_SLOT_START + timedelta(hours=1),
        price=SlotPrice(import_price=0.20, export_price=0.05),
    )


def _write_single_slot(
    *,
    ec_kwh: float,
    pv_avail_kwh: float,
    active_evs: list[EVConfig] | None = None,
    ev_var_offsets: list[int] | None = None,
    result_x: np.ndarray | None = None,
    has_session_demand: bool = False,
    session_slots_set: set[int] | None = None,
) -> PlannedSlot:
    """Run the write-out for a single future slot and return the result."""
    slots = [_make_single_charge_slot()]
    active_evs = active_evs or []
    ev_var_offsets = ev_var_offsets or []
    if result_x is None:
        result_x = np.array([0.0])

    out_slots = _write_milp_results_to_slots(
        slots,
        future_idx=[0],
        now=_NOW,
        ec_sol=np.array([ec_kwh]),
        ed_sol=np.array([0.0]),
        result_x=result_x,
        m=1,
        battery_export_off=0,
        active_evs=active_evs,
        ev_var_offsets=ev_var_offsets,
        pv_avail=np.array([pv_avail_kwh]),
        base_load=np.array([0.0]),
        charge_eff=1.0,
        discharge_eff=1.0,
        p_exp=np.array([0.05]),
        min_export_price=0.0,
        _has_session_demand=has_session_demand,
        session_slots_set=session_slots_set or set(),
        current_kwh=0.0,
        usable_kwh=10.0,
        curt_sol_full=np.array([0.0]),
    )
    return out_slots[0]


def test_full_pv_coverage_yields_solar() -> None:
    """PV surplus covering the entire planned charge -> batteries_charge_solar."""
    slot = _write_single_slot(ec_kwh=0.5, pv_avail_kwh=0.6)
    assert slot.recommendation == Recommendations.BatteriesChargeSolar.value


def test_zero_pv_yields_grid() -> None:
    """No forecast PV surplus at all -> batteries_charge_grid."""
    slot = _write_single_slot(ec_kwh=0.5, pv_avail_kwh=0.0)
    assert slot.recommendation == Recommendations.BatteriesChargeGrid.value


def test_partial_pv_below_planned_charge_yields_grid() -> None:
    """Partial PV (less than ec_kwh) must not be mislabeled solar (issue #913).

    Reproduces the numbers from the reported issue: 0.613 kWh planned charge
    with only 0.033 kWh of forecast PV surplus — the remaining 0.58 kWh must
    come from grid import, so the slot must be labelled
    ``batteries_charge_grid`` so the applier actually enables grid charging.
    """
    slot = _write_single_slot(ec_kwh=0.613, pv_avail_kwh=0.033)
    assert slot.recommendation == Recommendations.BatteriesChargeGrid.value


def test_pv_exactly_covers_planned_charge_yields_solar() -> None:
    """PV surplus exactly equal to the planned charge (within tolerance) -> solar."""
    slot = _write_single_slot(ec_kwh=0.5, pv_avail_kwh=0.5)
    assert slot.recommendation == Recommendations.BatteriesChargeSolar.value


def test_ev_charging_slot_guard_forces_grid_even_with_full_pv_coverage() -> None:
    """EV charging in the same slot forces grid, regardless of PV coverage.

    The EV consumes the solar surplus, so the battery must draw from grid to
    actually receive the energy the MILP allocated — this guard must survive
    the PV-coverage fix unchanged.
    """
    ev = EVConfig(
        enabled=True,
        initial_soc_kwh=5.0,
        target_kwh=20.0,
        capacity_kwh=50.0,
        max_charge_per_slot=5.0,
        charger_efficiency=1.0,
    )
    # ev_c[t] lives at offset 1 in the (fake) LP vector; battery vars at 0.
    result_x = np.array([0.0, 1.0])

    slot = _write_single_slot(
        ec_kwh=0.5,
        pv_avail_kwh=0.6,  # full PV coverage for the battery alone
        active_evs=[ev],
        ev_var_offsets=[1],
        result_x=result_x,
    )
    assert slot.recommendation == Recommendations.BatteriesChargeGrid.value


def test_session_slot_guard_preserved_with_partial_pv() -> None:
    """Session-slot guard still avoids batteries_charge_grid (issue #615).

    Session EV-demand slots must never be assigned ``batteries_charge_grid``
    even though the PV-coverage fix would otherwise route this partial-PV
    slot to grid; the pre-existing session guard takes priority.
    """
    slot = _write_single_slot(
        ec_kwh=0.613,
        pv_avail_kwh=0.033,
        has_session_demand=True,
        session_slots_set={0},
    )
    assert slot.recommendation == Recommendations.BatteriesChargeSolar.value


def test_session_slot_guard_with_zero_pv_leaves_recommendation_unset() -> None:
    """Session-slot guard with no PV at all: neither solar nor grid is assigned.

    This mirrors the pre-existing (unchanged) fallthrough behaviour: the LP
    constraints already prevent ec[t] > 0 in session slots in practice, so
    this is a defensive edge case, not a normal path.
    """
    slot = _write_single_slot(
        ec_kwh=0.613,
        pv_avail_kwh=0.0,
        has_session_demand=True,
        session_slots_set={0},
    )
    assert slot.recommendation is None

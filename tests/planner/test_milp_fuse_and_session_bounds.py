"""Regressions for the hard fuse cap and live-session EV constraint rows.

These cover three defects found while backporting the fork's planner-safety
work, none of which were caught by the existing suite:

1. The hard no-worsening grid-import cap ignored forecast PV, inflating the
   cap by the whole PV forecast on sunny slots.
2. The flexible EV charge bound was not scaled down for a partly elapsed
   current slot, reserving energy the charger cannot physically deliver.
3. Slots pinned by a live charging session stayed as free columns in the EV
   SoC/deadline/post-deadline/surplus rows, making the model infeasible
   whenever the session already commits more than the remaining headroom.
"""

from __future__ import annotations

import numpy as np
import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.planner.milp._constraints import _build_constraints
from custom_components.hsem.planner.milp._layout import (
    MilpColumnLayout,
    build_milp_column_layout,
)

_M = 4
_SLOT_HOURS = 1.0


def _offsets(num_evs: int) -> MilpColumnLayout:
    """Return the canonical declared layout for *num_evs* EVs."""
    return build_milp_column_layout(_M, num_evs, fuse_active=True)


def _build(
    *,
    active_evs: list[EVConfig] | None = None,
    pv_avail: np.ndarray | None = None,
    base_load: np.ndarray | None = None,
    max_grid_import_per_slot_kwh: float = 1.5,
    fuse_active: bool = True,
    session_slots_set: set[int] | None = None,
    session_ev_indices: list[int] | None = None,
    session_dc_by_ev: dict[int, dict[int, float]] | None = None,
    available_slot_hours: np.ndarray | None = None,
) -> dict:
    """Call ``_build_constraints`` with compact, valid defaults."""
    evs = active_evs or []
    off = _offsets(len(evs))
    return _build_constraints(
        _M,
        off.column_count,
        off.offset("battery_charge"),
        off.offset("battery_discharge"),
        off.offset("grid_import"),
        off.offset("grid_export"),
        off.offset("pv"),
        off.offset("primary_throughput"),
        off.offset("curtailment"),
        off.offset("grid_import_penalty"),
        off.offset("soc_max_penalty"),
        off.offset("soc_min_penalty"),
        [off.offset(f"ev_{i}_charge") for i in range(len(evs))],
        [off.offset(f"ev_{i}_target_penalty") for i in range(len(evs))],
        evs,
        np.zeros(_M) if pv_avail is None else pv_avail,
        np.zeros(_M) if base_load is None else base_load,
        np.zeros(_M),
        1.0,
        1.0,
        0.0,
        10.0,
        2.0,
        2.0,
        max_grid_import_per_slot_kwh,
        fuse_active,
        False,
        session_slots_set or set(),
        session_ev_indices or [],
        _SLOT_HOURS,
        bool(session_ev_indices),
        battery_export_off=off.offset("primary_battery_export"),
        export_mode_off=off.offset("battery_export_mode"),
        grid_flow_mode_off=off.offset("grid_flow_mode"),
        session_dc_by_ev=session_dc_by_ev,
        available_slot_hours=available_slot_hours,
        column_layout=off,
    )


# ---------------------------------------------------------------------------
# 1. Hard aggregate fuse cap must be net of forecast PV
# ---------------------------------------------------------------------------


def test_hard_fuse_cap_is_net_of_forecast_pv() -> None:
    """PV that covers house load must not raise the hard import cap."""
    constraints = _build(
        base_load=np.full(_M, 2.0),
        pv_avail=np.full(_M, 3.0),
        max_grid_import_per_slot_kwh=1.5,
    )
    caps = constraints["hard_grid_import_cap_per_slot_kwh"]
    assert caps is not None
    # base_load - pv = -1.0 -> clamps to 0, so the fuse limit governs.
    # Before the fix this was max(1.5, 2.0) == 2.0 and let controllable
    # charging import 0.5 kWh straight through the fuse.
    assert caps == pytest.approx(np.full(_M, 1.5))


def test_hard_fuse_cap_preserves_unavoidable_overload() -> None:
    """A genuine PV-less overload stays feasible above the fuse limit."""
    constraints = _build(
        base_load=np.full(_M, 4.0),
        pv_avail=np.zeros(_M),
        max_grid_import_per_slot_kwh=1.5,
    )
    caps = constraints["hard_grid_import_cap_per_slot_kwh"]
    assert caps is not None
    assert caps == pytest.approx(np.full(_M, 4.0))


def test_hard_fuse_cap_counts_only_uncovered_load() -> None:
    """Partial PV cover leaves exactly the uncovered remainder."""
    constraints = _build(
        base_load=np.full(_M, 5.0),
        pv_avail=np.full(_M, 1.25),
        max_grid_import_per_slot_kwh=1.5,
    )
    caps = constraints["hard_grid_import_cap_per_slot_kwh"]
    assert caps is not None
    assert caps == pytest.approx(np.full(_M, 3.75))


# ---------------------------------------------------------------------------
# 2. Flexible EV bound scales with the slot's remaining time
# ---------------------------------------------------------------------------


def _ev(**kwargs: object) -> EVConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "capacity_kwh": 60.0,
        "target_kwh": 40.0,
        "initial_soc_kwh": 10.0,
        "max_charge_per_slot": 4.0,
        "charger_efficiency": 1.0,
    }
    defaults.update(kwargs)
    return EVConfig(**defaults)  # type: ignore[arg-type]


def test_flexible_ev_bound_scales_with_remaining_slot_time() -> None:
    """A quarter-elapsed current slot may only charge a quarter as much."""
    available = np.array([0.25, 1.0, 1.0, 1.0])
    constraints = _build(
        active_evs=[_ev()],
        max_grid_import_per_slot_kwh=1000.0,
        available_slot_hours=available,
    )
    off = _offsets(1)
    ev_off = off.offset("ev_0_charge")
    bounds = constraints["bounds"]
    # Slot 0 is only 25 % available -> 4.0 kWh * 0.25.
    assert bounds[ev_off][1] == pytest.approx(1.0)
    for t in range(1, _M):
        assert bounds[ev_off + t][1] == pytest.approx(4.0)


def test_flexible_ev_bound_defaults_to_full_slot() -> None:
    """Omitting available_slot_hours keeps the untouched full-slot ceiling."""
    constraints = _build(active_evs=[_ev()], max_grid_import_per_slot_kwh=1000.0)
    off = _offsets(1)
    ev_off = off.offset("ev_0_charge")
    bounds = constraints["bounds"]
    for t in range(_M):
        assert bounds[ev_off + t][1] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# 3. Live-session slots must not make the EV rows infeasible
# ---------------------------------------------------------------------------


def _pinned_dc(ev: EVConfig) -> float:
    """Return the DC energy a full-hour session slot pins for *ev*."""
    return min(
        max(float(ev.session_charge_kw or 0.0), 0.0) * 1.0 * ev.charger_efficiency,
        ev.max_charge_per_slot,
    )


def _session_build(ev: EVConfig) -> dict:
    """Build with a 2-slot live session pinned on the single EV."""
    pinned = _pinned_dc(ev)
    return _build(
        active_evs=[ev],
        max_grid_import_per_slot_kwh=1000.0,
        session_slots_set={0, 1},
        session_ev_indices=[0],
        session_dc_by_ev={0: {0: pinned, 1: pinned}},
    )


def _assert_rows_feasible_against_fixed_bounds(constraints: dict) -> None:
    """Assert every monotone row can still be satisfied by the fixed bounds.

    A row whose coefficients are all non-negative is minimised by putting each
    variable at its lower bound.  Session-pinned EV slots have lower == upper,
    so if that minimum already exceeds the row's right-hand side the whole
    model is infeasible and ``solve_milp`` returns ``None`` for the cycle.
    """
    a_ub = constraints["A_ub"]
    b_ub = constraints["b_ub"]
    lowers = np.array([lo for lo, _hi in constraints["bounds"]], dtype=float)
    for row in range(a_ub.shape[0]):
        coeffs = a_ub[row]
        if (coeffs < -1e-12).any():
            # Soft rows carry negative coefficients and a penalty column; a
            # negative right-hand side is legitimate there.
            continue
        if not (coeffs > 1e-12).any():
            continue
        minimum = float(coeffs @ lowers)
        assert minimum <= float(b_ub[row]) + 1e-6, (
            f"row {row} is infeasible: fixed lower bounds force "
            f"{minimum:.3f} but the row allows only {float(b_ub[row]):.3f}"
        )


def test_session_energy_leaves_soc_row_feasible_near_full() -> None:
    """A session on a nearly-full EV must not make the SoC row infeasible."""
    # headroom = 60 - 59 = 1.0 kWh, but the session commits 3.0 kWh/slot.
    ev = _ev(capacity_kwh=60.0, initial_soc_kwh=59.0, session_charge_kw=3.0)
    _assert_rows_feasible_against_fixed_bounds(_session_build(ev))


def test_session_energy_leaves_target_row_feasible_near_target() -> None:
    """A session past the target shortfall must not make that row infeasible."""
    ev = _ev(
        capacity_kwh=60.0,
        initial_soc_kwh=39.0,
        target_kwh=40.0,
        deadline_slot=3,
        session_charge_kw=3.0,
    )
    _assert_rows_feasible_against_fixed_bounds(_session_build(ev))


def test_session_slots_excluded_from_post_deadline_zero_rows() -> None:
    """A session slot after the deadline keeps its pinned energy."""
    ev = _ev(
        capacity_kwh=60.0,
        initial_soc_kwh=10.0,
        target_kwh=40.0,
        deadline_slot=0,
        session_charge_kw=3.0,
    )
    constraints = _session_build(ev)
    off = _offsets(1)
    ev_off = off.offset("ev_0_charge")
    a_ub = constraints["A_ub"]
    b_ub = constraints["b_ub"]
    # Slot 1 is both post-deadline and session-pinned: no row may force it to
    # zero, because its bounds already fix it to a positive value.
    pinned_col = ev_off + 1
    for row in range(a_ub.shape[0]):
        if a_ub[row, pinned_col] > 0.0 and b_ub[row] <= 1e-9:
            others = np.count_nonzero(a_ub[row]) - 1
            assert others > 0, (
                f"row {row} forces session-pinned slot 1 to zero while its "
                "bounds fix it to positive session energy"
            )


def test_session_slots_excluded_from_past_target_surplus_rows() -> None:
    """Charge-past-target surplus rule must not apply to pinned session slots."""
    ev = _ev(
        capacity_kwh=60.0,
        initial_soc_kwh=10.0,
        target_kwh=40.0,
        charge_past_target=True,
        session_charge_kw=3.0,
    )
    # No PV at all: a surplus row over a pinned slot would be 0 >= positive.
    pinned = _pinned_dc(ev)
    constraints = _build(
        active_evs=[ev],
        max_grid_import_per_slot_kwh=1000.0,
        pv_avail=np.zeros(_M),
        base_load=np.full(_M, 1.0),
        session_slots_set={0, 1},
        session_ev_indices=[0],
        session_dc_by_ev={0: {0: pinned, 1: pinned}},
    )
    off = _offsets(1)
    ev_off = off.offset("ev_0_charge")
    a_ub = constraints["A_ub"]
    b_ub = constraints["b_ub"]
    bounds = constraints["bounds"]
    for t in (0, 1):
        pinned_low = bounds[ev_off + t][0]
        assert pinned_low > 0.0
        for row in range(a_ub.shape[0]):
            coeff = a_ub[row, ev_off + t]
            if coeff > 0.0 and np.count_nonzero(a_ub[row]) == 1:
                assert b_ub[row] + 1e-9 >= coeff * pinned_low, (
                    f"row {row} is infeasible against the pinned session "
                    f"energy in slot {t}"
                )

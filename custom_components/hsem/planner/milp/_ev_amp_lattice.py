"""Solver-native whole-amp EV charging lattice (issue #797).

Links each managed EV's DC-side charge energy ``ev_c[t]`` to an executable
whole-amp charger command via a semi-integer LP variable (``ev_{i}_amps``),
so a solved plan can never diverge from what the charger can actually
execute.  A charger cannot run below its configured minimum, so the amp
variable is zero-or-``[min_amp, rated_amp]`` — never a fractional amp.

Also gates the house battery's discharge whenever an EV lacks (or exceeds)
its Huawei discharge permission for a slot: Huawei exposes one global
discharge limit, so an EV charging without permission (or above its
configured ceiling) must force primary battery discharge in that slot down
to the EV's permitted ceiling (zero when no permission was granted).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from custom_components.hsem.planner.milp._layout import Bound, MilpBoundsBuilder
from custom_components.hsem.utils.phase_power import (
    charger_current_to_power_w,
    charger_min_power_to_current_a,
    charger_power_to_current_a,
)

if TYPE_CHECKING:
    from custom_components.hsem.models.ev_config import EVConfig


def ev_discharge_cap_kwh(ev: EVConfig, slot_hours: float) -> float:
    """Return this EV's fail-closed battery-side discharge ceiling per slot.

    Zero (fail-closed) unless the EV has explicitly opted in via
    ``force_max_discharge_power`` with a finite, positive ceiling.
    """
    if not bool(ev.force_max_discharge_power):
        return 0.0
    try:
        cap_w = float(ev.max_discharge_power_w)
    except TypeError, ValueError:
        return 0.0
    if not math.isfinite(cap_w) or cap_w <= 0.0:
        return 0.0
    return cap_w * max(float(slot_hours), 0.0) / 1000.0


def ev_has_live_session(ev: EVConfig) -> bool:
    """Return whether finite positive charger telemetry proves a live session."""
    try:
        session_kw = (
            float(ev.session_charge_kw) if ev.session_charge_kw is not None else 0.0
        )
    except TypeError, ValueError:
        return False
    return math.isfinite(session_kw) and session_kw > 1e-9


@dataclass(frozen=True)
class EvAmpSpec:
    """Resolved whole-amp lattice parameters for one active EV."""

    ev_idx: int
    managed: bool
    minimum_current_a: int
    rated_current_a: int
    runnable: bool
    discharge_cap_kwh: float
    needs_on: bool
    has_live_session: bool


@dataclass(frozen=True)
class EvAmpPlan:
    """Per-EV amp-lattice specs and the column widths they require."""

    specs: list[EvAmpSpec]

    def amp_widths(self, m: int) -> list[int | None]:
        """Return each EV's ``ev_{i}_amps`` column width, or ``None``."""
        return [m if spec.managed else None for spec in self.specs]

    def on_widths(self, m: int) -> list[int | None]:
        """Return each EV's ``ev_{i}_on`` column width, or ``None``."""
        return [m if (spec.managed and spec.needs_on) else None for spec in self.specs]


def resolve_ev_amp_plan(
    active_evs: list[EVConfig],
    *,
    max_dis: float,
    slot_hours: float,
) -> EvAmpPlan:
    """Resolve the amp-lattice plan for every active EV.

    Must run before the column layout is declared: the layout needs to know
    which EVs get an ``ev_{i}_amps`` / ``ev_{i}_on`` block before any offset
    can be assigned.
    """
    specs: list[EvAmpSpec] = []
    for ev_idx, ev in enumerate(active_evs):
        managed = not ev.fixed_session_only
        discharge_cap_kwh = ev_discharge_cap_kwh(ev, slot_hours)
        if not managed:
            specs.append(
                EvAmpSpec(
                    ev_idx=ev_idx,
                    managed=False,
                    minimum_current_a=0,
                    rated_current_a=0,
                    runnable=False,
                    discharge_cap_kwh=discharge_cap_kwh,
                    needs_on=False,
                    has_live_session=ev_has_live_session(ev),
                )
            )
            continue
        # EVConfig.max_charge_per_slot is already the exact executable DC
        # envelope. Floor here so a direct exact-energy caller can never
        # gain capacity from a second round-to-nearest conversion.
        ev_bound_ac_power_w = (
            ev.max_charge_per_slot
            / max(ev.charger_efficiency, 0.01)
            / max(slot_hours, 1e-9)
            * 1000.0
        )
        rated_current_a = charger_power_to_current_a(
            ev_bound_ac_power_w, ev.charger_phase_topology
        )
        minimum_current_a = max(
            charger_min_power_to_current_a(
                ev.charger_min_power_w, ev.charger_phase_topology
            ),
            1,
        )
        runnable = rated_current_a >= minimum_current_a
        # A conditional discharge-permission binary is only useful when this
        # EV can actually command a positive amp; an EV pinned to (0, 0)
        # amps (e.g. a managed_session_cap_only sentinel) never activates
        # it, so skip the wasted binary/rows entirely.
        needs_on = runnable and discharge_cap_kwh < max_dis - 1e-9
        specs.append(
            EvAmpSpec(
                ev_idx=ev_idx,
                managed=True,
                minimum_current_a=minimum_current_a,
                rated_current_a=rated_current_a,
                runnable=runnable,
                discharge_cap_kwh=discharge_cap_kwh,
                needs_on=needs_on,
                has_live_session=ev_has_live_session(ev),
            )
        )
    return EvAmpPlan(specs=specs)


def write_ev_amp_bounds(
    bounds_builder: MilpBoundsBuilder,
    plan: EvAmpPlan,
    *,
    m: int,
) -> None:
    """Write ``ev_{i}_amps`` / ``ev_{i}_on`` bounds for every planned EV."""
    for spec in plan.specs:
        if not spec.managed:
            continue
        amp_bound: Bound = (
            (float(spec.minimum_current_a), float(spec.rated_current_a))
            if spec.runnable
            else (0.0, 0.0)
        )
        bounds_builder.set(f"ev_{spec.ev_idx}_amps", [amp_bound] * m)
        if spec.needs_on:
            bounds_builder.fill(f"ev_{spec.ev_idx}_on", (0.0, 1.0))


def add_ev_amp_lattice_constraints(
    constraints: dict[str, Any],
    plan: EvAmpPlan,
    active_evs: list[EVConfig],
    *,
    n_vars: int,
    m: int,
    ev_var_offsets: list[int],
    ev_amp_offsets: list[int | None],
    ev_on_offsets: list[int | None],
    ed_off: int,
    max_dis: float,
    available_slot_hours: np.ndarray,  # type: ignore[type-arg]
    session_dc_by_ev: dict[int, dict[int, float]],
) -> dict[str, Any]:
    """Link managed EV energy to executable whole-amp commands.

    Adds one equality row per managed-EV slot linking ``ev_c[t]`` to the
    semi-integer amp variable, plus (when the EV's discharge permission is
    restrictive) three inequality rows per slot conditionally capping
    primary battery discharge while that EV draws current.  A live
    session's already-flowing current also caps discharge directly on the
    current slot, independent of any amp decision.
    """
    managed_specs = [spec for spec in plan.specs if spec.managed]
    physical_session_caps: list[tuple[int, float]] = [
        (spec.ev_idx, spec.discharge_cap_kwh)
        for spec in managed_specs
        if spec.has_live_session and spec.discharge_cap_kwh < max_dis - 1e-9 and m > 0
    ]
    if not managed_specs and not physical_session_caps:
        return constraints

    old_a_eq = constraints["A_eq"]
    old_b_eq = constraints["b_eq"]
    old_a_ub = constraints["A_ub"]
    old_b_ub = constraints["b_ub"]

    equality_rows = len(managed_specs) * m
    on_rows = sum(3 * m for spec in managed_specs if spec.needs_on)
    session_rows = len(physical_session_caps)

    a_eq = np.zeros((old_a_eq.shape[0] + equality_rows, n_vars))
    b_eq = np.zeros(old_b_eq.shape[0] + equality_rows)
    a_eq[: old_a_eq.shape[0], : old_a_eq.shape[1]] = old_a_eq
    b_eq[: old_b_eq.shape[0]] = old_b_eq

    a_ub = np.zeros((old_a_ub.shape[0] + on_rows + session_rows, n_vars))
    b_ub = np.zeros(old_b_ub.shape[0] + on_rows + session_rows)
    a_ub[: old_a_ub.shape[0], : old_a_ub.shape[1]] = old_a_ub
    b_ub[: old_b_ub.shape[0]] = old_b_ub

    eq_row = old_a_eq.shape[0]
    ub_row = old_a_ub.shape[0]
    for spec in managed_specs:
        ev = active_evs[spec.ev_idx]
        amp_off = ev_amp_offsets[spec.ev_idx]
        assert amp_off is not None
        ev_off = ev_var_offsets[spec.ev_idx]
        on_off = ev_on_offsets[spec.ev_idx]
        for t in range(m):
            one_amp_dc_kwh = (
                charger_current_to_power_w(1, ev.charger_phase_topology)
                * max(float(available_slot_hours[t]), 0.0)
                * ev.charger_efficiency
                / 1000.0
            )
            # ev_c[t] - one_amp_dc_kwh * amp[t] = 0
            a_eq[eq_row, ev_off + t] = 1.0
            a_eq[eq_row, amp_off + t] = -one_amp_dc_kwh
            eq_row += 1

            if on_off is not None:
                # A restrictive (or zero) discharge ceiling needs an exact
                # conditional cap. Link the semi-integer amp variable to one
                # binary, then activate ed <= cap only while this EV has a
                # non-zero command:
                #   amp <= rated*on
                #   min*on <= amp
                #   ed + (max_dis-cap)*on <= max_dis
                a_ub[ub_row, amp_off + t] = 1.0
                a_ub[ub_row, on_off + t] = -float(spec.rated_current_a)
                ub_row += 1
                a_ub[ub_row, on_off + t] = float(spec.minimum_current_a)
                a_ub[ub_row, amp_off + t] = -1.0
                ub_row += 1
                a_ub[ub_row, ed_off + t] = 1.0
                a_ub[ub_row, on_off + t] = max_dis - spec.discharge_cap_kwh
                b_ub[ub_row] = max_dis
                ub_row += 1

    # A live session's already-flowing current is physical evidence for
    # Huawei's global discharge policy even when the optimiser commands
    # zero amps for future slots — it caps the current slot directly.
    for _ev_idx, discharge_cap_kwh in physical_session_caps:
        a_ub[ub_row, ed_off + 0] = 1.0
        b_ub[ub_row] = discharge_cap_kwh
        ub_row += 1

    constraints["A_eq"] = a_eq
    constraints["b_eq"] = b_eq
    constraints["A_ub"] = a_ub
    constraints["b_ub"] = b_ub
    return constraints


def ev_amp_integrality(
    plan: EvAmpPlan,
    *,
    n_vars: int,
    ev_amp_offsets: list[int | None],
    ev_on_offsets: list[int | None],
    m: int,
) -> np.ndarray:  # type: ignore[type-arg]
    """Return the integrality contribution for amp/on columns.

    Type 3 (semi-integer) for amp columns: zero, or an integer in
    ``[min_amp, rated_amp]``.  Type 1 (integer) for on columns: a plain 0/1
    binary.
    """
    integrality = np.zeros(n_vars, dtype=int)
    for spec in plan.specs:
        amp_off = ev_amp_offsets[spec.ev_idx]
        if amp_off is not None:
            integrality[amp_off : amp_off + m] = 3
        on_off = ev_on_offsets[spec.ev_idx]
        if on_off is not None:
            integrality[on_off : on_off + m] = 1
    return integrality


def target_cap_activation_quantum_dc(
    ev: EVConfig,
    *,
    d: int,
    available_slot_hours: np.ndarray,  # type: ignore[type-arg]
) -> float:
    """Return the largest single-slot activation-quantum energy up to slot *d*.

    The EV target-cap constraint (``planner/milp/_constraints.py``) permits
    cumulative pre-deadline charge up to one activation quantum above the
    exact economic shortfall: whole-amp hardware may have no executable
    point exactly at the remaining need, and a strict cap would report an
    avoidable deadline miss (issue #797).
    """
    activation_current_a = max(
        charger_min_power_to_current_a(
            ev.charger_min_power_w, ev.charger_phase_topology
        ),
        1,
    )
    activation_power_w = charger_current_to_power_w(
        activation_current_a, ev.charger_phase_topology
    )
    return max(
        (
            activation_power_w
            * float(available_slot_hours[k])
            * ev.charger_efficiency
            / 1000.0
            for k in range(d + 1)
        ),
        default=0.0,
    )

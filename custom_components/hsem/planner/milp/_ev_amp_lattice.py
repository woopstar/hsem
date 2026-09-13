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
    EV_TOPOLOGY_SINGLE_PHASE,
    EV_TOPOLOGY_THREE_PHASE_BALANCED,
    EV_TOPOLOGY_THREE_PHASE_SWITCHABLE,
    charger_current_to_power_w,
    charger_power_to_current_a,
    ev_min_start_current_a,
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
    #: ``True`` for an auto-phase-switching charger (issue #1001): the
    #: lattice gets a second amp block (``ev_{i}_amps3``, three-phase mode)
    #: and a phase-mode binary (``ev_{i}_mode3``) so exactly one mode is
    #: active per slot.  One-phase mode spans
    #: ``[minimum_current_a, rated_current_a]`` amps at 1× phase voltage;
    #: three-phase mode spans the same amp range at 3× phase voltage.
    switchable: bool = False


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

    def amp3_widths(self, m: int) -> list[int | None]:
        """Return each EV's ``ev_{i}_amps3`` column width, or ``None``."""
        return [
            m if (spec.managed and spec.switchable) else None for spec in self.specs
        ]

    def mode3_widths(self, m: int) -> list[int | None]:
        """Return each EV's ``ev_{i}_mode3`` column width, or ``None``."""
        return [
            m if (spec.managed and spec.switchable) else None for spec in self.specs
        ]


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
        switchable = ev.charger_phase_topology == EV_TOPOLOGY_THREE_PHASE_SWITCHABLE
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
        # An auto-phase-switching charger (issue #1001) starts at its
        # minimum on ONE phase (6 A × 230 V = 1380 W) and only reaches its
        # nameplate across THREE phases — so the minimum amp floor converts
        # on a single-phase basis while the rated amp ceiling converts on a
        # three-phase basis.
        rated_current_a = charger_power_to_current_a(
            ev_bound_ac_power_w,
            (
                EV_TOPOLOGY_THREE_PHASE_BALANCED
                if switchable
                else ev.charger_phase_topology
            ),
        )
        minimum_current_a = ev_min_start_current_a(
            ev.charger_min_power_w,
            EV_TOPOLOGY_SINGLE_PHASE if switchable else ev.charger_phase_topology,
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
                switchable=switchable,
            )
        )
    return EvAmpPlan(specs=specs)


def write_ev_amp_bounds(
    bounds_builder: MilpBoundsBuilder,
    plan: EvAmpPlan,
    *,
    m: int,
) -> None:
    """Write ``ev_{i}_amps``/``ev_{i}_amps3``/``ev_{i}_mode3``/``ev_{i}_on`` bounds."""
    for spec in plan.specs:
        if not spec.managed:
            continue
        amp_bound: Bound = (
            (float(spec.minimum_current_a), float(spec.rated_current_a))
            if spec.runnable
            else (0.0, 0.0)
        )
        bounds_builder.set(f"ev_{spec.ev_idx}_amps", [amp_bound] * m)
        if spec.switchable:
            bounds_builder.set(f"ev_{spec.ev_idx}_amps3", [amp_bound] * m)
            bounds_builder.fill(f"ev_{spec.ev_idx}_mode3", (0.0, 1.0))
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
    ev_amp3_offsets: list[int | None] | None = None,
    ev_mode3_offsets: list[int | None] | None = None,
) -> dict[str, Any]:
    """Link managed EV energy to executable whole-amp commands.

    Adds one equality row per managed-EV slot linking ``ev_c[t]`` to the
    semi-integer amp variable, plus (when the EV's discharge permission is
    restrictive) three inequality rows per slot conditionally capping
    primary battery discharge while that EV draws current.  A live
    session's already-flowing current also caps discharge directly on the
    current slot, independent of any amp decision.

    An auto-phase-switching charger (``three_phase_switchable``, issue
    #1001) gets a second amp variable ``a3[t]`` (three-phase mode) and a
    phase-mode binary ``mode3[t]`` per slot::

        ev_c[t] = k1·a1[t] + k3·a3[t]
        a1[t] + rated·mode3[t] ≤ rated   (one-phase amps only when mode3=0)
        a3[t] − rated·mode3[t] ≤ 0       (three-phase amps only when mode3=1)

    with ``k1``/``k3`` the DC energy of one amp on one/three phases.  Both
    amp variables keep the semi-integer zero-or-[min, rated] domain and the
    mode rows guarantee at most one mode is active per slot, so the
    executable power set is exactly ``{a×230 V} ∪ {a×690 V}`` for whole amps
    in ``[min, rated]``.
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
    mode_rows = sum(2 * m for spec in managed_specs if spec.switchable)
    session_rows = len(physical_session_caps)

    a_eq = np.zeros((old_a_eq.shape[0] + equality_rows, n_vars))
    b_eq = np.zeros(old_b_eq.shape[0] + equality_rows)
    a_eq[: old_a_eq.shape[0], : old_a_eq.shape[1]] = old_a_eq
    b_eq[: old_b_eq.shape[0]] = old_b_eq

    a_ub = np.zeros((old_a_ub.shape[0] + on_rows + mode_rows + session_rows, n_vars))
    b_ub = np.zeros(old_b_ub.shape[0] + on_rows + mode_rows + session_rows)
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
        amp3_off = ev_amp3_offsets[spec.ev_idx] if ev_amp3_offsets else None
        mode3_off = ev_mode3_offsets[spec.ev_idx] if ev_mode3_offsets else None
        for t in range(m):
            one_amp_dc_kwh = (
                charger_current_to_power_w(
                    1,
                    (
                        EV_TOPOLOGY_SINGLE_PHASE
                        if spec.switchable
                        else ev.charger_phase_topology
                    ),
                )
                * max(float(available_slot_hours[t]), 0.0)
                * ev.charger_efficiency
                / 1000.0
            )
            # ev_c[t] - one_amp_dc_kwh * amp[t] = 0
            a_eq[eq_row, ev_off + t] = 1.0
            a_eq[eq_row, amp_off + t] = -one_amp_dc_kwh
            if spec.switchable:
                # Second amp variable carries the three-phase mode: one amp
                # on three phases delivers PHASE_COUNT× the DC energy.
                assert amp3_off is not None  # declared for every switchable EV
                a_eq[eq_row, amp3_off + t] = -one_amp_dc_kwh * 3.0
            eq_row += 1

            if spec.switchable:
                assert mode3_off is not None and amp3_off is not None
                # a1[t] + rated·mode3[t] ≤ rated — one-phase amps only when
                # mode3 = 0.
                a_ub[ub_row, amp_off + t] = 1.0
                a_ub[ub_row, mode3_off + t] = float(spec.rated_current_a)
                b_ub[ub_row] = float(spec.rated_current_a)
                ub_row += 1
                # a3[t] − rated·mode3[t] ≤ 0 — three-phase amps only when
                # mode3 = 1.
                a_ub[ub_row, amp3_off + t] = 1.0
                a_ub[ub_row, mode3_off + t] = -float(spec.rated_current_a)
                ub_row += 1

            if on_off is not None:
                # A restrictive (or zero) discharge ceiling needs an exact
                # conditional cap. Link the semi-integer amp variable(s) to
                # one binary, then activate ed <= cap only while this EV has
                # a non-zero command:
                #   total_amps <= rated*on
                #   min*on <= total_amps
                #   ed + (max_dis-cap)*on <= max_dis
                # For a switchable charger total_amps is a1+a3 — the mode
                # rows above guarantee at most one of them is non-zero.
                a_ub[ub_row, amp_off + t] = 1.0
                if spec.switchable:
                    assert amp3_off is not None  # declared for switchable EVs
                    a_ub[ub_row, amp3_off + t] = 1.0
                a_ub[ub_row, on_off + t] = -float(spec.rated_current_a)
                ub_row += 1
                a_ub[ub_row, on_off + t] = float(spec.minimum_current_a)
                a_ub[ub_row, amp_off + t] = -1.0
                if spec.switchable:
                    assert amp3_off is not None  # declared for switchable EVs
                    a_ub[ub_row, amp3_off + t] = -1.0
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
    ev_amp3_offsets: list[int | None] | None = None,
    ev_mode3_offsets: list[int | None] | None = None,
) -> np.ndarray:  # type: ignore[type-arg]
    """Return the integrality contribution for amp/on columns.

    Type 3 (semi-integer) for amp columns: zero, or an integer in
    ``[min_amp, rated_amp]``.  Type 1 (integer) for on columns: a plain 0/1
    binary.  A switchable charger's ``amps3`` block is likewise
    semi-integer and its ``mode3`` block binary (issue #1001).
    """
    integrality = np.zeros(n_vars, dtype=int)
    for spec in plan.specs:
        amp_off = ev_amp_offsets[spec.ev_idx]
        if amp_off is not None:
            integrality[amp_off : amp_off + m] = 3
        on_off = ev_on_offsets[spec.ev_idx]
        if on_off is not None:
            integrality[on_off : on_off + m] = 1
        if ev_amp3_offsets is not None:
            amp3_off = ev_amp3_offsets[spec.ev_idx]
            if amp3_off is not None:
                integrality[amp3_off : amp3_off + m] = 3
        if ev_mode3_offsets is not None:
            mode3_off = ev_mode3_offsets[spec.ev_idx]
            if mode3_off is not None:
                integrality[mode3_off : mode3_off + m] = 1
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
    activation_current_a = ev_min_start_current_a(
        ev.charger_min_power_w, ev.charger_phase_topology
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

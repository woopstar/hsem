"""MILP-based optimal battery charge/discharge scheduler.

Formulated as a continuous LP via ``scipy.optimize.linprog`` with HiGHS.
Binary flags relaxed to continuous; mutex constraint prevents
simultaneous charge+discharge.

Pure Python, no HA imports — testable with plain pytest.  Constraint,
bounds, objective, diagnostics, write-out and fuse setup live in the
``planner/milp/`` submodules so this orchestrator stays under the
repository's 30 KB file limit.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.planner._scipy_probe import (  # noqa: F401
    is_scipy_available,
)
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import clamp_efficiency

if TYPE_CHECKING:
    from custom_components.hsem.models.planned_slot import PlannedSlot

# Name exported so the engine and tests can reference it without re-defining
CANDIDATE_MILP = "milp"

# Solver timeout in seconds — HiGHS respects this via ``options``.
# Increased from 0.5 to 2.0 to handle 192-slot (768 variable) problems
# where preprocessing overhead alone can reach 200-400ms.
_SOLVER_TIME_LIMIT_S = 2.0

# Minimum energy threshold below which a slot is treated as zero-charge/discharge
# to avoid writing tiny floating-point artefacts into recommendations.
_MIN_ACTION_KWH = 1e-4


def solve_milp(
    slots: list[PlannedSlot],
    now: datetime,
    current_kwh: float,
    usable_kwh: float,
    max_charge_per_slot: float,
    max_discharge_per_slot: float | None,
    cycle_cost_per_kwh: float = 0.0,
    charge_efficiency_pct: float = 97.0,
    discharge_efficiency_pct: float = 97.0,
    time_discount_rate: float = 1.0,
    replacement_price_per_kwh: float | None = None,
    *,
    min_export_price: float = 0.0,
    ev_configs: list[EVConfig] | None = None,
    no_export: bool = False,
    main_fuse_amps: float | None = None,
    main_fuse_phases: int = 3,
    max_grid_export_power_kw: float | None = None,
    battery_export_min_price: float = 0.0,
    excess_export_discharge_buffer_pct: float = 0.0,
) -> tuple[list[PlannedSlot], dict] | None:
    """Solve the LP and return a deep-copy slot list with MILP recommendations.

    The returned list is independent of *slots* — it is safe to mutate without
    affecting the caller's data.  Fields written by the MILP are:

    - ``recommendation``  — one of ``BatteriesChargeGrid``, ``BatteriesDischargeMode``,
      ``ForceBatteriesDischarge``, or ``None`` (idle).
    - ``batteries_charged_kwh`` — energy entering the battery this slot (kWh).
    - ``batteries_discharged_kwh`` — energy discharged from the battery this slot
      (kWh).  Derived from the **resolved** ed after mutex resolution — this is the
      source of truth and must not be re-derived by the SoC simulation.
    - ``grid_import_kwh`` — grid import this slot (kWh), derived from the energy
      balance equation using the resolved ec/ed values.
    - ``grid_export_kwh`` — grid export this slot (kWh), derived from the energy
      balance equation using the resolved ec/ed values.
    - ``ev_planned_load_kwh`` — EV AC load that must be added to base consumption
      (when ``ev_configs`` is provided and ``base_load_includes_ev`` is False).
    - ``ev_accounted_load_kwh`` — EV AC load already captured in house consumption
      (when ``ev_configs`` is provided and ``base_load_includes_ev`` is True).
    - ``ev_total_planned_load_kwh`` — total EV AC load (sum of planned + accounted).
    - ``ev_charger_calculated_power`` — target AC power (W) for the primary EV charger.
    - ``ev_second_charger_calculated_power`` — target AC power (W) for the second EV.
    - ``estimated_net_consumption_kwh`` — recomputed after EV decisions.
    - ``estimated_cost_currency`` — recomputed after EV decisions.

    The SoC simulation (:func:`~soc_simulation.simulate_soc`) must be run
    by the caller **after** receiving these slots with
    ``milp_prepopulated=True`` to populate ``estimated_battery_soc``
    and ``estimated_battery_capacity_kwh`` while preserving the LP-derived
    energy flow fields.

    The energy balance accounts for charge/discharge efficiencies so ``gi[t]``
    and ``ge[t]`` contain the real AC draw and delivery. Their money terms price
    conversion loss exactly once; no separate loss coefficient is added.

    Args:
        slots:
            Fully populated (pre-SoC-simulation) slot list from the engine.
            Past slots with recommendation ``TimePassed`` are treated as fixed
            (zero charge/discharge) and excluded from the LP.
        now:
            Timezone-aware current datetime used to identify past slots.
        current_kwh:
            Battery energy above the discharge floor at the start of the horizon
            (kWh).  This is the LP's initial SoC state.
        usable_kwh:
            Maximum usable energy (max_soc − min_soc, kWh).  Acts as the SoC
            upper bound.
        max_charge_per_slot:
            Maximum energy chargeable per slot (kWh, post-conversion-loss).
        max_discharge_per_slot:
            Maximum energy dischargeable per slot (kWh).  ``None`` means unlimited;
            the LP uses ``usable_kwh`` as the effective ceiling in that case.
        cycle_cost_per_kwh:
            Battery cycle (depreciation) cost per kWh cycled.  Defaults to 0.0.
        charge_efficiency_pct:
            Charge-side efficiency as a percentage (0-100).  Energy stored in
            the battery equals input energy x (charge_efficiency_pct / 100).
            Defaults to 97 % (3 % charge-side loss).
        discharge_efficiency_pct:
            Discharge-side efficiency as a percentage (0-100).  Energy delivered
            to the house equals battery energy removed x (discharge_efficiency_pct / 100).
            Defaults to 97 % (3 % discharge-side loss).
        replacement_price_per_kwh:
            Terminal-SoC replacement price (currency/kWh) used to value the
            opportunity cost of ending the horizon with less stored energy.
            Passed from the engine (computed from the next discharge window).
            ``None`` disables the terminal-SoC credit term.
        min_export_price:
            Minimum export price (local currency/kWh) for the combined
            battery-export floor.  Caps ``ed[t]`` to
            ``base_load[t] / discharge_eff`` on slots where the export
            price is below the floor, and decides between
            ``ForceBatteriesDischarge`` and ``BatteriesDischargeMode`` in
            post-processing.  Defaults to 0.0.
        ev_configs:
            Optional list of :class:`EVConfig` objects (one per EV).  When
            provided, the MILP co-optimises EV charging alongside the battery
            with deadline-target soft constraints; ``None`` (default) uses
            pre-computed ``ev_planned_load_kwh`` as fixed inputs.
        no_export:
            When ``True``, caps battery discharge per slot so the battery
            never exports to the grid — it only serves house load.  The
            per-slot cap is ``ed[t] ≤ base_load[t] / discharge_eff``.
        main_fuse_amps:
            Main fuse/breaker rating in amps.  When provided and > 0, a soft
            constraint limits total grid import power per slot to
            ``main_fuse_amps * 230 * main_fuse_phases / 1000`` kWh per hour.
            A penalty variable ``gi_pen[t]`` absorbs any excess, preventing
            infeasibility when house base load alone exceeds the fuse rating.
            When EV co-optimisation is also active, hard per-phase rows are
            added (see ``_phase_fuse.py`` and the planner spec).
            ``None`` or 0 disables the constraint (identical to current behaviour).
        main_fuse_phases:
            Electrical phase count (1 or 3).  Used as the multiplier in the
            max-grid-import formula above.  Defaults to 3 (three-phase).
            Single-phase installations MUST use 1.
        max_grid_export_power_kw:
            DNO/inverter grid export cap in kW (issue #726).  When > 0, the
            per-slot ``ge[t]`` is hard-bounded to
            ``max_grid_export_power_kw * slot_hours`` kWh.  ``None`` or 0
            disables the bound.
        battery_export_min_price:
            Per-slot hard floor below which intentional battery-to-grid
            discharge is forbidden (issue #752).  ``0.0`` disables it.

    Returns:
        A tuple ``(slots, diagnostics)`` where ``slots`` is a list of
        :class:`PlannedSlot` copies with MILP-derived recommendations and
        ``diagnostics`` carries penalty/fuse/EV keys (see
        ``_diagnostics.py``).  Returns ``None`` if the solver fails
        (unrelated to constraint violations — e.g., solver crash or
        numerical issue).
    """
    log_planner(
        "debug",
        "[milp] solve_milp  slots=%d  current=%.3f  usable=%.3f  "
        "max_chg=%.3f  max_dis=%s  cycle_cost=%.6f  "
        "chg_eff=%.2f  dis_eff=%.2f  discount=%.4f  repl_price=%s  "
        "no_export=%s  min_export_price=%.4f  battery_export_min_price=%.4f  "
        "fuse=%s",
        len(slots),
        current_kwh,
        usable_kwh,
        max_charge_per_slot,
        f"{max_discharge_per_slot:.3f}" if max_discharge_per_slot is not None else "∞",
        cycle_cost_per_kwh,
        charge_efficiency_pct,
        discharge_efficiency_pct,
        time_discount_rate,
        (
            f"{replacement_price_per_kwh:.6f}"
            if replacement_price_per_kwh is not None
            else "None"
        ),
        no_export,
        min_export_price,
        battery_export_min_price,
        (
            f"{main_fuse_amps:.1f}A/{main_fuse_phases}ph"
            if main_fuse_amps is not None
            else "disabled"
        ),
    )

    try:
        import numpy as np
        from scipy.optimize import linprog
    except ImportError:
        log_planner("debug", "[milp] scipy/numpy not available — MILP disabled")
        return None

    if usable_kwh <= 0 or max_charge_per_slot <= 0:
        log_planner(
            "debug",
            "[milp] Skipping — usable_kwh=%.3f max_charge_per_slot=%.3f",
            usable_kwh,
            max_charge_per_slot,
        )
        return None

    n = len(slots)
    if n == 0:
        return None

    max_dis = (
        max_discharge_per_slot if max_discharge_per_slot is not None else usable_kwh
    )

    # ------------------------------------------------------------------
    # Identify future (active) vs. past (fixed-zero) slot indices
    # ------------------------------------------------------------------
    future_mask = [as_tz(s.end, now.tzinfo) > now for s in slots]
    # Indices of future slots in the full slot list
    future_idx = [i for i, m in enumerate(future_mask) if m]

    if not future_idx:
        return None

    # ------------------------------------------------------------------
    # Build per-slot data arrays (future slots only)
    # ------------------------------------------------------------------
    p_imp = np.array([slots[i].price.import_price for i in future_idx], dtype=float)
    p_exp = np.array([slots[i].price.export_price for i in future_idx], dtype=float)

    from custom_components.hsem.planner.milp._layout import (
        build_milp_column_layout,
        derive_milp_offsets,
    )
    from custom_components.hsem.planner.milp._price_sanitise import sanitize_prices
    from custom_components.hsem.planner.milp._reserve_diagnostics import (
        attach_export_reserve_diagnostics,
    )

    p_imp_obj, p_exp, battery_export_blocked = sanitize_prices(
        p_imp,
        p_exp,
        min_export_price=min_export_price,
        battery_export_min_price=battery_export_min_price,
    )

    # Net load = house consumption + EV extra load − PV estimate.
    # A positive value means the battery/grid must supply extra energy.
    # A negative value means there is PV surplus.
    # Split into base_load (positive demand) and pv_avail (PV surplus after load).
    # pv_avail[t] is added as an explicit LP variable to prevent infeasibility
    # when net_load is strongly negative and SoC limits constrain charge.
    #
    # EV adjustment: when EV charging is active, the EV consumes PV surplus
    # first (before the battery).  This reduces the PV surplus available to
    # the battery by the EV's total planned load (which includes both
    # ev_planned_load_kwh and ev_accounted_load_kwh).  base_load is NOT
    # increased because the battery never feeds the EV — any remaining EV
    # demand after PV goes to the grid.
    net_load = np.array(
        [
            slots[i].avg_house_consumption_kwh
            + slots[i].ev_planned_load_kwh
            - slots[i].solcast_pv_estimate_kwh
            for i in future_idx
        ],
        dtype=float,
    )
    pv_avail = np.maximum(-net_load, 0.0)  # PV surplus after house consumption
    base_load = np.maximum(net_load, 0.0)  # remaining demand after PV

    # ------------------------------------------------------------------
    # EV accounted load: when base_load_includes_ev=True, ev_accounted_load_kwh
    # is the EV load already captured in avg_house_consumption_kwh.  The battery
    # must not discharge to cover this load — it is the EV's own demand served
    # by the grid (or PV).  Without this cap, the live-injected current-slot
    # house consumption (which includes EV power when the CT clamp is upstream
    # of the charger) causes the MILP to discharge the house battery into the EV,
    # which provides zero financial benefit when EV charging is reimbursed
    # (issue #592).
    # ------------------------------------------------------------------
    ev_accounted = np.array(
        [slots[i].ev_accounted_load_kwh for i in future_idx], dtype=float
    )

    # ------------------------------------------------------------------
    # EV co-optimisation: when ev_configs is provided, the MILP decides EV
    # charging alongside the battery.  Recompute net_load/pv_avail/base_load
    # WITHOUT the pre-computed EV planned loads (the LP will decide allocation).
    # Otherwise keep the pre-existing EV adjustment (backward-compatible).
    # ------------------------------------------------------------------
    from custom_components.hsem.planner.milp._ev_amp_lattice import (
        ev_has_live_session,
    )

    active_evs: list[EVConfig] = []
    if ev_configs:
        for ev in ev_configs:
            # A managed live session (managed_session_cap_only, issue #797)
            # has max_charge_per_slot=0 by construction — it must still be
            # admitted so its Huawei discharge permission/ceiling is
            # honoured even though it can command no further charge.
            if (
                ev.enabled
                and ev.capacity_kwh > 1e-9
                and (ev.max_charge_per_slot > 1e-9 or ev_has_live_session(ev))
            ):
                active_evs.append(ev)
        if active_evs:
            # Recompute net_load without EV planned loads
            net_load = np.array(
                [
                    slots[i].avg_house_consumption_kwh
                    - slots[i].solcast_pv_estimate_kwh
                    for i in future_idx
                ],
                dtype=float,
            )
            pv_avail = np.maximum(-net_load, 0.0)
            base_load = np.maximum(net_load, 0.0)
            log_planner(
                "debug",
                "[milp] EV co-optimisation enabled: %d active EV(s), "
                "net_load rebuilt without pre-computed EV loads",
                len(active_evs),
            )
        else:
            active_evs = []
    if not active_evs and ev_configs:
        log_planner(
            "debug",
            "[milp] EV configs provided but no valid active EVs — "
            "falling back to fixed EV loads",
        )

    m = len(future_idx)  # number of active LP slots

    # ------------------------------------------------------------------
    # Session-aware EV demand, bounded by control authority (issue #615,
    # #789).  See _session_window.resolve_session_windows for the full
    # managed-vs-unmanaged certainty-window derivation.
    # ------------------------------------------------------------------
    from custom_components.hsem.planner.milp._session_window import (
        resolve_session_windows,
    )

    _session_windows = resolve_session_windows(
        slots=slots,
        future_idx=future_idx,
        now=now,
        active_evs=active_evs,
        m=m,
    )
    slot_hours = _session_windows.slot_hours
    available_slot_hours = _session_windows.available_slot_hours
    session_ev_indices = _session_windows.session_ev_indices
    session_dc_by_ev = _session_windows.session_dc_by_ev
    session_slots_set = _session_windows.session_slots_set
    session_slots_by_ev = _session_windows.session_slots_by_ev
    _has_session_demand = _session_windows.has_session_demand

    # ------------------------------------------------------------------
    # Variable layout:
    #   x = [ec(0..m-1), ed(0..m-1), gi(0..m-1), ge(0..m-1),
    #        pv(0..m-1), m(0..m-1),
    #        s_max_pen(0..m-1), s_min_pen(0..m-1),
    #        curt(0..m-1), bx(0..m-1), z_export(0..m-1)]
    #   + [evN_c(0..m-1) for each active EV]      ← EV DC charge per slot
    #   + [evN_target_pen for each active EV]      ← deadline target slack
    # ------------------------------------------------------------------
    # The declared layout is the single source of truth for the decision-vector
    # shape; every offset below is read from it rather than recomputed by hand,
    # so the constraint matrices and the bounds assembly cannot drift apart.
    fuse_active = main_fuse_amps is not None and main_fuse_amps > 1e-9

    # Solver-native whole-amp EV lattice (issue #797): resolved before the
    # column layout so it can declare each EV's ev_{i}_amps / ev_{i}_on
    # blocks up front — no incremental widening required.
    from custom_components.hsem.planner.milp._ev_amp_lattice import (
        resolve_ev_amp_plan,
    )

    ev_amp_plan = resolve_ev_amp_plan(
        active_evs, max_dis=max_dis, slot_hours=slot_hours
    )
    column_layout = build_milp_column_layout(
        m,
        len(active_evs),
        fuse_active=fuse_active,
        ev_amp_widths=ev_amp_plan.amp_widths(m),
        ev_on_widths=ev_amp_plan.on_widths(m),
    )
    _off = derive_milp_offsets(column_layout, len(active_evs))
    ev_amp_offsets = _off.ev_amp_offsets
    ev_on_offsets = _off.ev_on_offsets
    n_vars = _off.n_vars
    ec_off, ed_off, gi_off, ge_off = _off.ec_off, _off.ed_off, _off.gi_off, _off.ge_off
    pv_off, m_off = _off.pv_off, _off.m_off
    s_max_off, s_min_off, curt_off = _off.s_max_off, _off.s_min_off, _off.curt_off
    battery_export_off = _off.battery_export_off
    export_mode_off = _off.export_mode_off
    grid_flow_mode_off = _off.grid_flow_mode_off
    ev_var_offsets = _off.ev_var_offsets
    ev_pen_offsets = _off.ev_pen_offsets

    # --- Fuse constraint variables (aggregate + per-phase) ---
    # When main_fuse_amps is provided and > 0, gi_pen[t] penalty variables
    # absorb grid import exceeding the fuse rating.  Per-phase headroom and
    # the EV phase-topology flag are resolved alongside in _phase_fuse.py.
    from custom_components.hsem.planner.milp._fuse_setup import (
        resolve_fuse_variables,
    )

    (
        gi_pen_off,
        max_grid_import_per_slot_kwh,
        phase_fuse_active,
        max_phase_import_per_slot_kwh,
    ) = resolve_fuse_variables(
        fuse_active=fuse_active,
        main_fuse_amps=main_fuse_amps,
        main_fuse_phases=main_fuse_phases,
        column_layout=column_layout,
        active_evs=active_evs,
        slot_hours=slot_hours,
        first_slot=slots[future_idx[0]],
    )

    # Finite physical grid bounds close both signed-price unbounded directions.
    charge_eff = clamp_efficiency(charge_efficiency_pct)
    discharge_eff = clamp_efficiency(discharge_efficiency_pct)
    ev_import_capacity = sum(
        ev.max_charge_per_slot / max(ev.charger_efficiency, 0.01) for ev in active_evs
    )
    grid_import_ub_per_slot = (
        base_load + max_charge_per_slot / charge_eff + ev_import_capacity
    )
    grid_export_ub_per_slot = pv_avail + max_dis * discharge_eff

    # Grid export power cap (issue #726): hard per-slot bound on ge[t].
    from custom_components.hsem.planner.milp._export_cap import _resolve_export_cap

    export_limit_active, max_grid_export_per_slot_kwh = _resolve_export_cap(
        max_grid_export_power_kw, slots, future_idx
    )
    if export_limit_active:
        grid_export_ub_per_slot = np.minimum(
            grid_export_ub_per_slot,
            max_grid_export_per_slot_kwh,
        )

    # ------------------------------------------------------------------
    # Build objective vector and constraint matrices
    # ------------------------------------------------------------------
    p_imp_max = float(np.max(p_imp)) if m > 0 else 0.1
    p_soc = max(p_imp_max, 0.1) * 100.0

    from custom_components.hsem.planner.milp._constraints import _build_constraints
    from custom_components.hsem.planner.milp._objective import _build_objective

    c_obj = _build_objective(
        slots,
        future_idx,
        now,
        m,
        n_vars,
        ec_off,
        ed_off,
        gi_off,
        ge_off,
        battery_export_off,
        m_off,
        s_max_off,
        s_min_off,
        gi_pen_off,
        ev_var_offsets,
        ev_pen_offsets,
        active_evs,
        p_imp,
        p_imp_obj,
        p_exp,
        p_soc,
        cycle_cost_per_kwh,
        charge_eff,
        time_discount_rate,
        replacement_price_per_kwh,
        fuse_active,
        usable_kwh=usable_kwh,
        max_charge_per_slot=max_charge_per_slot,
        current_kwh=current_kwh,
        pv_avail=pv_avail,
        base_load=base_load,
    )

    constraints = _build_constraints(
        m,
        n_vars,
        ec_off,
        ed_off,
        gi_off,
        ge_off,
        pv_off,
        m_off,
        curt_off,
        gi_pen_off,
        s_max_off,
        s_min_off,
        ev_var_offsets,
        ev_pen_offsets,
        active_evs,
        pv_avail,
        base_load,
        ev_accounted,
        charge_eff,
        discharge_eff,
        current_kwh,
        usable_kwh,
        max_charge_per_slot,
        max_dis,
        max_grid_import_per_slot_kwh,
        fuse_active,
        no_export,
        session_slots_set,
        session_ev_indices,
        slot_hours,
        _has_session_demand,
        session_dc_by_ev=session_dc_by_ev,
        available_slot_hours=available_slot_hours,
        column_layout=column_layout,
        max_grid_export_per_slot_kwh=max_grid_export_per_slot_kwh,
        export_limit_active=export_limit_active,
        battery_export_blocked=battery_export_blocked,
        battery_export_off=battery_export_off,
        export_mode_off=export_mode_off,
        excess_export_discharge_buffer_pct=excess_export_discharge_buffer_pct,
        grid_flow_mode_off=grid_flow_mode_off,
        grid_import_ub_per_slot=grid_import_ub_per_slot,
        grid_export_ub_per_slot=grid_export_ub_per_slot,
        phase_fuse_active=phase_fuse_active,
        max_phase_import_per_slot_kwh=max_phase_import_per_slot_kwh,
        ev_amp_plan=ev_amp_plan,
    )

    # Solver-native whole-amp EV lattice (issue #797): link ev_c[t] to the
    # executable amp variable and, for any EV whose Huawei discharge
    # permission is restrictive, cap primary discharge while it charges.
    # Appended last, after every other dense-matrix extension, so it can
    # reference the widest (final) n_vars directly.
    from custom_components.hsem.planner.milp._ev_amp_lattice import (
        add_ev_amp_lattice_constraints,
    )

    constraints = add_ev_amp_lattice_constraints(
        constraints,
        ev_amp_plan,
        active_evs,
        n_vars=n_vars,
        m=m,
        ev_var_offsets=ev_var_offsets,
        ev_amp_offsets=ev_amp_offsets,
        ev_on_offsets=ev_on_offsets,
        ed_off=ed_off,
        max_dis=max_dis,
        available_slot_hours=available_slot_hours,
        session_dc_by_ev=session_dc_by_ev,
    )

    A_eq = constraints["A_eq"]
    b_eq = constraints["b_eq"]
    A_ub = constraints["A_ub"]
    b_ub = constraints["b_ub"]
    bounds = constraints["bounds"]

    # Fail fast if the positional bounds list is not exactly one entry per
    # LP variable.  A wrong-width or missing block would otherwise silently
    # misalign the bounds array handed to linprog and produce either an
    # opaque solver failure or, worse, bounds applied to the wrong variables.
    if len(bounds) != n_vars:
        log_planner(
            "error",
            "[milp] bounds layout mismatch: %d bounds for %d variables",
            len(bounds),
            n_vars,
        )
        return None

    # ------------------------------------------------------------------
    # Solve using HiGHS
    # ------------------------------------------------------------------
    from custom_components.hsem.planner.milp._ev_amp_lattice import (
        ev_amp_integrality,
    )
    from custom_components.hsem.planner.milp._incumbent import solve_and_validate

    integrality = np.zeros(n_vars, dtype=int)
    integrality[export_mode_off : export_mode_off + m] = 1
    integrality[grid_flow_mode_off : grid_flow_mode_off + m] = 1
    integrality |= ev_amp_integrality(
        ev_amp_plan,
        n_vars=n_vars,
        ev_amp_offsets=ev_amp_offsets,
        ev_on_offsets=ev_on_offsets,
        m=m,
    )
    result = solve_and_validate(
        linprog,
        c_obj=c_obj,
        a_ub=A_ub,
        b_ub=b_ub,
        a_eq=A_eq,
        b_eq=b_eq,
        bounds=bounds,
        integrality=integrality,
        solver_time_limit_s=_SOLVER_TIME_LIMIT_S,
        n_vars=n_vars,
        slot_count=n,
        future_idx=future_idx,
        m=m,
        variable_blocks={
            block.name: (block.offset, block.width) for block in column_layout.blocks
        },
    )
    if result is None:
        return None

    # Terminal-SoC credit at end-of-horizon (diagnostic only — the LP
    # objective already includes this term as a linear cost).
    ec_sol = result.x[ec_off : ec_off + m]
    ed_sol = result.x[ed_off : ed_off + m]

    # Import helpers here to avoid circular imports with the milp package __init__
    from custom_components.hsem.planner.milp._diagnostics import (
        _compute_milp_diagnostics,
        _compute_terminal_soc_credit,
    )
    from custom_components.hsem.planner.milp._write_results import (
        _write_milp_results_to_slots,
    )

    terminal_soc_credit = _compute_terminal_soc_credit(
        current_kwh=current_kwh,
        usable_kwh=usable_kwh,
        ec_sol=ec_sol,
        ed_sol=ed_sol,
        replacement_price_per_kwh=replacement_price_per_kwh,
    )

    # Pre-compute curtailment solution (needed by both write-out and diagnostics)
    curt_sol_full = result.x[curt_off : curt_off + m]

    # Write MILP decision variables into output slots.
    ev_writeback_diagnostics: dict[str, dict[str, object]] = {}
    out_slots = _write_milp_results_to_slots(
        slots,
        future_idx,
        now,
        ec_sol,
        ed_sol,
        result.x,
        m,
        battery_export_off,
        active_evs,
        ev_var_offsets,
        pv_avail,
        base_load,
        charge_eff,
        discharge_eff,
        p_exp,
        min_export_price,
        _has_session_demand,
        session_slots_set,
        current_kwh,
        usable_kwh,
        curt_sol_full,
        ev_writeback_diagnostics=ev_writeback_diagnostics,
        _min_action_kwh=_MIN_ACTION_KWH,
    )

    from custom_components.hsem.planner.milp._postwrite_validation import (
        validate_primary_inventory,
    )

    inventory_validation = validate_primary_inventory(
        out_slots,
        future_idx,
        current_kwh=current_kwh,
        usable_kwh=usable_kwh,
    )
    if not bool(inventory_validation["valid"]):
        log_planner(
            "warning",
            "[milp] Rejecting executable primary inventory: %s",
            inventory_validation,
        )
        return None

    # Post-solve per-phase envelope validation (EV charger phase topology).
    # Uses the same shared topology shares as the constraint rows, so a plan
    # the solver accepted is never erased by a validator that assumed a
    # different topology.
    from custom_components.hsem.planner.milp._phase_fuse import (
        validate_published_phase_envelope,
    )

    phase_validation = validate_published_phase_envelope(
        out_slots=out_slots,
        future_idx=future_idx,
        active_evs=active_evs,
        session_slots_by_ev=session_slots_by_ev,
        slot_hours=slot_hours,
        phase_fuse_active=phase_fuse_active,
        max_phase_import_per_slot_kwh=max_phase_import_per_slot_kwh,
    )

    # Compute diagnostics
    diagnostics = _compute_milp_diagnostics(
        result,
        out_slots,
        slots,
        future_idx,
        m,
        s_max_off,
        s_min_off,
        curt_off,
        gi_off,
        gi_pen_off,
        replacement_price_per_kwh,
        fuse_active,
        max_grid_import_per_slot_kwh,
        active_evs,
        ev_var_offsets,
        ev_pen_offsets,
        terminal_soc_credit,
        _min_action_kwh=_MIN_ACTION_KWH,
    )
    diagnostics["primary_postwrite_inventory_validation"] = inventory_validation
    if phase_fuse_active:
        diagnostics.update(
            phase_fuse_validation=phase_validation,
            max_phase_import_kwh=phase_validation["max_phase_import_kwh"],
        )
    if ev_writeback_diagnostics:
        diagnostics["ev"] = ev_writeback_diagnostics
    attach_export_reserve_diagnostics(
        diagnostics,
        constraints,
        m=m,
        export_mode_off=export_mode_off,
        solution=result.x,
        ec_sol=ec_sol,
        ed_sol=ed_sol,
        current_kwh=current_kwh,
    )

    return out_slots, diagnostics

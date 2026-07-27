"""MILP-based optimal battery charge/discharge scheduler.

Formulated as a continuous LP via ``scipy.optimize.linprog`` with HiGHS.
Binary flags relaxed to continuous; mutex constraint prevents
simultaneous charge+discharge.

Decision variables per slot t (9+n*1 for EVs + fuse penalties):
ec, ed, gi, ge, pv, m (=max(ec,ed)), s_max_pen, s_min_pen, curt.

Objective: Σ p_imp·gi - p_exp·ge + cycle_cost·m + p_soc·penalties.

Constraints: SoC recurrence, SoC soft bounds, charge/discharge limits,
mutex, energy balance (with efficiencies), EV co-optimisation, fuse limit.

Price sanitisation: export<min_export→0, export≤import, import_obj≥0.
Curtailment variable ``curt[t]`` allows explicit PV shedding.

Pure Python, no HA imports — testable with plain pytest.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from custom_components.hsem.models.ev_config import EVConfig
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
    main_fuse_amps: float | None = None,
    main_fuse_phases: int = 3,
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

    The MILP objective now includes conversion loss costs so its optimisation
    matches the cost function's ``total_cost``.  The energy balance equation
    accounts for charge/discharge efficiencies so ``gi[t]`` reflects real grid
    import (not the idealised lossless value).

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
            threshold below which export is not worthwhile.  Set by the
            caller to ``max(export_min_price, recommended_threshold)``
            where ``export_min_price`` is the inverter's physical block
            threshold and ``recommended_threshold`` is the
            depreciation-based discharge minimum.  Used for:
            - Clamping export prices to 0 before the LP solves (export
              below this price is physically blocked).
            - Deciding between ``ForceBatteriesDischarge`` and
              ``BatteriesDischargeMode`` in post-processing.
            Defaults to 0.0.
        ev_configs:
            Optional list of :class:`EVConfig` objects (one per EV).  When
            provided, the MILP co-optimises EV charging alongside the battery.
            EV loads are treated as decision variables with deadline-target
            soft constraints.  The ``ev_planned_load_kwh`` field on the input
            slots is ignored for EV-enabled slots (the MILP decides allocation).
            ``None`` (default) uses pre-computed ``ev_planned_load_kwh`` as
            fixed inputs (backward-compatible behaviour).
        main_fuse_amps:
            Main fuse/breaker rating in amps.  When provided and > 0, a soft
            constraint limits total grid import power per slot to
            ``main_fuse_amps * 230 * main_fuse_phases / 1000 * (interval_minutes / 60)`` kWh.
            A penalty variable ``gi_pen[t]`` absorbs any excess, preventing
            infeasibility when house base load alone exceeds the fuse rating.
            ``None`` or 0 disables the constraint (identical to current behaviour).
        main_fuse_phases:
            Electrical phase count (1 or 3).  Used as the multiplier in the
            max-grid-import formula above.  Defaults to 3 (three-phase).
            Single-phase installations MUST use 1.

    Returns:
        A tuple ``(slots, diagnostics)`` where:
        - ``slots`` is a list of :class:`PlannedSlot` copies with MILP-derived
          recommendations.
        - ``diagnostics`` is a dict with keys ``"s_max_pen"``, ``"s_min_pen"``,
          ``"has_violations"``, ``"total_violation_kwh"``,
          ``"discharge_loss_cost_destination_aware"``.
        Returns ``None`` if the solver fails (unrelated to constraint
        violations — e.g., solver crash or numerical issue).
    """
    log_planner(
        "debug",
        "[milp] solve_milp  slots=%d  current=%.3f  usable=%.3f  "
        "max_chg=%.3f  max_dis=%s  cycle_cost=%.6f  "
        "chg_eff=%.2f  dis_eff=%.2f  discount=%.4f  repl_price=%s",
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

    # Replace NaN prices with 0 to prevent solver numerical issues
    p_imp = np.nan_to_num(p_imp, nan=0.0)
    p_exp = np.nan_to_num(p_exp, nan=0.0)

    # Clamp export prices below min_export_price to 0.
    # The applier physically sets the inverter to GRID_EXPORT_LIMIT_WATT
    # for these slots, blocking export entirely.  The LP must not optimise
    # around a price signal that will never be realised.
    #
    # Negative export prices are NOT clamped — the LP has a curt[t]
    # variable with zero objective cost that naturally handles them:
    # when p_exp < 0, export costs money (p_exp is negative, so
    # -p_exp·ge becomes a positive cost), and the LP prefers curtailment
    # (cost 0) over export (cost > 0).
    if min_export_price > 1e-9:
        blocked = p_exp < min_export_price
        n_blocked = int(np.sum(blocked))
        if n_blocked > 0:
            log_planner(
                "debug",
                "[milp] Clamping %d export prices below min_price (%.4f) to 0 "
                "(max clamped=%.4f)",
                n_blocked,
                min_export_price,
                float(np.max(p_exp[blocked])),
            )
        p_exp = np.where(blocked, 0.0, p_exp)

    # Clamp export price to never exceed import price for the same slot.
    # Without this, slots where p_exp[t] > p_imp[t] create an unbounded LP
    # (HiGHS status=3): both gi[t] and ge[t] are [0, ∞) and linked only
    # through the energy-balance equality, so the LP can drive both to
    # infinity (import cheap, export expensive) while the terms cancel in
    # the balance equation.  This is economically correct — no rational
    # agent imports and exports the same commodity in the same instant for
    # profit — and capping the achievable arbitrage spread removes the
    # unbounded direction without changing any other behavior.
    export_exceeds_import = p_exp > p_imp
    n_clamped = int(np.sum(export_exceeds_import))
    if n_clamped > 0:
        deltas = p_exp[export_exceeds_import] - p_imp[export_exceeds_import]
        log_planner(
            "debug",
            "[milp] Clamping %d export prices that exceed import price "
            "(max delta=%.4f)",
            n_clamped,
            float(np.max(deltas)),
        )
        p_exp = np.minimum(p_exp, p_imp)

    # Clamp negative import prices to 0 for objective coefficients.
    # When p_imp[t] < 0, the gi[t] objective coefficient becomes
    # negative, incentivising the LP to import infinite energy
    # (HiGHS status=3, unbounded LP).  curt[t] has zero objective
    # cost but participates in the energy balance, so the LP can
    # import-and-curtail for unbounded profit even without p_exp>p_imp.
    #
    # Clamping to 0 here removes that unbounded direction while
    # keeping the original p_imp for the export-≤-import clamp and
    # penalty scaling (both need the real market signal).
    #
    # This is the companion to the export-≤-import clamp above:
    # together they close both unbounded-LP directions identified in
    # issue #635.
    p_imp_obj = np.maximum(p_imp, 0.0)

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
    active_evs: list[EVConfig] = []
    if ev_configs:
        for ev in ev_configs:
            if ev.enabled and ev.capacity_kwh > 1e-9 and ev.max_charge_per_slot > 1e-9:
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
    # Session-aware EV demand (issue #615).
    # When an EV is actively charging (session_charge_kw is set), treat
    # the first 2 hours as certain demand at that power level.
    # Grid-charging the battery is blocked during these slots to avoid
    # stacking battery charge on top of the EV draw.
    # ------------------------------------------------------------------
    slot_hours = (
        (slots[future_idx[0]].end - slots[future_idx[0]].start).total_seconds() / 3600.0
        if future_idx
        else 0.0
    )
    SESSION_HOURS = 2.0
    if slot_hours > 1e-9:
        SESSION_SLOTS = min(round(SESSION_HOURS / slot_hours), m)
    else:
        SESSION_SLOTS = min(8, m)  # fallback guard, should not normally trigger
    session_ev_indices: list[int] = []  # indices into active_evs
    session_slots_set: set[int] = set()
    if active_evs and slot_hours > 0:
        for ev_idx, ev in enumerate(active_evs):
            if ev.session_charge_kw is not None and ev.session_charge_kw > 1e-9:
                session_ev_indices.append(ev_idx)
        if session_ev_indices:
            session_slots_set = set(range(SESSION_SLOTS))
    _has_session_demand = bool(session_ev_indices)

    # ------------------------------------------------------------------
    # Variable layout:
    #   x = [ec(0..m-1), ed(0..m-1), gi(0..m-1), ge(0..m-1),
    #        pv(0..m-1), m(0..m-1),
    #        s_max_pen(0..m-1), s_min_pen(0..m-1),
    #        curt(0..m-1)]
    #   + [evN_c(0..m-1) for each active EV]      ← EV DC charge per slot
    #   + [evN_target_pen for each active EV]      ← deadline target slack
    # ------------------------------------------------------------------
    ec_off, ed_off, gi_off, ge_off, pv_off, m_off = 0, m, 2 * m, 3 * m, 4 * m, 5 * m
    s_max_off = 6 * m
    s_min_off = 7 * m
    curt_off = 8 * m
    n_vars = 9 * m

    # --- EV variable layout ---
    ev_var_offsets: list[int] = []  # start of ev_c[t] block per EV
    ev_pen_offsets: list[int] = []  # index of deadline penalty per EV
    for _ev_idx, _ev in enumerate(active_evs):
        ev_var_offsets.append(n_vars)
        n_vars += m  # ev_c[0..m-1] per EV
        ev_pen_offsets.append(n_vars)
        n_vars += 1  # single penalty per EV

    # --- Fuse constraint variables ---
    # When main_fuse_amps is provided and > 0, add gi_pen[t] penalty
    # variables that absorb grid import exceeding the fuse rating.
    fuse_active = main_fuse_amps is not None and main_fuse_amps > 1e-9
    if fuse_active:
        gi_pen_off = n_vars
        n_vars += m  # gi_pen[0..m-1] per slot
        # Calculate max grid import per slot in kWh
        # Formula: amps * 230V * phases / 1000 (kW) * (interval_minutes / 60) (hours)
        # We derive interval_minutes from the first slot's duration
        first_slot = slots[future_idx[0]]
        interval_minutes = (first_slot.end - first_slot.start).total_seconds() / 60.0
        assert main_fuse_amps is not None  # guarded by fuse_active
        max_grid_import_per_slot_kwh = (
            main_fuse_amps
            * 230.0
            * float(main_fuse_phases)
            / 1000.0
            * (interval_minutes / 60.0)
        )
        log_planner(
            "debug",
            "[milp] Main fuse constraint active: %d A × %d-phase → max %.3f kWh/slot "
            "(interval=%.0f min)",
            main_fuse_amps,
            main_fuse_phases,
            max_grid_import_per_slot_kwh,
            interval_minutes,
        )
    else:
        gi_pen_off = 0  # unused when fuse is inactive
        max_grid_import_per_slot_kwh = 0.0

    # Resolve charge/discharge efficiencies for the energy balance equation.
    # The MILP must account for real-world conversion losses so its solution
    # matches the cost function's total_cost (which includes conversion loss
    # via the conversion_loss_cost term).
    charge_eff = clamp_efficiency(charge_efficiency_pct)
    discharge_eff = clamp_efficiency(discharge_efficiency_pct)
    charge_loss = 1.0 - charge_eff
    discharge_loss = 1.0 - discharge_eff

    # ------------------------------------------------------------------
    # Build objective vector and constraint matrices
    # ------------------------------------------------------------------
    p_imp_max = float(np.max(p_imp)) if m > 0 else 0.1
    p_soc = max(p_imp_max, 0.1) * 100.0

    from custom_components.hsem.planner.milp._constraints import _build_constraints
    from custom_components.hsem.planner.milp._objective import _build_objective

    c_obj = _build_objective(
        slots, future_idx, now, m, n_vars, ec_off, ed_off, gi_off, ge_off,
        m_off, s_max_off, s_min_off, gi_pen_off, ev_var_offsets, ev_pen_offsets,
        active_evs, p_imp, p_imp_obj, p_exp, p_soc, cycle_cost_per_kwh,
        charge_loss, discharge_loss, time_discount_rate,
        replacement_price_per_kwh, fuse_active,
    )

    constraints = _build_constraints(
        m, n_vars, ec_off, ed_off, gi_off, ge_off, pv_off, m_off, curt_off,
        gi_pen_off, s_max_off, s_min_off, ev_var_offsets, ev_pen_offsets,
        active_evs, pv_avail, base_load, ev_accounted, charge_eff, discharge_eff,
        current_kwh, usable_kwh, max_charge_per_slot, max_dis,
        max_grid_import_per_slot_kwh, fuse_active, session_slots_set,
        session_ev_indices, SESSION_SLOTS, slot_hours, _has_session_demand,
    )

    A_eq = constraints["A_eq"]
    b_eq = constraints["b_eq"]
    A_ub = constraints["A_ub"]
    b_ub = constraints["b_ub"]
    bounds = constraints["bounds"]

    # ------------------------------------------------------------------
    # Solve using HiGHS
    # ------------------------------------------------------------------
    try:
        result = linprog(
            c_obj,
            A_ub=A_ub,
            b_ub=b_ub,
            A_eq=A_eq,
            b_eq=b_eq,
            bounds=bounds,
            method="highs",
            options={"time_limit": _SOLVER_TIME_LIMIT_S, "disp": False},
        )
    except Exception as exc:
        log_planner("warning", "[milp] Solver raised an exception: %s", exc)
        return None

    if not result.success:
        log_planner(
            "debug",
            "[milp] Solver returned status=%s (%s)",
            result.status,
            result.message,
        )
        return None

    # ------------------------------------------------------------------
    # Compute terminal-SoC credit at end-of-horizon (diagnostic).
    # This matches cost_function.py's terminal_soc_value calculation:
    # terminal_soc_value = (initial_kwh - final_kwh) * replacement_price
    #
    # The LP objective now INCLUDES this term (see c_obj construction
    # above), so the solution itself already reflects this valuation.
    # This post-hoc calculation is retained as a diagnostic consistency
    # check and for the diagnostics dict.
    # ------------------------------------------------------------------
    ec_sol = result.x[ec_off : ec_off + m]
    ed_sol = result.x[ed_off : ed_off + m]

    # Compute final SoC from the LP solution
    final_soc_kwh = current_kwh + float(np.sum(ec_sol)) - float(np.sum(ed_sol))
    final_soc_kwh = max(0.0, min(final_soc_kwh, usable_kwh))  # clamp to bounds

    # Terminal-SoC credit: positive when plan ends with less energy (penalty),
    # negative when plan ends with more energy (credit).
    terminal_soc_credit = 0.0
    if replacement_price_per_kwh is not None and abs(replacement_price_per_kwh) > 1e-9:
        terminal_soc_credit = (current_kwh - final_soc_kwh) * replacement_price_per_kwh
        log_planner(
            "debug",
            "[milp] Terminal-SoC credit: initial=%.3f  final=%.3f  repl_price=%.4f  credit=%.4f",
            current_kwh,
            final_soc_kwh,
            replacement_price_per_kwh,
            terminal_soc_credit,
        )

    # Pre-compute curtailment solution (needed by both write-out and diagnostics)
    curt_sol_full = result.x[curt_off : curt_off + m]

    # Import helpers here to avoid circular imports with the milp package __init__
    from custom_components.hsem.planner.milp._diagnostics import (
        _compute_milp_diagnostics,
    )
    from custom_components.hsem.planner.milp._write_results import (
        _write_milp_results_to_slots,
    )

    # Write MILP decision variables into output slots
    out_slots = _write_milp_results_to_slots(
        slots,
        future_idx,
        now,
        ec_sol,
        ed_sol,
        result.x,
        m,
        ge_off,
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
        _min_action_kwh=_MIN_ACTION_KWH,
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
        min_export_price,
        p_imp_obj,
        discharge_loss,
        fuse_active,
        max_grid_import_per_slot_kwh,
        active_evs,
        ev_var_offsets,
        ev_pen_offsets,
        terminal_soc_credit,
        _min_action_kwh=_MIN_ACTION_KWH,
    )

    return out_slots, diagnostics


def is_scipy_available() -> bool:
    """Return ``True`` if scipy is importable in the current environment.

    The import result is cached at module level so that the blocking
    ``import scipy.optimize`` happens exactly once at import time rather
    than on every planner run inside the Home Assistant event loop.
    """
    return _SCIPY_AVAILABLE


# --- Module-level cache: computed once at import time --------------------
def _check_scipy() -> bool:
    """Check whether scipy is importable.  Called once at module load."""
    try:
        import scipy.optimize  # noqa: F401

        return True
    except ImportError:
        return False


_SCIPY_AVAILABLE: bool = _check_scipy()

"""MILP-based optimal battery charge/discharge scheduler.

Finds the globally cost-optimal charge/discharge schedule via LP.
It is the PRIMARY planner — heuristic candidates are disabled.

Algorithm overview
------------------
Formulated as a **continuous LP** (not MILP) using the HiGHS solver
via ``scipy.optimize.linprog``.  Binary charge/discharge flags are
relaxed to continuous because the mutual-exclusion constraint already
prevents simultaneous charge + discharge.

Decision variables (flattened into ``x`` of length 8*n)
----------------------------------------------------------
For each slot ``t ∈ 0…n-1``:

- ``ec[t]`` — energy charged and *stored* in battery this slot (kWh)
- ``ed[t]`` — energy discharged *from* battery this slot (kWh)
- ``gi[t]`` — grid import this slot (kWh)
- ``ge[t]`` — grid export this slot (kWh)
- ``pv[t]`` — PV surplus after house consumption (kWh, fixed)
- ``m[t]`` — max(ec[t], ed[t]) for cycle cost (kWh)
- ``s_max_pen[t]`` — kWh SoC exceeds usable_kwh
- ``s_min_pen[t]`` — kWh SoC drops below 0

``soc[t]`` is derived from ``ec``/``ed`` via forward recurrence.

Objective (minimise)
--------------------
``Σ_t [ p_imp[t]*gi[t] - p_exp[t]*ge[t] + α*m[t]
       + γ*(ed[t]-ec[t]) + p_soc*s_max_pen[t] + p_soc*s_min_pen[t] ]``

where ``α`` = battery cycle cost/kWh, ``γ`` = terminal-SoC replacement
price, ``p_soc = max(p_imp) * 100`` ensures penalties are only used when
forced (e.g., initial SoC outside bounds).

Constraints
-----------
1. SoC recurrence: ``soc[t] = soc[t-1] + ec[t] - ed[t]``
2. SoC bounds (soft): ``soc[t] − s_max_pen ≤ usable_kwh``,
   ``−soc[t] − s_min_pen ≤ 0``
3. Charge limit: ``ec[t] ≤ max_charge_per_slot``
4. Discharge limit: ``ed[t] ≤ max_discharge_per_slot``
5. Mutual exclusion: ``ec[t]/mc + ed[t]/md ≤ 1``
6. Energy balance:
   ``gi + pv + ed·η_disch = load + ec/η_chg + ge``
7. Non-negativity: all ≥ 0. Past slots fixed at zero.

Solving
-------
``scipy.optimize.linprog(method='highs')``.  96-slot horizon < 50 ms.

Fallback
--------
Returns ``None`` if solver crashes/times out; engine falls back to
rule-based baseline.

Design constraints
------------------
- Pure Python, no Home Assistant imports — testable with plain pytest.
- Only mutates ``recommendation``/``batteries_charged`` on deep-copied
  slots; never touches the caller's baseline.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import clamp_efficiency
from custom_components.hsem.utils.recommendations import Recommendations

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
) -> tuple[list[PlannedSlot], dict] | None:
    """Solve the LP and return a deep-copy slot list with MILP recommendations.

    The returned list is independent of *slots* — it is safe to mutate without
    affecting the caller's data.  Fields written by the MILP are:

    - ``recommendation``  — one of ``BatteriesChargeGrid``, ``BatteriesDischargeMode``,
      ``ForceBatteriesDischarge``, or ``None`` (idle).
    - ``batteries_charged`` — energy entering the battery this slot (kWh).

    The SoC simulation (:func:`~soc_simulation.simulate_soc`) must be run
    by the caller **after** receiving these slots to populate
    ``grid_import_kwh``, ``grid_export_kwh``, and ``estimated_battery_soc``.

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

    Returns:
        A tuple ``(slots, diagnostics)`` where:
        - ``slots`` is a list of :class:`PlannedSlot` copies with MILP-derived
          recommendations.
        - ``diagnostics`` is a dict with keys ``"s_max_pen"``, ``"s_min_pen"``,
          ``"has_violations"``, ``"total_violation_kwh"``.
        Returns ``None`` if the solver fails (unrelated to constraint
        violations — e.g., solver crash or numerical issue).
    """
    import copy

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

    # Clamp export prices to reflect physical inverter behaviour:
    # 1. Negative prices → 0 (the inverter curtails PV rather than pay to export).
    # 2. Prices below min_export_price → 0 (the applier sets the inverter to
    #    GRID_EXPORT_LIMIT_WATT, blocking export entirely).
    #
    # The MILP cannot model curtailment (pv[t] is fixed), so flooring to 0 is
    # the best approximation: the LP sees no revenue for blocked-export slots
    # and does not optimise around a price signal that will never be realised.
    neg_mask = p_exp < 0.0
    n_neg = int(np.sum(neg_mask))
    if n_neg > 0:
        log_planner(
            "debug",
            "[milp] Clamping %d negative export prices to 0 (min=%.4f)",
            n_neg,
            float(np.min(p_exp)),
        )
    p_exp = np.maximum(p_exp, 0.0)

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
    # Variable layout:
    #   x = [ec(0..m-1), ed(0..m-1), gi(0..m-1), ge(0..m-1),
    #        pv(0..m-1), m(0..m-1),
    #        s_max_pen(0..m-1), s_min_pen(0..m-1)]
    #   + [evN_c(0..m-1) for each active EV]      ← EV DC charge per slot
    #   + [evN_target_pen for each active EV]      ← deadline target slack
    # ------------------------------------------------------------------
    ec_off, ed_off, gi_off, ge_off, pv_off, m_off = 0, m, 2 * m, 3 * m, 4 * m, 5 * m
    s_max_off = 6 * m
    s_min_off = 7 * m
    n_vars = 8 * m

    # --- EV variable layout ---
    ev_var_offsets: list[int] = []  # start of ev_c[t] block per EV
    ev_pen_offsets: list[int] = []  # index of deadline penalty per EV
    for _ev_idx, _ev in enumerate(active_evs):
        ev_var_offsets.append(n_vars)
        n_vars += m  # ev_c[0..m-1] per EV
        ev_pen_offsets.append(n_vars)
        n_vars += 1  # single penalty per EV
    num_evs = len(active_evs)

    # Resolve charge/discharge efficiencies for the energy balance equation.
    # The MILP must account for real-world conversion losses so its solution
    # matches the cost function's total_cost (which includes conversion loss
    # via the conversion_loss_cost term).
    charge_eff = clamp_efficiency(charge_efficiency_pct)
    discharge_eff = clamp_efficiency(discharge_efficiency_pct)
    charge_loss = 1.0 - charge_eff
    discharge_loss = 1.0 - discharge_eff

    # ------------------------------------------------------------------
    # Objective vector: minimise grid_import_cost - export_revenue + cycle_cost
    # + conversion_loss_cost - terminal_soc_credit.
    # pv[t] has zero objective cost (it's free).
    #
    # Cycle cost is counted once per slot (matching cost_function.py's
    # max(charge, discharge) counting).  cycle_cost_per_kwh already includes
    # the 2× throughput factor in its denominator, so one full round-trip
    # (charge + discharge) correctly costs 2 × usable_kwh × cycle_cost_per_kwh
    # per direction.
    #
    # Terminal-SoC credit: energy left in the battery at end of horizon
    # avoids importing at the next discharge window price.  This mirrors
    # terminal_soc_value in cost_function.py.
    #
    # Apply time discount so the MILP objective matches the selector's
    # discounted score (distant savings are worth less).
    #
    # Penalty cost p_soc ensures penalties are never used unless forced.
    # It must be much larger than the maximum possible import cost per kWh.
    # ------------------------------------------------------------------
    p_imp_max = float(np.max(p_imp)) if m > 0 else 0.1
    p_soc = max(p_imp_max, 0.1) * 100.0

    use_discount = time_discount_rate < 1.0 - 1e-9
    c_obj = np.zeros(n_vars)

    for t in range(m):
        discount = 1.0
        if use_discount:
            # Compute hours from now for this slot's midpoint
            slot = slots[future_idx[t]]
            slot_mid = slot.start + (slot.end - slot.start) / 2
            hours_ahead = max((slot_mid - now).total_seconds() / 3600.0, 0.0)
            discount = time_discount_rate**hours_ahead

        # Charge-side: energy lost during charge, priced at import price
        c_obj[ec_off + t] = (charge_loss * p_imp[t]) * discount
        # Discharge-side: energy lost during discharge, priced at import price
        c_obj[ed_off + t] = (discharge_loss * p_imp[t]) * discount
        # Cycle cost through auxiliary variable m[t] (= max(ec, ed))
        c_obj[m_off + t] = cycle_cost_per_kwh * discount
        c_obj[gi_off + t] = p_imp[t] * discount  # grid import cost
        c_obj[ge_off + t] = -p_exp[t] * discount  # export revenue (negative = gain)
        # pv[t] has zero objective cost

        # Terminal-SoC credit: storing energy (ec) reduces future import cost;
        # discharging (ed) increases future import cost.  The terminal value
        # is undiscounted to match the selector's cost_function.score_plan()
        # (which keeps terminal_soc_value raw regardless of time_discount_rate).
        if (
            replacement_price_per_kwh is not None
            and abs(replacement_price_per_kwh) > 1e-9
        ):
            c_obj[ec_off + t] -= replacement_price_per_kwh
            c_obj[ed_off + t] += replacement_price_per_kwh

        # Penalty costs: high enough that penalties are zero when SoC is
        # within bounds, but absorb violations when the initial SoC is
        # outside [0, usable_kwh] (e.g., overcharged battery).
        c_obj[s_max_off + t] = p_soc * discount
        c_obj[s_min_off + t] = p_soc * discount

    # --- EV deadline penalty (undiscounted — deadline is a hard commitment) ---
    # Must be high enough that the MILP always prefers meeting the target
    # when it is physically possible within the available slots.
    for ev_idx, ev in enumerate(active_evs):
        if ev.deadline_slot is not None and ev.target_kwh > ev.initial_soc_kwh + 1e-9:
            # Penalty per kWh shortfall: >= max_import_cost_for_full_charge
            # This ensures the MILP will import at the most expensive price
            # rather than miss the deadline target.
            ev_penalty_cost = max(p_imp_max, 0.1) * max(ev.capacity_kwh, 1.0) * 100.0
            c_obj[ev_pen_offsets[ev_idx]] = ev_penalty_cost

    # ------------------------------------------------------------------
    # Equality constraints: energy balance per slot
    # gi[t] + pv[t] + ed[t]*discharge_eff
    #     = base_load[t] + ec[t]/charge_eff + ge[t] + Σ ev_c/eff
    # ->  gi - ec/η_chg + ed·η_dis + pv - ge - Σ ev_c/eff = base_load
    #
    # EV charge energy ev_c[t] is DC-side (delivered to EV battery).
    # The AC grid/PV draw is ev_c[t] / charger_efficiency — that is the
    # load the house must supply.  base_load already EXCLUDES EV load
    # when ev_configs is active (net_load was rebuilt without EV).
    # ------------------------------------------------------------------
    A_eq = np.zeros((m, n_vars))  # NOSONAR
    for t in range(m):
        A_eq[t, ec_off + t] = -1.0 / charge_eff  # -ec[t]/charge_eff
        A_eq[t, ed_off + t] = 1.0 * discharge_eff  # +ed[t]*discharge_eff
        A_eq[t, gi_off + t] = 1.0  # +gi[t]
        A_eq[t, ge_off + t] = -1.0  # -ge[t]
        A_eq[t, pv_off + t] = 1.0  # +pv[t] (fixed to pv_avail[t])
        # EV AC load: -ev_c[t] / charger_eff per active EV
        for ev_idx, ev in enumerate(active_evs):
            A_eq[t, ev_var_offsets[ev_idx] + t] = -1.0 / ev.charger_efficiency
    b_eq = base_load.copy()  # always non-negative — pv[t] covers surplus

    # ------------------------------------------------------------------
    # Inequality constraints:
    #   1. SoC recurrence: soc[t] = soc[0] + Σ_{k≤t} (ec[k] − ed[k])
    #      Upper (soft): Σ_{k≤t}(ec[k]−ed[k]) − s_max_pen[t] ≤ usable−soc0
    #      Lower (soft): −Σ_{k≤t}(ec[k]−ed[k]) − s_min_pen[t] ≤ soc0
    #      Penalty variables s_max_pen[t] and s_min_pen[t] absorb violations
    #      at high cost, preventing infeasibility from out-of-bounds initial SoC.
    #   2. Mutual exclusion: ec[t]/max_charge + ed[t]/max_dis ≤ 1
    #   3. ec[t] ≤ max_charge_per_slot  (via bounds)
    #   4. ed[t] ≤ max_dis              (via bounds)
    # ------------------------------------------------------------------
    # We encode SoC bounds as inequality rows:
    #   upper: cumsum(ec−ed)[t] − s_max_pen[t] ≤ (usable_kwh − current_kwh)
    #   lower: −cumsum(ec−ed)[t] − s_min_pen[t] ≤ current_kwh
    soc_rows = 2 * m
    # Mutual exclusion rows: ec[t]/max_charge + ed[t]/max_dis <= 1
    mutex_rows = m
    # Cycle cost auxiliary rows: m[t] >= ec[t] and m[t] >= ed[t]
    #   → -m[t] + ec[t] <= 0  and  -m[t] + ed[t] <= 0
    cycle_rows = 2 * m
    A_ub = np.zeros((soc_rows + mutex_rows + cycle_rows, n_vars))  # NOSONAR
    b_ub = np.zeros(soc_rows + mutex_rows + cycle_rows)

    for t in range(m):
        for k in range(t + 1):
            # Upper SoC bound row (soft)
            A_ub[t, ec_off + k] = 1.0
            A_ub[t, ed_off + k] = -1.0
            # Lower SoC bound row (soft)
            A_ub[m + t, ec_off + k] = -1.0
            A_ub[m + t, ed_off + k] = 1.0
        # Penalty variable absorbs violation in upper bound
        A_ub[t, s_max_off + t] = -1.0
        # Penalty variable absorbs violation in lower bound
        A_ub[m + t, s_min_off + t] = -1.0
        b_ub[t] = usable_kwh - current_kwh  # upper SoC headroom
        b_ub[m + t] = current_kwh  # lower SoC headroom

        # Mutual exclusion: ec[t]/max_charge + ed[t]/max_dis <= 1
        A_ub[2 * m + t, ec_off + t] = 1.0 / max_charge_per_slot
        A_ub[2 * m + t, ed_off + t] = 1.0 / max_dis
        b_ub[2 * m + t] = 1.0

    # Cycle cost auxiliary: m[t] >= ec[t]  →  -m[t] + ec[t] <= 0
    #                     m[t] >= ed[t]  →  -m[t] + ed[t] <= 0
    cycle_row_start = soc_rows + mutex_rows  # = 3m
    for t in range(m):
        A_ub[cycle_row_start + t, ec_off + t] = 1.0
        A_ub[cycle_row_start + t, m_off + t] = -1.0
        b_ub[cycle_row_start + t] = 0.0
        A_ub[cycle_row_start + m + t, ed_off + t] = 1.0
        A_ub[cycle_row_start + m + t, m_off + t] = -1.0
        b_ub[cycle_row_start + m + t] = 0.0

    # ------------------------------------------------------------------
    # EV constraints (only when active_evs is non-empty)
    # ------------------------------------------------------------------
    # Row counts for EV constraints
    ev_soc_rows = num_evs * m  # cumulative SOC upper bound per EV
    ev_deadline_rows = sum(
        1
        for ev in active_evs
        if ev.deadline_slot is not None and ev.target_kwh > ev.initial_soc_kwh + 1e-9
    )
    ev_total_rows = ev_soc_rows + ev_deadline_rows

    if ev_total_rows > 0:
        # Extend A_ub and b_ub to accommodate EV rows
        existing_rows = soc_rows + mutex_rows + cycle_rows
        A_ub_old = A_ub
        b_ub_old = b_ub
        A_ub = np.zeros((existing_rows + ev_total_rows, n_vars))
        b_ub = np.zeros(existing_rows + ev_total_rows)
        A_ub[:existing_rows, :] = A_ub_old
        b_ub[:existing_rows] = b_ub_old

        ev_row = existing_rows
        for ev_idx, ev in enumerate(active_evs):
            ev_off = ev_var_offsets[ev_idx]
            # EV SOC upper bound per slot: Σ_{k≤t} ev_c[k] ≤ cap − init
            #   For each t in 0..m-1:
            #   Σ_{k=0..t} ev_c[k] ≤ ev.capacity_kwh - ev.initial_soc_kwh
            headroom = max(ev.capacity_kwh - ev.initial_soc_kwh, 0.0)
            for t in range(m):
                for k in range(t + 1):
                    A_ub[ev_row + t, ev_off + k] = 1.0
                b_ub[ev_row + t] = headroom
            ev_row += m

            # EV deadline soft constraint:
            # initial_soc + Σ_{k≤D} ev_c[k] + penalty ≥ target
            # → -Σ_{k≤D} ev_c[k] - penalty ≤ initial_soc - target
            if (
                ev.deadline_slot is not None
                and ev.target_kwh > ev.initial_soc_kwh + 1e-9
            ):
                d = ev.deadline_slot
                # Clamp deadline to valid range
                d = max(0, min(d, m - 1))
                for k in range(d + 1):
                    A_ub[ev_row, ev_off + k] = -1.0
                A_ub[ev_row, ev_pen_offsets[ev_idx]] = -1.0
                b_ub[ev_row] = ev.initial_soc_kwh - ev.target_kwh
                ev_row += 1

    # ------------------------------------------------------------------
    # Variable bounds: all ≥ 0, charge/discharge capped by power limits.
    # Penalty variables are unbounded above (can absorb arbitrary
    # violations) and non-negative (violations cannot be negative).
    # ------------------------------------------------------------------
    bounds: list[tuple[float, float | None]] = (
        [(0.0, max_charge_per_slot)] * m  # ec[t]
        + [(0.0, max_dis)] * m  # ed[t]
        + [(0.0, None)] * m  # gi[t] (unbounded above)
        + [(0.0, None)] * m  # ge[t] (unbounded above)
        + [
            (pv_avail[t], pv_avail[t]) for t in range(m)
        ]  # pv[t] fixed to actual surplus
        + [(0.0, None)] * m  # m[t] (auxiliary, unbounded above, ≥ 0)
        + [(0.0, None)] * m  # s_max_pen[t] (penalty, ≥ 0)
        + [(0.0, None)] * m  # s_min_pen[t] (penalty, ≥ 0)
    )
    # --- EV bounds ---
    for ev in active_evs:
        # ev_c[t] bounded by [0, ev.max_charge_per_slot]
        bounds += [(0.0, ev.max_charge_per_slot)] * m
        # ev deadline penalty: [0, unbounded)
        bounds.append((0.0, None))

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
    # Decode solution and build output slot list
    # ------------------------------------------------------------------
    ec_sol = result.x[ec_off : ec_off + m]
    ed_sol = result.x[ed_off : ed_off + m]

    out_slots: list[PlannedSlot] = [copy.copy(s) for s in slots]

    # Reset charge/discharge and EV fields on all future slots; past slots keep TimePassed
    for i in future_idx:
        out_slots[i].recommendation = None
        out_slots[i].batteries_charged_kwh = 0.0
        out_slots[i].ev_planned_load_kwh = 0.0
        out_slots[i].ev_accounted_load_kwh = 0.0
        out_slots[i].ev_total_planned_load_kwh = 0.0

    # Write MILP-derived charge/discharge actions
    for lp_t, slot_i in enumerate(future_idx):
        ec_kwh = float(ec_sol[lp_t])
        ed_kwh = float(ed_sol[lp_t])
        ge_kwh = float(result.x[ge_off + lp_t])  # grid export from LP

        if ec_kwh > _MIN_ACTION_KWH and ed_kwh > _MIN_ACTION_KWH:
            # Mutual exclusion is guaranteed by the LP constraint
            # (ec/max_charge + ed/max_dis <= 1).  If we reach here,
            # it is due to numerical tolerance.  Resolve by checking
            # whether the round-trip is net profitable (Bug J fix).
            # The value of discharging is avoided import (p_imp),
            # not export revenue (p_exp).
            net_charge_profit = (
                p_imp[lp_t] * discharge_eff
                - p_imp[lp_t] / charge_eff
                - 2.0 * cycle_cost_per_kwh
            )
            if net_charge_profit > 0:
                ed_kwh = 0.0
            else:
                ec_kwh = 0.0
                ed_kwh = 0.0

        if ec_kwh > _MIN_ACTION_KWH:
            # Use BatteriesChargeSolar when PV surplus is available,
            # BatteriesChargeGrid otherwise.
            if pv_avail[lp_t] > _MIN_ACTION_KWH:
                out_slots[
                    slot_i
                ].recommendation = Recommendations.BatteriesChargeSolar.value
            else:
                out_slots[
                    slot_i
                ].recommendation = Recommendations.BatteriesChargeGrid.value
            out_slots[slot_i].batteries_charged_kwh = round(ec_kwh, 3)
        elif ed_kwh > _MIN_ACTION_KWH:
            # If the LP is exporting (ge > 0) in this slot, use
            # ForceBatteriesDischarge to signal that the battery should
            # cover house load AND export excess to grid.
            if ge_kwh > _MIN_ACTION_KWH and p_exp[lp_t] >= min_export_price:
                out_slots[
                    slot_i
                ].recommendation = Recommendations.ForceBatteriesDischarge.value
            else:
                out_slots[
                    slot_i
                ].recommendation = Recommendations.BatteriesDischargeMode.value

    # ------------------------------------------------------------------
    # Write MILP-derived EV charging decisions to output slots
    # ------------------------------------------------------------------
    if active_evs:
        for ev_idx, ev in enumerate(active_evs):
            ev_off = ev_var_offsets[ev_idx]
            ev_c_sol = result.x[ev_off : ev_off + m]
            for lp_t, slot_i in enumerate(future_idx):
                ev_dc_kwh = float(ev_c_sol[lp_t])
                if ev_dc_kwh < _MIN_ACTION_KWH:
                    continue
                # AC load = DC / charger_eff (grid/PV draw)
                ac_load = round(ev_dc_kwh / ev.charger_efficiency, 3)
                # Accumulate into slot EV fields (additive for multiple EVs)
                if ev.base_load_includes_ev:
                    out_slots[slot_i].ev_accounted_load_kwh += ac_load
                else:
                    out_slots[slot_i].ev_planned_load_kwh += ac_load
                out_slots[slot_i].ev_total_planned_load_kwh += ac_load
        # Recompute estimated_net_consumption_kwh and estimated_cost_currency
        # to reflect new EV loads
        for i in future_idx:
            s = out_slots[i]
            s.estimated_net_consumption_kwh = (
                s.avg_house_consumption_kwh
                + s.ev_planned_load_kwh
                - s.solcast_pv_estimate_kwh
            )
            net = s.estimated_net_consumption_kwh
            if net > 0:
                s.estimated_cost_currency = round(net * s.price.import_price, 4)
            else:
                s.estimated_cost_currency = round(net * s.price.export_price, 4)

    # ------------------------------------------------------------------
    # Extract penalty variable values and compute violation diagnostics
    # ------------------------------------------------------------------
    s_max_pen_sol = result.x[s_max_off : s_max_off + m]
    s_min_pen_sol = result.x[s_min_off : s_min_off + m]

    s_max_pen_list = [float(v) for v in s_max_pen_sol]
    s_min_pen_list = [float(v) for v in s_min_pen_sol]
    total_violation = sum(s_max_pen_list) + sum(s_min_pen_list)
    has_violations = total_violation > 1e-6

    if has_violations:
        violating_slots: list[dict] = []
        for t in range(m):
            slot_i = future_idx[t]
            s_start = slots[slot_i].start.isoformat()
            if s_max_pen_list[t] > 1e-6:
                violating_slots.append(
                    {
                        "slot": t,
                        "time": s_start,
                        "type": "s_max_pen",
                        "kwh": round(s_max_pen_list[t], 4),
                    }
                )
            if s_min_pen_list[t] > 1e-6:
                violating_slots.append(
                    {
                        "slot": t,
                        "time": s_start,
                        "type": "s_min_pen",
                        "kwh": round(s_min_pen_list[t], 4),
                    }
                )
        log_planner(
            "warning",
            "[milp] SoC penalty violations detected: total=%.4f kWh, %d violating slots",
            total_violation,
            len(violating_slots),
        )
        for v in violating_slots:
            log_planner(
                "warning",
                "[milp] Penalty slot %d (%s) %s: %.4f kWh",
                v["slot"],
                v["time"],
                v["type"],
                v["kwh"],
            )

    log_planner(
        "debug",
        "[milp] LP solved: objective=%.4f  charge_slots=%d  discharge_slots=%d"
        "  replacement_price=%s  penalty_total=%.4f  has_violations=%s"
        "  ev_slots=%d",
        float(result.fun),
        sum(
            1
            for i in future_idx
            if out_slots[i].recommendation
            in (
                Recommendations.BatteriesChargeGrid.value,
                Recommendations.BatteriesChargeSolar.value,
            )
        ),
        sum(
            1
            for i in future_idx
            if out_slots[i].recommendation
            == Recommendations.BatteriesDischargeMode.value
        ),
        (
            f"{replacement_price_per_kwh:.4f}"
            if replacement_price_per_kwh is not None
            else "(none)"
        ),
        total_violation,
        has_violations,
        sum(1 for s in out_slots if abs(s.ev_total_planned_load_kwh) > _MIN_ACTION_KWH),
    )

    diagnostics: dict = {
        "s_max_pen": s_max_pen_list,
        "s_min_pen": s_min_pen_list,
        "has_violations": has_violations,
        "total_violation_kwh": round(total_violation, 4),
    }

    # --- EV diagnostics ---
    if active_evs:
        ev_diag: dict = {}
        for ev_idx, _ev in enumerate(active_evs):
            ev_off = ev_var_offsets[ev_idx]
            ev_c_sol = result.x[ev_off : ev_off + m]
            ev_total_dc = float(np.sum(ev_c_sol))
            ev_pen_val = float(result.x[ev_pen_offsets[ev_idx]])
            ev_diag[f"ev{ev_idx}"] = {
                "total_dc_kwh": round(ev_total_dc, 4),
                "deadline_penalty_kwh": round(ev_pen_val, 4),
                "deadline_met": ev_pen_val < 1e-6,
            }
        diagnostics["ev"] = ev_diag

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

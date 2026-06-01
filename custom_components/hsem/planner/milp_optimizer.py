"""MILP-based optimal battery charge/discharge scheduler.

This module implements a **Mixed Integer Linear Program** that finds the
globally cost-optimal charge/discharge schedule for the planning horizon.
It is the PRIMARY planner — heuristic candidates are disabled.

Algorithm overview
------------------
The scheduler is formulated as a **continuous LP** (not MILP) using the
HiGHS solver via ``scipy.optimize.linprog``.  Binary charge/discharge flags
are relaxed to continuous values in [0, 1] because the mutual-exclusion
constraint together with the per-slot energy caps already prevents
simultaneous charge + discharge in the optimal solution.

Decision variables (flattened into a single vector ``x`` of length 6*n)
-----------------------------------------------------------------------
For each slot ``t ∈ 0…n-1``:

- ``ec[t]``  — energy charged and *stored* in the battery this slot (kWh)
- ``ed[t]``  — energy discharged *from* the battery this slot (kWh)
- ``gi[t]``  — total grid import this slot (kWh)
- ``ge[t]``  — total grid export this slot (kWh)
- ``pv[t]``  — PV energy available after house consumption (kWh, fixed)
- ``m[t]``   — auxiliary variable = max(ec[t], ed[t]) for cycle cost (kWh)

``soc[t]`` is derived from ``ec`` and ``ed`` via the forward recurrence and
does not need to be an explicit variable.

Objective (minimise)
--------------------
``Σ_t [ p_imp[t] * gi[t] - p_exp[t] * ge[t] + α * m[t]
       + γ * (ed[t] - ec[t]) ]``

where ``α`` = battery cycle cost per kWh and ``γ`` = terminal-SoC
replacement price (opportunity cost of ending the horizon with less stored
energy).  The cycle cost uses the auxiliary variable ``m[t]`` which is
constrained to satisfy ``m[t] ≥ max(ec[t], ed[t])``, matching
``cost_function.py``'s ``max(charge, discharge)`` counting.  The
``cycle_cost_per_kwh`` already includes the 2× throughput factor in its
denominator (``purchase_price / (2 × usable_kwh × expected_cycles)``),
so one full round-trip (charge + discharge) correctly costs
``2 × usable_kwh × cycle_cost_per_kwh = purchase_price / expected_cycles``.

Constraints
-----------
For each slot ``t``:

1. **SoC forward recurrence** (equality):
   ``soc[t] = soc[t-1] + ec[t] - ed[t]``
   (internal SoC in kWh above the discharge floor)

2. **SoC bounds** (inequality):
   ``soc[t] ≥ 0``  and  ``soc[t] ≤ usable_kwh``

3. **Charge limit** (inequality):
   ``ec[t] ≤ max_charge_per_slot``

4. **Discharge limit** (inequality):
   ``ed[t] ≤ max_discharge_per_slot``  (relaxed to usable_kwh when unlimited)

5. **Mutual exclusion** (inequality):
   ``ec[t] / max_charge_per_slot + ed[t] / max_discharge_per_slot ≤ 1``
   Prevents simultaneous charge + discharge without binary variables.

6. **Energy balance** (equality):
   ``gi[t] + pv_avail[t] + ed[t] * discharge_eff
       = load[t] + ec[t] / charge_eff + ge[t]``

   Where ``pv_avail[t]`` is the PV energy available after house consumption is
   subtracted.  Because PV first serves the house load, the net residual PV
   is what the battery or export can absorb.

7. **Non-negativity**: All variables ≥ 0.

Past slots (recommendation == TimePassed) are fixed at zero by using
zero-capacity bounds and are excluded from the energy balance.

Solving
-------
``scipy.optimize.linprog(method='highs')`` is used.  For a 96-slot (48 h ×
30 min) horizon this solves in well under 50 ms on commodity hardware.
No extra dependencies beyond ``scipy`` (already a Home Assistant dependency).

Fallback
--------
If the solver fails (infeasible, numerical issue, or timeout), this module
returns ``None``.  The engine falls back to the rule-based baseline.

Design constraints
------------------
- **Pure Python, no Home Assistant imports** — testable with plain pytest.
- Only mutates the output ``recommendation`` / ``batteries_charged`` fields
  on deep-copied slots; never touches the caller's baseline.
- Respects the same SoC bounds and power limits as :mod:`soc_simulation`.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import clamp_efficiency
from custom_components.hsem.utils.recommendations import Recommendations

if TYPE_CHECKING:
    from custom_components.hsem.models.planner_outputs import PlannedSlot

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
    export_min_price: float = 0.0,
    recommended_threshold: float = 0.0,
) -> list[PlannedSlot] | None:
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
        export_min_price:
            Minimum export price (local currency/kWh) below which the inverter
            physically blocks export (``GRID_EXPORT_LIMIT_WATT``).  Sourced from
            ``hsem_export_electricity_min_price``.  Export prices below this
            threshold are clamped to 0 before the LP solves, because the
            applier will prevent any export at those prices.  Defaults to 0.0.
        recommended_threshold:
            Recommended discharge price threshold (local currency/kWh) computed
            by ``calculate_recommended_threshold()``.  Used in post-processing
            to decide between ``ForceBatteriesDischarge`` (export is worthwhile)
            and ``BatteriesDischargeMode`` (house-load only).  Defaults to 0.0.

    Returns:
        A list of :class:`PlannedSlot` copies with MILP-derived recommendations,
        or ``None`` if the solver fails or the problem is infeasible.
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
    # 2. Prices below export_min_price → 0 (the applier sets the inverter to
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

    if export_min_price > 1e-9:
        blocked = p_exp < export_min_price
        n_blocked = int(np.sum(blocked))
        if n_blocked > 0:
            log_planner(
                "debug",
                "[milp] Clamping %d export prices below min_price (%.4f) to 0 "
                "(max clamped=%.4f)",
                n_blocked,
                export_min_price,
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

    # Reduce PV surplus by EV load: the EV charger is fed from excess PV
    # first, so energy that would charge the battery goes to the EV instead.
    ev_total = np.array(
        [
            slots[i].ev_planned_load_kwh + slots[i].ev_accounted_load_kwh
            for i in future_idx
        ],
        dtype=float,
    )
    if np.any(ev_total > 1e-9):
        n_ev_slots = int(np.sum(ev_total > 1e-9))
        total_ev_pv = float(np.sum(np.minimum(ev_total, pv_avail)))
        pv_avail = np.maximum(pv_avail - ev_total, 0.0)
        log_planner(
            "debug",
            "[milp] EV adjustment: %d slot(s) with EV load, "
            "%.3f kWh PV diverted from battery to EV",
            n_ev_slots,
            total_ev_pv,
        )
    m = len(future_idx)  # number of active LP slots

    # ------------------------------------------------------------------
    # Variable layout: x = [ec(0..m-1), ed(0..m-1), gi(0..m-1), ge(0..m-1),
    #                       pv(0..m-1), m(0..m-1)]
    # Offsets: ec_off=0, ed_off=m, gi_off=2m, ge_off=3m, pv_off=4m, m_off=5m
    # pv[t] is a FIXED variable bounded to [pv_avail[t], pv_avail[t]] — the
    # solar energy available after house consumption.  Making it explicit in
    # the LP (rather than netting it into b_eq) prevents infeasibility when
    # net_load is strongly negative and SoC constraints limit charge/export.
    # m[t] is the auxiliary variable for cycle cost: m[t] = max(ec[t], ed[t]).
    # ------------------------------------------------------------------
    ec_off, ed_off, gi_off, ge_off, pv_off, m_off = 0, m, 2 * m, 3 * m, 4 * m, 5 * m
    n_vars = 6 * m

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
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Equality constraints: energy balance per slot
    # gi[t] + pv[t] + ed[t]*discharge_eff = base_load[t] + ec[t]/charge_eff + ge[t]
    # -> gi[t] - ec[t]/charge_eff + ed[t]*discharge_eff + pv[t] - ge[t] = base_load[t]
    #
    # ec[t] is the battery-side stored energy (post charge loss).  To store
    # ec[t] kWh the grid + PV must supply ec[t]/charge_eff kWh.
    # ed[t] is the battery-side removed energy (pre discharge loss).  The
    # house receives ed[t]*discharge_eff kWh.
    # ------------------------------------------------------------------
    A_eq = np.zeros((m, n_vars))  # NOSONAR
    for t in range(m):
        A_eq[t, ec_off + t] = -1.0 / charge_eff  # -ec[t]/charge_eff
        A_eq[t, ed_off + t] = 1.0 * discharge_eff  # +ed[t]*discharge_eff
        A_eq[t, gi_off + t] = 1.0  # +gi[t]
        A_eq[t, ge_off + t] = -1.0  # -ge[t]
        A_eq[t, pv_off + t] = 1.0  # +pv[t] (fixed to pv_avail[t])
    b_eq = base_load.copy()  # always non-negative — pv[t] covers surplus

    # ------------------------------------------------------------------
    # Inequality constraints:
    #   1. SoC recurrence: soc[t] = soc[0] + Σ_{k≤t} (t) (ec[k] − ed[k])
    #      We need: soc[t] ≤ usable_kwh  →  Σ_{k≤t}(ec[k]−ed[k]) ≤ usable−soc0
    #      And:    soc[t] ≥ 0          → -Σ_{k≤t}(ec[k]−ed[k]) ≤ soc0
    #   2. Mutual exclusion: ec[t]/max_charge + ed[t]/max_dis ≤ 1
    #   3. ec[t] ≤ max_charge_per_slot  (via bounds)
    #   4. ed[t] ≤ max_dis              (via bounds)
    # ------------------------------------------------------------------
    # We encode SoC bounds as inequality rows:
    #   upper: cumsum(ec−ed)[t] ≤ (usable_kwh − current_kwh)
    #   lower: −cumsum(ec−ed)[t] ≤ current_kwh
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
            # Upper SoC bound row
            A_ub[t, ec_off + k] = 1.0
            A_ub[t, ed_off + k] = -1.0
            # Lower SoC bound row
            A_ub[m + t, ec_off + k] = -1.0
            A_ub[m + t, ed_off + k] = 1.0
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
    # Variable bounds: all ≥ 0, charge/discharge capped by power limits
    # ------------------------------------------------------------------
    bounds = (
        [(0.0, max_charge_per_slot)] * m  # ec[t]
        + [(0.0, max_dis)] * m  # ed[t]
        + [(0.0, None)] * m  # gi[t] (unbounded above)
        + [(0.0, None)] * m  # ge[t] (unbounded above)
        + [
            (pv_avail[t], pv_avail[t]) for t in range(m)
        ]  # pv[t] fixed to actual surplus
        + [(0.0, None)] * m  # m[t] (auxiliary, unbounded above, ≥ 0)
    )

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

    # Reset charge/discharge on all future slots; past slots keep TimePassed
    for i in future_idx:
        out_slots[i].recommendation = None
        out_slots[i].batteries_charged_kwh = 0.0

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
            if ge_kwh > _MIN_ACTION_KWH and p_exp[lp_t] >= max(
                export_min_price, recommended_threshold
            ):
                out_slots[
                    slot_i
                ].recommendation = Recommendations.ForceBatteriesDischarge.value
            else:
                out_slots[
                    slot_i
                ].recommendation = Recommendations.BatteriesDischargeMode.value

    log_planner(
        "debug",
        "[milp] LP solved: objective=%.4f  charge_slots=%d  discharge_slots=%d"
        "  replacement_price=%s",
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
    )

    return out_slots


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

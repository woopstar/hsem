"""Build the MILP objective vector (cost terms per decision variable).

Extracted from ``solve_milp`` so the orchestrator remains under 30 KB.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np

from custom_components.hsem.planner.cost_helpers import (
    compute_charge_premium,
    deferred_export_price_by_slot,
)
from custom_components.hsem.utils.units import hours_ahead

if TYPE_CHECKING:
    from custom_components.hsem.models.ev_config import EVConfig
    from custom_components.hsem.models.planned_slot import PlannedSlot


def _build_objective(
    slots: list[PlannedSlot],
    future_idx: list[int],
    now: datetime,
    m: int,
    n_vars: int,
    ec_off: int,
    ed_off: int,
    gi_off: int,
    ge_off: int,
    m_off: int,
    s_max_off: int,
    s_min_off: int,
    gi_pen_off: int,
    ev_var_offsets: list[int],
    ev_pen_offsets: list[int],
    active_evs: list[EVConfig],
    p_imp: np.ndarray,  # type: ignore[name-defined]
    p_imp_obj: np.ndarray,  # type: ignore[name-defined]
    p_exp: np.ndarray,  # type: ignore[name-defined]
    p_soc: float,
    cycle_cost_per_kwh: float,
    charge_loss: float,
    discharge_loss: float,
    time_discount_rate: float,
    replacement_price_per_kwh: float | None,
    fuse_active: bool,
    usable_kwh: float = 0.0,
    max_charge_per_slot: float = 0.0,
) -> np.ndarray:  # type: ignore[name-defined]
    """Build the linear objective vector for the MILP.

    Returns:
        Numpy array ``c_obj`` of length ``n_vars``.
    """
    import numpy as np

    c_obj = np.zeros(n_vars)

    # Deferred-export correction (issue #592): for each LP slot, the
    # minimum export price among later slots whose PV surplus exceeds the
    # battery's per-slot absorption capacity.  Indexed parallel to
    # ``future_idx`` (LP-local index t → deferred price or None).
    _deferred_by_lp_idx: list[float | None] | None = None
    if (
        usable_kwh > 1e-9
        and max_charge_per_slot > 1e-9
        and replacement_price_per_kwh is not None
        and abs(replacement_price_per_kwh) > 1e-9
    ):
        _by_slot_idx = deferred_export_price_by_slot(
            slots,
            usable_kwh=usable_kwh,
            max_charge_per_slot=max_charge_per_slot,
            now=now,
        )
        _deferred_by_lp_idx = [_by_slot_idx[i] for i in future_idx]

    p_imp_max = float(np.max(p_imp)) if m > 0 else 0.1
    use_discount = time_discount_rate < 1.0 - 1e-9

    for t in range(m):
        discount = 1.0
        if use_discount:
            # Compute hours from now for this slot's midpoint
            slot = slots[future_idx[t]]
            slot_mid = slot.start + (slot.end - slot.start) / 2
            ha = hours_ahead(now, slot_mid)
            discount = time_discount_rate**ha

        # Charge-side conversion loss: energy lost during charge, priced at
        # this slot's import price (where the charge occurs).
        c_obj[ec_off + t] = (charge_loss * p_imp_obj[t]) * discount
        # Discharge-side conversion loss: energy lost during discharge.
        # Priced at the sanitised import price of the discharge slot.
        #
        # NOTE (issue #641): This is a CONSERVATIVE APPROXIMATION.  The
        # LP cannot know the destination of discharged energy (house load
        # vs. export) before solving, because the gi[t]/ge[t] split is
        # itself an LP decision.  Defaulting to the (typically higher)
        # import price is the safe choice — it never leads the LP to be
        # overly optimistic about an export cycle's profitability.
        #
        # The accurate destination-aware cost is computed post-hoc in
        # cost_function.py::score_plan() (which sees the solved
        # grid_export_kwh/grid_import_kwh fields) and reported in the
        # diagnostics dict as "discharge_loss_cost_destination_aware".
        c_obj[ed_off + t] = (discharge_loss * p_imp_obj[t]) * discount
        # Cycle cost through auxiliary variable m[t] (= max(ec, ed))
        c_obj[m_off + t] = cycle_cost_per_kwh * discount
        c_obj[gi_off + t] = p_imp_obj[t] * discount  # grid import cost
        c_obj[ge_off + t] = -p_exp[t] * discount  # export revenue (negative = gain)
        # pv[t] has zero objective cost
        # curt[t] has zero objective cost (curtailment is free)

        # Terminal-SoC term in the objective (undiscounted).
        # Values the opportunity cost of ending the horizon with more or
        # less stored battery energy.  Every unit of charge/discharge
        # anywhere in the horizon contributes to the final cumulative SoC:
        #   terminal_soc_value = (Σed - Σec) * replacement_price_per_kwh
        # Charging (ec) earns a credit, discharging (ed) incurs a penalty.
        #
        # IMPORTANT: the per-slot incentive is capped by the
        # opportunity-cost DIFFERENTIAL between the replacement price and
        # this slot's import price.  When replacement_price ≤ p_imp[t],
        # energy is worth the same or less later than now, so the
        # terminal-SoC term must not discourage a genuine discharge
        # decision (covering house load with an otherwise-idle battery).
        # This prevents the regression identified in issue #638 where
        # flat-price scenarios saw zero discharge because the uniform
        # +replacement_price penalty dominated the per-slot import-saving
        # benefit.
        #
        # The differential is computed against the sanitised import price
        # (p_imp_obj, non-negative) so that negative import prices cannot
        # artificially inflate the terminal premium.
        #
        # SECOND CAP (issue #694): the terminal premium must never make
        # battery charging more attractive than grid export for the same
        # slot.  Without this cap, a high replacement_price can cause the
        # LP to charge from solar during expensive hours instead of
        # exporting at peak prices — a "tunnel-vision" effect where the
        # LP rushes to satisfy the terminal-SoC target immediately rather
        # than deferring charging to cheaper slots with ample solar.
        #
        #   Export benefit:  -p_exp[t]
        #   Charge cost:     (charge_loss·p_imp[t] - premium + cycle_cost) · η_chg
        #
        # Requiring export ≤ charge (both negative = both beneficial):
        #   -p_exp[t] ≤ (charge_loss·p_imp[t] - premium + cycle_cost) · η_chg
        #   → premium ≤ charge_loss·p_imp[t] + cycle_cost + p_exp[t] / η_chg
        #
        # The cap only reduces the terminal premium — it can never increase
        # it.  When p_exp[t] is high the cap is large and rarely binds;
        # when p_exp[t] is low (cheap slots) the cap is small, but the LP
        # still charges in those slots because the global optimum values
        # stored energy for future discharge windows.
        #
        # Deferred-export correction (issue #592): the #694 cap compares
        # charging against exporting in the SAME slot.  When a future slot
        # has PV surplus beyond what the battery can absorb, that surplus
        # is exported regardless — so the true opportunity cost of charging
        # now is the spread between this slot's (high) export price and the
        # future slot's (low) export price.  ``compute_charge_premium``
        # restores that spread so the LP charges now at high prices and
        # lets the inevitable future surplus refill at low prices.
        if (
            replacement_price_per_kwh is not None
            and abs(replacement_price_per_kwh) > 1e-9
        ):
            terminal_premium = max(0.0, replacement_price_per_kwh - p_imp_obj[t])
            # Cap the CHARGE credit only: the terminal premium for
            # charging is reduced by the opportunity cost of not
            # exporting the same PV surplus (issue #694).
            #
            #   charge_premium = repl - p_imp - p_exp / η_chg
            #
            # This ensures that when export prices are high (expensive
            # slots), the charge credit is small and the LP exports.
            # When export prices are low (cheap slots), the charge
            # credit is close to the full terminal premium and the
            # LP charges to store energy for future discharge windows.
            #
            # The discharge penalty is NOT capped — it remains at the
            # full terminal_premium to prevent unnecessary discharging
            # (issue #638).
            _charge_eff = 1.0 - charge_loss
            _charge_premium = compute_charge_premium(
                replacement_price_per_kwh=replacement_price_per_kwh,
                imp_price_obj=p_imp_obj[t],
                exp_price=p_exp[t],
                charge_eff=_charge_eff,
                deferred_export_price=(
                    _deferred_by_lp_idx[t] if _deferred_by_lp_idx else None
                ),
            )
            c_obj[ec_off + t] -= _charge_premium  # capped credit for charging
            c_obj[ed_off + t] += terminal_premium  # full penalty for discharging

        # Penalty costs: high enough that penalties are zero when SoC is
        # within bounds, but absorb violations when the initial SoC is
        # outside [0, usable_kwh] (e.g., overcharged battery).
        c_obj[s_max_off + t] = p_soc * discount
        c_obj[s_min_off + t] = p_soc * discount

    # --- EV deadline penalty (undiscounted — deadline is a hard commitment) ---
    # Must be high enough that the MILP always prefers meeting the target
    # when it is physically possible within the available slots.
    for ev_idx, ev in enumerate(active_evs):
        if (
            ev.deadline_slot is not None
            and ev.target_kwh > ev.initial_soc_kwh + 1e-9
            and not ev.charge_past_target
        ):
            # Penalty per kWh shortfall: proportional to energy needed,
            # not full capacity. This ensures the MILP prioritizes the EV
            # when it needs significant energy, but doesn't force EV charging
            # when it only needs a small top-up (e.g., 90% -> 100%) at the
            # expense of a critically low house battery.
            energy_needed = ev.target_kwh - ev.initial_soc_kwh
            ev_penalty_cost = max(p_imp_max, 0.1) * max(energy_needed, 1.0) * 10.0
            c_obj[ev_pen_offsets[ev_idx]] = ev_penalty_cost

            # Direct benefit on ev_c[t]: the avoided penalty per kWh of DC
            # charge delivered before the deadline.  Without this, the LP
            # sees ev_c[t] as having zero benefit — only the slack penalty
            # provides an incentive, and the LP may prefer paying the penalty
            # over importing expensive grid power to charge the EV.
            #
            # The benefit equals the penalty cost per kWh, so the LP always
            # prefers charging over paying the penalty.  Slots before the
            # deadline get the full benefit; slots after get zero coefficient.
            # Post-deadline charging is forbidden by hard constraints below
            # unless charge_past_target is enabled (surplus PV only).
            ev_off = ev_var_offsets[ev_idx]
            d = ev.deadline_slot
            d = max(0, min(d, m - 1))
            for t in range(m):
                if t <= d:
                    # Negative coefficient = benefit (reduces objective).
                    # The benefit is the avoided penalty per kWh DC.
                    c_obj[ev_off + t] -= ev_penalty_cost
                # Post-deadline slots: no benefit (coefficient stays 0).
                # Hard constraints below prevent charging unless
                # charge_past_target is enabled.

    # --- EV charge-past-target benefit ---
    # When an EV is already at its user-configured target SoC but
    # charge_past_target is enabled, EV charging is valued at
    # ev.future_value_per_kwh — the avoided cost of importing that same
    # energy later (see ev_future_charge_value_per_kwh in
    # candidate_selector.py), so it competes fairly against export revenue
    # (p_exp) and house battery charging on real currency terms.
    #
    # When no future price data is available (future_value_per_kwh is
    # None), fall back to a tiny fixed tiebreaker benefit (0.0001 per kWh
    # AC) so surplus PV still prefers the EV over being wastefully
    # curtailed/exported at near-zero or negative prices.
    for ev_idx, ev in enumerate(active_evs):
        if ev.charge_past_target:
            ev_off = ev_var_offsets[ev_idx]
            ev_value = (
                ev.future_value_per_kwh
                if ev.future_value_per_kwh is not None
                else 0.0001
            )
            for t in range(m):
                discount = 1.0
                if use_discount:
                    slot = slots[future_idx[t]]
                    slot_mid = slot.start + (slot.end - slot.start) / 2
                    ha = hours_ahead(now, slot_mid)
                    discount = time_discount_rate**ha
                # Negative coefficient = reduces objective = benefit.
                c_obj[ev_off + t] -= (ev_value / ev.charger_efficiency) * discount

    # --- Fuse penalty cost (same magnitude as SOC penalties) ---
    # P_fuse = max(p_imp) * 100 — high enough that the solver only exceeds
    # the fuse limit when physically unavoidable.
    if fuse_active:
        p_fuse = max(p_imp_max, 0.1) * 100.0
        for t in range(m):
            discount = 1.0
            if use_discount:
                slot = slots[future_idx[t]]
                slot_mid = slot.start + (slot.end - slot.start) / 2
                ha = hours_ahead(now, slot_mid)
                discount = time_discount_rate**ha
            c_obj[gi_pen_off + t] = p_fuse * discount

    return c_obj

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

# Resolve source-attribution degeneracy without materially changing economics.
_BATTERY_EXPORT_SOURCE_TIEBREAK = 1e-7

# Resolve otherwise free PV/zero-price deadline allocations toward the
# smallest executable whole-amp energy above the target (issue #797).
_EV_TARGET_ENERGY_TIEBREAK_COST = 1e-7

# When even max-power charging can't reach the margined deadline target
# (EVConfig.deadline_escalated), steepen the deadline penalty so the LP
# treats closing the gap as more urgent than its already-high baseline
# priority (issue #845).
_EV_DEADLINE_ESCALATION_PENALTY_MULTIPLIER = 5.0


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
    battery_export_off: int,
    m_off: int,
    s_max_off: int,
    s_min_off: int,
    gi_pen_off: int,
    ev_var_offsets: list[int],
    ev_pen_offsets: list[int],
    active_evs: list[EVConfig],
    p_imp_obj: np.ndarray,  # type: ignore[name-defined]
    p_exp: np.ndarray,  # type: ignore[name-defined]
    p_soc: float,
    cycle_cost_per_kwh: float,
    charge_eff: float,
    time_discount_rate: float,
    replacement_price_per_kwh: float | None,
    fuse_active: bool,
    usable_kwh: float = 0.0,
    max_charge_per_slot: float = 0.0,
    current_kwh: float = 0.0,
    pv_avail: np.ndarray | None = None,  # type: ignore[name-defined]
    base_load: np.ndarray | None = None,  # type: ignore[name-defined]
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

    p_imp_max = float(np.max(p_imp_obj)) if m > 0 else 0.1
    use_discount = time_discount_rate < 1.0 - 1e-9

    for t in range(m):
        discount = 1.0
        if use_discount:
            # Compute hours from now for this slot's midpoint
            slot = slots[future_idx[t]]
            slot_mid = slot.start + (slot.end - slot.start) / 2
            ha = hours_ahead(now, slot_mid)
            discount = time_discount_rate**ha

        # Conversion losses are already physical in the site balance:
        # charging draws ec/charge_eff AC and discharging delivers
        # ed*discharge_eff AC. The gi/ge money coefficients therefore price
        # those losses exactly once; no separate ec/ed loss coefficient applies.
        # Cycle cost through auxiliary variable m[t] (= max(ec, ed))
        c_obj[m_off + t] = cycle_cost_per_kwh * discount
        c_obj[gi_off + t] = p_imp_obj[t] * discount  # grid import cost
        c_obj[ge_off + t] = -p_exp[t] * discount  # export revenue (negative = gain)
        c_obj[battery_export_off + t] = _BATTERY_EXPORT_SOURCE_TIEBREAK
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
        # The differential is computed against the finite signed import
        # price (p_imp_obj).  The premium itself is floored at zero, so a
        # negative import price cannot inflate the terminal premium beyond
        # replacement_price_per_kwh.
        #
        # SECOND CAP (issue #694): the terminal premium must never make
        # battery charging more attractive than grid export for the same
        # slot.  Without this cap, a high replacement_price can cause the
        # LP to charge from solar during expensive hours instead of
        # exporting at peak prices — a "tunnel-vision" effect where the
        # LP rushes to satisfy the terminal-SoC target immediately rather
        # than deferring charging to cheaper slots with ample solar.
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
            _charge_premium = compute_charge_premium(
                replacement_price_per_kwh=replacement_price_per_kwh,
                imp_price_obj=p_imp_obj[t],
                exp_price=p_exp[t],
                charge_eff=charge_eff,
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
            #
            # No direct per-kWh benefit is placed on ev_c[t] (issue #797):
            # the slack penalty alone already prices meeting the deadline at
            # ev_penalty_cost per kWh shortfall, which is almost always far
            # above any real p_imp[t], so the LP already prefers charging
            # over paying the penalty without an additional coefficient.
            # Charging still pays its own real grid/PV opportunity cost.  A
            # tiny per-kWh tiebreak cost nudges the LP toward the smallest
            # executable (whole-amp) energy that clears the target-cap
            # constraint, rather than leaving it indifferent among
            # cost-equivalent solutions above the target.
            energy_needed = ev.effective_deadline_target_kwh - ev.initial_soc_kwh
            ev_penalty_cost = max(p_imp_max, 0.1) * max(energy_needed, 1.0) * 10.0
            if ev.deadline_escalated(m):
                ev_penalty_cost *= _EV_DEADLINE_ESCALATION_PENALTY_MULTIPLIER
            c_obj[ev_pen_offsets[ev_idx]] = ev_penalty_cost

            ev_off = ev_var_offsets[ev_idx]
            d = ev.deadline_slot
            d = max(0, min(d, m - 1))
            for t in range(d + 1):
                c_obj[ev_off + t] += _EV_TARGET_ENERGY_TIEBREAK_COST

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
    #
    # Battery-first priority (issue #775): the house battery must take its
    # share of a slot's PV surplus BEFORE a charge-past-target EV absorbs
    # any.  The EV's avoided-future-import value is a *speculative* benefit
    # (it assumes the EV will need that energy later), whereas the battery's
    # charge credit is a *concrete* one (the battery has a scheduled
    # discharge window).  Without a guard, the speculative EV value can
    # outrank the battery's charge credit and divert surplus the battery
    # needs — the EV and battery then oscillate for the same surplus across
    # replans.  To enforce battery-first, the EV's per-kWh benefit is capped
    # at the battery's charge-side cost (the magnitude of the battery's
    # charge credit, ``abs(c_obj[ec_off + t])``) whenever the battery has
    # headroom to absorb surplus.  This makes the battery weakly preferred
    # for the first ``max_charge_per_slot`` kWh of surplus (its absorption
    # limit); once the battery is saturated, the EV's full value applies to
    # the remaining surplus.  The shared surplus budget is enforced by the
    # battery-first constraint in ``_constraints.py``.
    _charge_eff_frac = charge_eff
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
                # Battery-first cap (issue #775): the EV's benefit is capped
                # at the battery's charge credit ONLY when the battery can
                # absorb the full slot surplus.  The battery's per-slot
                # absorption is ``min(max_charge_per_slot, usable_kwh -
                # current_kwh)``.  When that is >= the slot's PV surplus, the
                # battery takes it all and the EV should get nothing — so the
                # EV's (speculative) benefit is capped at the battery's
                # (concrete) charge credit.  When the battery cannot absorb
                # the full surplus (e.g. a tiny battery, or the battery is
                # nearly full), the EV keeps its full benefit for the
                # remainder the battery cannot take.
                slot_surplus = 0.0
                if pv_avail is not None and base_load is not None:
                    slot_surplus = max(float(pv_avail[t]) - float(base_load[t]), 0.0)
                battery_absorption = min(
                    max_charge_per_slot, max(usable_kwh - current_kwh, 0.0)
                )
                battery_takes_all = battery_absorption >= slot_surplus - 1e-9
                if battery_takes_all and slot_surplus > 1e-9:
                    # Magnitude of the battery's charge credit at this slot
                    # (c_obj[ec] is negative = a credit).  Capping the EV
                    # benefit at this value makes the battery weakly
                    # preferred for the surplus it can absorb.
                    #
                    # The cap must account for the AC-side efficiency
                    # difference between the battery and the EV.  The LP
                    # compares AC-side costs: the battery consumes
                    # ``1/charge_eff`` AC per 1 DC stored, while the EV
                    # consumes ``1/charger_eff`` AC per 1 DC.  When
                    # ``charge_eff < charger_eff`` (the common case), the
                    # battery's AC cost is higher, so equal coefficients
                    # still favour the EV.  Subtracting the efficiency
                    # difference (``p_imp_obj[t] * (1/charge_eff -
                    # 1/charger_eff)``) from the cap ensures the battery is
                    # weakly preferred for the surplus it can absorb.
                    battery_credit = abs(c_obj[ec_off + t])
                    eff_adj = p_imp_obj[t] * (
                        1.0 / _charge_eff_frac - 1.0 / ev.charger_efficiency
                    )
                    ev_value_t = min(ev_value, max(battery_credit - eff_adj, 0.0))
                else:
                    ev_value_t = ev_value
                # Negative coefficient = reduces objective = benefit.
                c_obj[ev_off + t] -= (ev_value_t / ev.charger_efficiency) * discount

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

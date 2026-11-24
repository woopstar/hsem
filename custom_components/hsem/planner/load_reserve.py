"""Pre-peak reserve for load-forecast uncertainty (issue #1361).

The MILP plans the house load as if the forecast were exact, so it charges
just enough for the next expensive window.  Being short at the end of that
window costs the peak price; carrying a kWh too much past it costs little.
With such an asymmetry the cost-minimising stock covers a quantile of the
demand, not its mean (the newsvendor rule).

This module finds the expensive windows ahead and resolves one
:class:`LoadReserveSpec` per window: the energy the battery should still hold
at the end of the window if the forecast is exact, cut into tiers.  A kWh of
reserve at ``x`` kWh above the forecast is used with probability
``P(error > x)``, so carrying it is worth ``Cu × P(error > x)`` per kWh: the
first tier is worth most and the last one least.  The MILP gets each window
as one soft row whose slack is priced per tier
(``planner/milp/_load_reserve_rows.py``), so it holds exactly the tiers that
cost less to carry than they are worth, whatever the energy comes from.  The
selector adds the same slack cost to every candidate's ``score`` through
``CostWeights.load_reserve``.  The house load in the energy balance is never
changed.

Opt-in (``PlannerInput.load_reserve_enabled``).  Off, :func:`resolve_load_reserve`
returns before reading anything.

Pure Python, no Home Assistant imports.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from statistics import NormalDist
from typing import Any

from custom_components.hsem.models.load_error_profile import window_spread_kwh
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.utils.datetime_utils import (
    future_slot_indices,
    utc_key,
)
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import clamp_efficiency
from custom_components.hsem.utils.soc_bounds import finite_or

#: Earlier days with a complete window needed before a reserve is applied.
LOAD_RESERVE_MIN_SAMPLES = 7
#: The reserve never exceeds this many standard deviations.
LOAD_RESERVE_MAX_Z = 2.0
#: Tiers the reserve is cut into, each ``MAX_Z / TIERS`` standard deviations.
LOAD_RESERVE_TIERS = 4
#: A reserve below this is not worth a row (kWh).
LOAD_RESERVE_MIN_KWH = 0.05
#: A shortfall must cost at least this share of the replacement cost ``R``
#: before a window counts as dearer than a charge before it.
LOAD_RESERVE_MIN_ASYMMETRY = 0.05
#: Hours at the end of the window whose cheapest price is the shortfall price.
LOAD_RESERVE_TAIL_HOURS = 2.0
#: Only a window that starts within this many hours gets a reserve.
LOAD_RESERVE_LOOKAHEAD_HOURS = 24.0
#: Most reserves (soft rows) of one solve.
LOAD_RESERVE_MAX_WINDOWS = 3
#: ``estimated_battery_capacity_kwh`` is rounded to 3 decimals.
SCORE_TOLERANCE_KWH = 1e-3
_PRICE_EPSILON = 1e-9

#: A row asks for a reserve.  It does not say the plan changed: energy the
#: plan carries past the window anyway counts towards the row.
REASON_APPLIED = "row_added"
REASON_NO_HISTORY = "no_history"
REASON_NO_PRICE_ASYMMETRY = "no_price_asymmetry"
REASON_INSUFFICIENT_HISTORY = "insufficient_history"
REASON_NO_UNCERTAINTY = "no_uncertainty"
REASON_CAPACITY_BOUND = "capacity_bound"
#: Reported when the selected plan holds none of an applied reserve.
REASON_SURPLUS_TOO_COSTLY = "surplus_too_costly"


@dataclass(frozen=True)
class LoadReserveSpec:
    """The reserve decision for one expensive window.

    Attributes:
        reason: Why the reserve has its value; ``row_added`` when one is
            asked for.
        sigma_kwh: Robust spread of the summed load forecast error over the
            window's hours on earlier days.
        samples: Earlier days behind ``sigma_kwh``.
        shortfall_cost: ``Cu``: cost of a missing AC kWh at the window's end,
            from the cheapest price of the window's last two hours.
        replacement_cost: ``R``: delivered cost of an AC kWh charged before
            the window.
        window_start: Start of the expensive window, or ``None``.
        window_end: End of the window; the reserve is measured here.
        window_slots: Slots in the window.
        floor_kwh: Model kWh the dynamic discharge floor asks at ``window_end``.
        tiers: ``(width_kwh, price_per_kwh)`` per tier, from the top of the
            reserve down: the first is the cheapest slack and is given up
            first.
    """

    reason: str
    sigma_kwh: float = 0.0
    samples: int = 0
    shortfall_cost: float = 0.0
    replacement_cost: float = 0.0
    window_start: datetime | None = None
    window_end: datetime | None = None
    window_slots: int = 0
    floor_kwh: float = 0.0
    tiers: tuple[tuple[float, float], ...] = ()

    @property
    def active(self) -> bool:
        """Return whether a reserve row and score term exist."""
        return self.reason == REASON_APPLIED

    @property
    def max_reserve_kwh(self) -> float:
        """Return the largest reserve the plan may hold for this window."""
        return math.fsum(width for width, _price in self.tiers)

    @property
    def level_kwh(self) -> float:
        """Return the model kWh the battery holds with the full reserve."""
        return self.floor_kwh + self.max_reserve_kwh

    def slack_cost(self, shortfall_kwh: float) -> float:
        """Return the cost of holding *shortfall_kwh* less than the level.

        Tiers are given up cheapest first; anything beyond the reserve is
        priced as the dearest tier, as in the LP.
        """
        left = max(shortfall_kwh, 0.0)
        cost = 0.0
        for width, price in self.tiers:
            used = min(left, width)
            cost += used * price
            left -= used
        return cost + left * (self.tiers[-1][1] if self.tiers else 0.0)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-safe diagnostics of this decision."""
        return {
            "reason": self.reason,
            "asked_kwh": round(self.max_reserve_kwh, 3),
            "sigma_kwh": round(self.sigma_kwh, 3),
            "samples": self.samples,
            "shortfall_cost_per_kwh": round(self.shortfall_cost, 4),
            "replacement_cost_per_kwh": round(self.replacement_cost, 4),
            "window_start": _iso(self.window_start),
            "window_end": _iso(self.window_end),
            "window_slots": self.window_slots,
            "level_kwh": round(self.level_kwh, 3),
        }


@dataclass(frozen=True)
class LoadReservePlan:
    """Every reserve of one planner run.

    Attributes:
        primary: The first reserve, or the decision that explains why there
            is none.
        windows: The applied reserves, in time order; each is one soft row.
    """

    primary: LoadReserveSpec
    windows: tuple[LoadReserveSpec, ...] = ()


def _iso(value: datetime | None) -> str | None:
    """Return an ISO string, or ``None``."""
    return value.isoformat() if value is not None else None


def delivered_cost_per_kwh(
    import_price: float, charge_eff: float, discharge_eff: float, cycle_cost: float
) -> float:
    """Return ``R``: the cost of one AC kWh delivered from a grid charge.

    ``1/η_d`` DC kWh leave the battery for it and ``1/(η_c·η_d)`` AC kWh are
    bought; cycle wear is paid on the charge and on the discharge.
    """
    return (import_price / charge_eff + 2.0 * max(cycle_cost, 0.0)) / discharge_eff


def window_after(
    prices: Sequence[float],
    charge_index: int,
    charge_eff: float,
    discharge_eff: float,
    cycle_cost: float,
) -> tuple[int, int, float] | None:
    """Return the expensive window that a charge in one slot would serve.

    The window opens at the first later slot whose price is above the
    delivered cost ``R`` of a charge at *charge_index*, and runs while the
    price stays above ``R``.  There is none when a cheaper slot comes before
    such a slot: that slot is then the place to charge.  A non-finite price
    is never a charge slot and ends a window.

    Args:
        prices: Import price per future slot, in LP order.
        charge_index: LP index of the candidate charge slot.
        charge_eff: Charge efficiency as a fraction.
        discharge_eff: Discharge efficiency as a fraction.
        cycle_cost: Cycle cost per kWh.

    Returns:
        ``(first, last, R)`` as LP indices, or ``None`` without a window.
    """
    charge_price = prices[charge_index]
    if not math.isfinite(charge_price):
        return None
    cost = delivered_cost_per_kwh(charge_price, charge_eff, discharge_eff, cycle_cost)
    first: int | None = None
    last = -1
    superseded = False
    for pos in range(charge_index + 1, len(prices)):
        price = prices[pos]
        dear = math.isfinite(price) and price > cost + _PRICE_EPSILON
        if first is None and not superseded:
            if dear:
                first = last = pos
            elif math.isfinite(price) and price < charge_price - _PRICE_EPSILON:
                superseded = True
        elif first is not None and last == pos - 1 and dear:
            last = pos
    return None if first is None else (first, last, cost)


def reserve_tiers(
    sigma_kwh: float, cap_kwh: float, shortfall_cost_per_stored_kwh: float
) -> tuple[tuple[float, float], ...]:
    """Cut a reserve of up to ``2σ`` into priced tiers, cheapest slack first.

    A kWh of reserve at ``z`` standard deviations above the forecast is
    used with probability ``1 − Φ(z)``.  Bought at ``a`` and worth ``v``
    when left over, it pays while ``a − v < (p − v) × (1 − Φ(z))``, with
    ``p`` the price a shortfall is bought at.  ``a − v`` is what carrying
    the kWh costs in the LP, whatever its source.  A leftover kWh is taken
    to be worth the replacement cost ``R`` at most, which gives the tier
    price ``Cu × (1 − Φ(z))`` with ``Cu = p − R``, at the tier's middle.

    Args:
        sigma_kwh: Spread of the window's summed forecast error.
        cap_kwh: Most the battery can hold on top of the forecast demand.
        shortfall_cost_per_stored_kwh: ``Cu`` per kWh in the battery.

    Returns:
        ``(width_kwh, price_per_kwh)`` per tier from the top down, without
        the part above *cap_kwh*.
    """
    normal = NormalDist()
    step = LOAD_RESERVE_MAX_Z / LOAD_RESERVE_TIERS * sigma_kwh
    tiers: list[tuple[float, float]] = []
    for index in range(LOAD_RESERVE_TIERS):
        low = index * step
        width = min(step, cap_kwh - low)
        if sigma_kwh > 0.0 and width > 0.0:
            used = 1.0 - normal.cdf((low + width / 2.0) / sigma_kwh)
            tiers.append((width, shortfall_cost_per_stored_kwh * used))
    return tuple(reversed(tiers))


def _price(slot: PlannedSlot) -> float:
    """Return the slot's import price, ``nan`` when it has none."""
    return finite_or(slot.price.import_price, math.nan)


def resolve_load_reserve(
    inp: PlannerInput,
    slots: Sequence[PlannedSlot],
    now: datetime,
    *,
    usable_kwh: float,
    cycle_cost_per_kwh: float,
    zone: tzinfo | None = None,
) -> LoadReservePlan | None:
    """Resolve the reserves for the expensive windows ahead of this run.

    Every future slot is tried as the place to charge.  A window that gets a
    reserve covers the slots inside it; a window that gets none (the battery
    cannot hold more, say) is searched for a shorter one inside it.  Only
    windows starting within ``LOAD_RESERVE_LOOKAHEAD_HOURS`` are used, and at
    most ``LOAD_RESERVE_MAX_WINDOWS`` reserves.

    Args:
        inp: Planner input; the switch, the error history and efficiencies.
        slots: Populated slots, before any candidate is built.
        now: Planning moment.
        usable_kwh: Model capacity above the hardware floor.
        cycle_cost_per_kwh: Effective cycle cost.
        zone: Time zone whose wall clock aligns a window with earlier days.

    Returns:
        ``None`` when the option is off.  Otherwise the plan, whose
        ``primary.reason`` says why no reserve is held when there is none.
    """
    if not inp.load_reserve_enabled:
        return None
    profile = inp.load_error_profile
    if profile is None or not profile.errors:
        return _logged(LoadReservePlan(LoadReserveSpec(reason=REASON_NO_HISTORY)))
    future = [slots[i] for i in future_slot_indices((s.end for s in slots), now)]
    charge_eff = clamp_efficiency(inp.battery_charge_efficiency_pct)
    discharge_eff = clamp_efficiency(inp.battery_discharge_efficiency_pct)
    prices = [_price(slot) for slot in future]
    latest_start = utc_key(now + timedelta(hours=LOAD_RESERVE_LOOKAHEAD_HOURS))
    applied: list[LoadReserveSpec] = []
    rejected: list[LoadReserveSpec] = []
    skip_before = 0
    for charge_index in range(len(prices)):
        if charge_index < skip_before or len(applied) >= LOAD_RESERVE_MAX_WINDOWS:
            continue
        found = window_after(
            prices, charge_index, charge_eff, discharge_eff, cycle_cost_per_kwh
        )
        if found is None or utc_key(future[found[0]].start) >= latest_start:
            continue
        spec = _evaluate(
            inp, future, prices, found, finite_or(usable_kwh, 0.0), discharge_eff, zone
        )
        if spec.active:
            applied.append(spec)
            skip_before = found[1] + 1
        else:
            rejected.append(spec)
            skip_before = found[0]
    primary = (applied or rejected or [LoadReserveSpec(REASON_NO_PRICE_ASYMMETRY)])[0]
    return _logged(LoadReservePlan(primary, tuple(applied)))


def _logged(plan: LoadReservePlan) -> LoadReservePlan:
    """Log the resolved plan and return it."""
    for spec in plan.windows or (plan.primary,):
        log_planner(
            "debug",
            "[load_reserve] reason=%s  window=%s..%s  n=%d  sigma=%.3f  "
            "samples=%d  Cu=%.4f  R=%.4f  max_reserve=%.3f  level=%.3f  tiers=%s",
            spec.reason,
            _iso(spec.window_start),
            _iso(spec.window_end),
            spec.window_slots,
            spec.sigma_kwh,
            spec.samples,
            spec.shortfall_cost,
            spec.replacement_cost,
            spec.max_reserve_kwh,
            spec.level_kwh,
            [(round(width, 3), round(price, 4)) for width, price in spec.tiers],
        )
    return plan


def _evaluate(
    inp: PlannerInput,
    future: Sequence[PlannedSlot],
    prices: Sequence[float],
    found: tuple[int, int, float],
    usable_kwh: float,
    discharge_eff: float,
    zone: tzinfo | None,
) -> LoadReserveSpec:
    """Return the reserve decision for one expensive window."""
    first, last, replacement = found
    window = future[first : last + 1]
    hours = max(inp.interval_minutes, 1) / 60.0
    tail_slots = max(math.ceil(LOAD_RESERVE_TAIL_HOURS / hours), 1)
    tail = prices[max(first, last + 1 - tail_slots) : last + 1]
    # A shortfall noticed late is bought in the cheapest slot still ahead,
    # so the cheapest tail price values it.  Withholding discharge in a
    # window slot at that price or above then costs more than any tier.
    shortfall = min(tail) - replacement
    fields: dict[str, Any] = {
        "shortfall_cost": shortfall,
        "replacement_cost": replacement,
        "window_start": window[0].start,
        "window_end": window[-1].end,
        "window_slots": len(window),
    }
    if shortfall < LOAD_RESERVE_MIN_ASYMMETRY * abs(replacement):
        return LoadReserveSpec(reason=REASON_NO_PRICE_ASYMMETRY, **fields)
    profile = inp.load_error_profile
    samples = (
        profile.window_samples([s.start for s in window], zone, inp.interval_minutes)
        if profile
        else []
    )
    fields["samples"] = len(samples)
    if len(samples) < LOAD_RESERVE_MIN_SAMPLES:
        return LoadReserveSpec(reason=REASON_INSUFFICIENT_HISTORY, **fields)
    sigma = window_spread_kwh(samples)
    fields["sigma_kwh"] = sigma
    if LOAD_RESERVE_MAX_Z * sigma < LOAD_RESERVE_MIN_KWH:
        return LoadReserveSpec(reason=REASON_NO_UNCERTAINTY, **fields)
    # The battery cannot hold more than its capacity at the window's start:
    # the forecast demand of the window comes off the room for a reserve.
    demand = sum(
        max(s.avg_house_consumption_kwh - s.solcast_pv_estimate_kwh, 0.0)
        for s in window
    )
    floor = max(finite_or(window[-1].discharge_reserve_kwh, 0.0), 0.0)
    cap = usable_kwh - floor - demand / discharge_eff
    if cap < LOAD_RESERVE_MIN_KWH:
        return LoadReserveSpec(reason=REASON_CAPACITY_BOUND, **fields)
    return LoadReserveSpec(
        reason=REASON_APPLIED,
        floor_kwh=floor,
        tiers=reserve_tiers(sigma, cap, shortfall * discharge_eff),
        **fields,
    )


def reserve_slot(
    slots: Sequence[PlannedSlot], spec: LoadReserveSpec
) -> PlannedSlot | None:
    """Return the slot the reserve is measured at the end of, or ``None``."""
    if spec.window_end is None:
        return None
    key = utc_key(spec.window_end)
    return next((s for s in slots if utc_key(s.end) == key), None)


def reserve_shortfall_kwh(slots: Sequence[PlannedSlot], spec: LoadReserveSpec) -> float:
    """Return how far a simulated plan is below one reserve level, in kWh."""
    slot = reserve_slot(slots, spec) if spec.active else None
    if slot is None:
        return 0.0
    held = finite_or(slot.estimated_battery_capacity_kwh, 0.0)
    shortfall = max(spec.level_kwh - held, 0.0)
    return shortfall if shortfall >= SCORE_TOLERANCE_KWH else 0.0


def load_reserve_penalty(
    slots: Sequence[PlannedSlot], plan: LoadReservePlan | None
) -> float:
    """Return the slack cost of every reserve for a simulated plan.

    Selector-only and undiscounted, like the MILP slack it mirrors: added to
    ``score``, never to ``total_cost``.
    """
    if plan is None:
        return 0.0
    return math.fsum(
        spec.slack_cost(reserve_shortfall_kwh(slots, spec)) for spec in plan.windows
    )


def _window_summary(
    spec: LoadReserveSpec, winner_slots: Sequence[PlannedSlot]
) -> dict[str, Any]:
    """Return one window's diagnostics with what the selected plan holds."""
    entry = spec.as_dict()
    slack = min(reserve_shortfall_kwh(winner_slots, spec), spec.max_reserve_kwh)
    stock = spec.max_reserve_kwh - slack
    entry["stock_at_window_end_kwh"] = round(stock, 3)
    entry["slack_kwh"] = round(slack, 3)
    entry["safety_factor"] = (
        round(stock / spec.sigma_kwh, 3) if spec.sigma_kwh > 0.0 else 0.0
    )
    if spec.active and stock < LOAD_RESERVE_MIN_KWH:
        entry["reason"] = REASON_SURPLUS_TOO_COSTLY
    return entry


def summarize_load_reserve(
    plan: LoadReservePlan | None, winner_slots: Sequence[PlannedSlot]
) -> dict[str, Any] | None:
    """Return the reserve diagnostics published with the selected plan.

    ``None`` when the option is off.  The top level describes the first
    reserve the plan holds, else the first one it was asked for, else the
    first window that got none; ``windows`` lists every reserve asked for.
    ``asked_kwh`` is what the row asks for and ``slack_kwh`` the part the
    selected plan does not hold.  ``stock_at_window_end_kwh`` is the rest:
    what the plan holds above the floor at the window's end, up to
    ``asked_kwh``.  It includes energy the plan would carry past the window
    without the row, so it is not what the row added.  ``safety_factor`` is
    that stock in standard deviations.
    """
    if plan is None:
        return None
    windows = [_window_summary(spec, winner_slots) for spec in plan.windows]
    for entry in windows:
        log_planner(
            "debug",
            "[milp] load_reserve window=%s..%s  n=%d  sigma=%.3f  z=%.3f  "
            "asked=%.3f  stock=%.3f  slack=%.3f  reason=%s",
            entry["window_start"],
            entry["window_end"],
            entry["window_slots"],
            entry["sigma_kwh"],
            entry["safety_factor"],
            entry["asked_kwh"],
            entry["stock_at_window_end_kwh"],
            entry["slack_kwh"],
            entry["reason"],
        )
    held = [entry for entry in windows if entry["reason"] == REASON_APPLIED]
    summary = dict((held or windows or [_window_summary(plan.primary, ())])[0])
    summary["total_stock_kwh"] = round(
        math.fsum(entry["stock_at_window_end_kwh"] for entry in windows), 3
    )
    summary["windows"] = windows
    return summary

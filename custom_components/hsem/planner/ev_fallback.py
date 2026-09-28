"""EV-only smart-charging fallback while the house-load forecast is not ready.

When the house-load forecast is not ready the coordinator skips the planner
and publishes a strict storage hold (``coordinator_load_hold.py``). Before
issue #1106 that also meant a managed EV on smart charging could not charge
at all until every average-sensor hour block had been re-observed, which can
take more than 24 h after a restore-state loss.

This module builds an EV-only plan from inputs that do not depend on the
house forecast: EV SoC/target/deadline, charger ratings, and per-slot import
prices. The house load is unknown, so no slot is credited with PV surplus
(``slot_net_surplus_kwh = 0`` for every slot) and the EV charges only in the
cheapest import slots before its deadline. That never assumes free solar an
unknown house load may already be using.

The home battery is not planned here and stays strictly held. Everything in
this module is pure: no Home Assistant types, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from custom_components.hsem.planner.engine_ev import _compute_ev_charger_power
from custom_components.hsem.planner.ev_planner import (
    build_ev_charging_plan,
    rebuild_ev_plan_from_slots,
)
from custom_components.hsem.planner.ev_planner_models import (
    EVChargingPlan,
    EVPlannerInput,
)
from custom_components.hsem.utils.datetime_utils import utc_key
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import clamp_efficiency

#: ``EVChargingPlan.data_quality["mode"]`` marker for a fallback plan.
EV_ONLY_FALLBACK_MODE = "ev_only_fallback"


@dataclass(frozen=True)
class EvFallbackSlot:
    """One horizon slot as seen by the EV-only fallback."""

    start: datetime
    end: datetime
    import_price: float


@dataclass
class _CommandSlot:
    """Scratch slot for :func:`_compute_ev_charger_power`."""

    start: datetime
    end: datetime
    ev_charger_calculated_power: float = 0.0
    ev_second_charger_calculated_power: float = 0.0


@dataclass(frozen=True)
class EvFallbackResult:
    """EV-only fallback plan plus the per-slot AC commands it implies."""

    plan: EVChargingPlan
    slot_commands_w: list[float]


def estimate_unpriced_tail(slots: list[EvFallbackSlot]) -> list[float]:
    """Return per-slot import prices with the unpublished tail estimated.

    Coordinator slots carry no "price missing" flag: a slot the price sensor
    has not published yet (typically tomorrow before the day-ahead auction)
    simply reads ``0.0``. A cheapest-first EV planner would treat those as
    free energy. Following the canonical missing-price rule (issue #1002),
    every slot after the last non-zero price takes the same local-time price
    from the nearest earlier day. Without such a day it takes the highest
    known price, so an unpriced slot is never preferred as free. Genuine
    zero prices inside the published range are kept.
    """
    prices = [float(s.import_price) for s in slots]
    last_priced = max(
        (i for i, price in enumerate(prices) if abs(price) > 1e-9), default=-1
    )
    if last_priced < 0:
        return prices
    known_max = max(prices[: last_priced + 1])
    by_clock: dict[tuple[int, int], float] = {}
    for i, slot in enumerate(slots):
        clock = (slot.start.hour, slot.start.minute)
        if i <= last_priced:
            by_clock[clock] = prices[i]
        else:
            prices[i] = by_clock.get(clock, known_max)
    return prices


def build_ev_only_fallback_plan(
    inp: EVPlannerInput,
    slots: list[EvFallbackSlot],
    *,
    interval_minutes: int,
    load_forecast_reason: str,
    is_second: bool = False,
) -> EvFallbackResult:
    """Return a grid-only EV plan and its per-slot AC commands in Watts.

    Delegates slot selection to :func:`build_ev_charging_plan` with zero PV
    surplus in every slot, so its guard states still apply unchanged:
    feature off / not connected / smart charging off return
    ``smart_charging_disabled`` or ``not_connected``, an unknown SoC returns
    ``unavailable`` (issue #988), and an EV at or above target returns
    ``fully_charged``. All of those produce all-zero commands.

    Unpublished trailing prices are estimated by
    :func:`estimate_unpriced_tail` so they are never selected as free.

    Commands are derived with the planner's own conversion
    (:func:`~planner.engine_ev._compute_ev_charger_power`): AC energy over
    the time left in the current slot (full width for future slots), capped
    at the charger rating and floored to zero below the charger minimum.

    Args:
        inp: EV planner input for one EV.
        slots: Horizon slots with import prices, chronologically ordered.
        interval_minutes: Slot width in minutes.
        load_forecast_reason: Machine-readable load-forecast rejection
            reason, surfaced in the plan's ``data_quality``.
        is_second: Whether this is the second EV (selects the power field).

    Returns:
        The fallback plan and one command (W, >= 0) per input slot.
    """
    prices = estimate_unpriced_tail(slots)
    plan = build_ev_charging_plan(
        inp,
        slots_start=[s.start for s in slots],
        slots_end=[s.end for s in slots],
        slot_net_surplus_kwh=[0.0] * len(slots),
        slot_import_price=prices,
    )
    plan.data_quality = {
        **plan.data_quality,
        "mode": EV_ONLY_FALLBACK_MODE,
        "load_forecast": load_forecast_reason,
    }

    scratch = [_CommandSlot(start=s.start, end=s.end) for s in slots]
    _compute_ev_charger_power(
        scratch,
        [s.start for s in slots],
        plan,
        interval_minutes,
        inp.now,
        second=is_second,
    )
    attr = (
        "ev_second_charger_calculated_power"
        if is_second
        else "ev_charger_calculated_power"
    )
    commands = [max(float(getattr(s, attr)), 0.0) for s in scratch]

    log_planner(
        "debug",
        "[ev_fallback] %s  state=%s  slots=%d  needed=%.3fkWh  "
        "commanded_slots=%d  reason=%s",
        "EV2" if is_second else "EV",
        plan.state,
        len(plan.charging_slots),
        plan.total_kwh_needed,
        sum(1 for c in commands if c > 1e-9),
        load_forecast_reason,
    )
    return EvFallbackResult(plan=plan, slot_commands_w=commands)


def finalize_fallback_plan(
    plan: EVChargingPlan,
    slots: list,
    now: datetime,
    charger_efficiency_pct: float,
    prices_by_start: dict[datetime, float],
    *,
    is_second: bool = False,
) -> EVChargingPlan:
    """Rebuild a fallback plan from the slots' published EV commands.

    The published commands can differ from the fallback's own allocation
    after force-charge-now, the fuse clamp, and command stability, and the EV
    plan sensor must describe what HSEM actually commands (spec invariant
    13). :func:`rebuild_ev_plan_from_slots` credits PV surplus against the
    slot's house load, but the house load is exactly what is unknown here,
    so every slot is re-priced as grid import (the fallback's grid-only
    assumption) at the estimated import price.

    Args:
        plan: The fallback plan (metadata source).
        slots: Recommendation slots carrying the published EV commands.
        now: Current time (timezone-aware).
        charger_efficiency_pct: Charger efficiency for AC to DC conversion.
        prices_by_start: Estimated import price keyed by UTC slot start.
        is_second: Whether this is the second EV.

    Returns:
        A new plan whose charging slots match the published commands.
    """
    rebuilt = rebuild_ev_plan_from_slots(
        plan,
        slots,
        now,
        charger_efficiency_pct,
        is_second=is_second,
    )
    eff = clamp_efficiency(charger_efficiency_pct)
    rebuilt.charging_slots = [
        replace(
            ev_slot,
            solar_surplus_kwh=0.0,
            import_needed_kwh=ev_slot.estimated_charged_kwh,
            import_price=prices_by_start.get(
                utc_key(ev_slot.start), ev_slot.import_price
            ),
            estimated_cost=round(
                (ev_slot.estimated_charged_kwh / eff)
                * prices_by_start.get(utc_key(ev_slot.start), ev_slot.import_price),
                4,
            ),
        )
        for ev_slot in rebuilt.charging_slots
    ]
    return rebuilt

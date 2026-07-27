"""Arbitrage grid charging."""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.battery_schedule_input import BatteryScheduleInput
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.charging._charge_helpers import (
    _already_planned_charge_kwh,
)
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Arbitrage grid charging
# ---------------------------------------------------------------------------


def apply_arbitrage_grid_charge(
    slots: list[PlannedSlot],
    battery_schedules: list[BatteryScheduleInput],
    now: datetime,
    current_capacity: float,
    usable_capacity: float,
    max_charge_per_interval: float,
    conversion_loss_pct: float,
    cycle_cost_per_kwh: float = 0.0,
    recommended_threshold: float = 0.0,
) -> None:
    """Charge from the grid when a future expensive import slot can be offset.

    This pass runs *after* the scheduled and opportunistic charge passes and
    *before* the seasonal fallback.  It exists to capture price arbitrage
    even when no discharge schedule is configured: HSEM's scheduled and
    opportunistic passes only react to discharge windows or a fixed
    depreciation threshold, so a clear cheap-now vs. expensive-later spread
    (e.g. 0.66 DKK/kWh at noon vs. 1.68 DKK/kWh at 18:00) would otherwise
    never trigger grid charging.

    Algorithm:

    1. Skip if remaining capacity ≤ 0 or no enabled battery schedule exists
       (the user has effectively disabled grid charging by leaving every
       schedule disabled).
    2. Build a list of future expensive consumption slots — slots with
       ``estimated_net_consumption > 0`` and no charge/discharge
       recommendation yet (i.e. those that would otherwise import from the
       grid).  These represent kWh that the battery can avoid importing
       later.
    3. Build a list of unassigned future charge candidates, sorted cheapest
       first.
    4. Walk charge candidates from cheapest up.  For each candidate, attempt
       to "pair" each kWh of charge with the most expensive future
       unmatched-import-kWh that occurs *after* the candidate slot.  A pair
       is profitable when::

           expensive.import_price - cheap.import_price
               >= min_price_difference + cycle_cost_per_kwh
                   + conversion_loss_cost_per_kwh

       where ``min_price_difference`` is the smallest enabled schedule's
       value (or the depreciation-derived ``recommended_threshold`` when no
       enabled schedule has set one), and conversion-loss cost is the cost
       of the conversion loss applied to each charged kWh.

    Args:
        slots: Mutable list of planned slots.
        battery_schedules: Schedule configurations.  At least one must be
            enabled for arbitrage charging to be active.
        now: Timezone-aware current datetime.
        current_capacity: Current available battery energy in kWh.
        usable_capacity: Maximum usable battery energy in kWh.
        max_charge_per_interval: Maximum energy (kWh) chargeable per slot.
        conversion_loss_pct: Round-trip conversion loss as a percentage
            (0-100).  Used to estimate the per-kWh loss cost that the
            future-vs-current price spread must cover.
        cycle_cost_per_kwh: Additional per-kWh battery wear cost (≥ 0).
        recommended_threshold: Depreciation + conversion-loss threshold.
            Used as a fallback minimum price-difference when no enabled
            schedule has supplied a non-zero ``min_price_difference``.
            Defaults to 0.0.
    """
    log_planner(
        "debug",
        "[chg] apply_arbitrage_grid_charge  current=%.3f  usable=%.3f  "
        "max_charge/slot=%.3f  conv_loss_pct=%.2f  cycle_cost=%.4f  threshold=%.4f",
        current_capacity,
        usable_capacity,
        max_charge_per_interval,
        conversion_loss_pct,
        cycle_cost_per_kwh,
        recommended_threshold,
    )
    if max_charge_per_interval <= 0:
        log_planner("debug", "arbitrage: max_charge_per_interval <= 0, skipping")
        return

    enabled = [s for s in battery_schedules if s.enabled]
    if not enabled:
        log_planner(
            "debug", "arbitrage: no enabled battery schedule — grid charge disabled"
        )
        return

    # Use usable_capacity as the budget rather than usable_capacity -
    # current_capacity, so arbitrage charging is evaluated even when the
    # battery starts full.  The battery will discharge between now and
    # the expensive consumption slots tomorrow, making room for charging.
    # The SoC simulation handles per-slot physical clamping.
    remaining_capacity = usable_capacity
    already_planned = _already_planned_charge_kwh(slots)
    remaining_capacity = max(remaining_capacity - already_planned, 0.0)
    if remaining_capacity <= 1e-9:
        log_planner(
            "debug",
            "arbitrage: battery effectively full (remaining=%.3f, already_planned=%.3f)",
            remaining_capacity,
            already_planned,
        )
        return

    # Pick the smallest enabled schedule's recommended_threshold as the guard floor.
    # All schedules fall back to the depreciation-derived recommended_threshold.
    sched_min_diff = recommended_threshold

    # Conversion loss cost per kWh charged: a rough lower bound — charging
    # one stored kWh requires (1 / (1 - loss)) kWh of grid energy, so each
    # kWh costs (1/(1-loss) - 1) * charge_price extra.  We approximate it
    # against the cheap-slot price so the comparison stays simple.
    loss_factor = max(conversion_loss_pct / 100.0, 0.0)

    # Collect future expensive consumption slots (will import unless offset).
    # These are slots with positive net consumption that have not been
    # assigned a recommendation yet — they represent grid-import kWh that
    # arbitrage can avoid.
    expensive_slots: list[tuple[PlannedSlot, float]] = []
    for s in slots:
        if as_tz(s.end, now.tzinfo) <= now:
            continue
        if s.recommendation is not None:
            continue
        if s.estimated_net_consumption_kwh <= 0:
            continue
        expensive_slots.append((s, float(s.estimated_net_consumption_kwh)))

    if not expensive_slots:
        log_planner(
            "debug",
            "arbitrage: no future positive-net-consumption slots — nothing to offset",
        )
        return

    # Collect cheap charge candidates, sorted by price then by start.
    candidates = sorted(
        (
            s
            for s in slots
            if as_tz(s.end, now.tzinfo) > now and s.recommendation is None
        ),
        key=lambda x: (x.price.import_price, x.start),
    )
    if not candidates:
        log_planner("debug", "arbitrage: no unassigned future slots")
        return

    # Track per-expensive-slot remaining unmatched import demand (kWh).
    # We mutate a parallel dict so we can deduct as we match.
    remaining_demand: dict[int, float] = {
        id(es): demand for es, demand in expensive_slots
    }

    charged_total = 0.0
    chosen_any = False

    for cand in candidates:
        if charged_total >= remaining_capacity - 1e-9:
            break

        cand_start_local = as_tz(cand.start, now.tzinfo)
        cand_price = cand.price.import_price

        # Per-kWh conversion-loss cost approximated against this candidate's
        # price.  Negative prices flip the sign; clamp at 0 to avoid making
        # the guard easier to pass when the grid pays us.
        loss_cost_per_kwh = max(cand_price, 0.0) * (
            loss_factor / (1.0 - loss_factor) if loss_factor < 1.0 else 0.0
        )
        min_required_spread = sched_min_diff + cycle_cost_per_kwh + loss_cost_per_kwh

        # Find future expensive slots strictly after this candidate, sorted
        # most-expensive first, with remaining unmatched demand.
        future_expensive = sorted(
            (
                es
                for es, _ in expensive_slots
                if as_tz(es.start, now.tzinfo)
                >= cand_start_local + (cand.end - cand.start)
                and remaining_demand.get(id(es), 0.0) > 1e-9
            ),
            key=lambda x: (-x.price.import_price, x.start),
        )

        slot_room = min(max_charge_per_interval, remaining_capacity - charged_total)
        slot_charged = 0.0

        for es in future_expensive:
            if slot_charged >= slot_room - 1e-9:
                break
            spread = es.price.import_price - cand_price
            if spread < min_required_spread:
                # future_expensive is sorted highest-price first; if the
                # most expensive remaining future slot does not clear the
                # required spread, no later (cheaper) one can either.
                break
            available_demand = remaining_demand.get(id(es), 0.0)
            energy = min(slot_room - slot_charged, available_demand)
            if energy <= 1e-9:
                continue
            remaining_demand[id(es)] = available_demand - energy
            slot_charged += energy
            log_planner(
                "debug",
                "arbitrage: pairing %.3f kWh at %s (price=%.4f) -> %s "
                "(price=%.4f, spread=%.4f, required=%.4f)",
                energy,
                cand.start.isoformat(),
                cand_price,
                es.start.isoformat(),
                es.price.import_price,
                spread,
                min_required_spread,
            )

        if slot_charged > 1e-9:
            cand.recommendation = Recommendations.BatteriesChargeGrid.value
            cand.batteries_charged_kwh = round(slot_charged, 3)
            charged_total += slot_charged
            chosen_any = True
        else:
            log_planner(
                "debug",
                "arbitrage: no profitable future slot for candidate at %s "
                "(price=%.4f, required spread=%.4f)",
                cand.start.isoformat(),
                cand_price,
                min_required_spread,
            )

    if chosen_any:
        # Mark matched expensive slots for discharge so the battery
        # actually covers them during the SoC simulation instead of
        # importing from grid.
        discharged_count = 0
        for es, original_demand in expensive_slots:
            matched_kwh = original_demand - remaining_demand.get(
                id(es), original_demand
            )
            if matched_kwh > 1e-9:
                es.recommendation = Recommendations.ForceBatteriesDischarge.value
                discharged_count += 1
        if discharged_count > 0:
            log_planner(
                "debug",
                "arbitrage: marked %d expensive slot(s) for force discharge",
                discharged_count,
            )

        log_planner(
            "debug",
            "arbitrage: total %.3f kWh of grid charging scheduled "
            "(remaining_capacity=%.3f, sched_min_diff=%.4f, "
            "cycle_cost=%.4f, conversion_loss_pct=%.2f)",
            charged_total,
            remaining_capacity,
            sched_min_diff,
            cycle_cost_per_kwh,
            conversion_loss_pct,
        )
    else:
        log_planner(
            "debug",
            "arbitrage: no slot scheduled — price spread did not cover "
            "min_price_difference(%.4f) + cycle_cost(%.4f) + conversion loss",
            sched_min_diff,
            cycle_cost_per_kwh,
        )

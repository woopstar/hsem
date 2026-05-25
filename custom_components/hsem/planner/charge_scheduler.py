"""Battery charge scheduling for the HSEM planner.

Single responsibility: decide *when* to charge the battery
based on discharge-window schedules, price signals, and arbitrage.

All functions are pure — no I/O, no Home Assistant imports.  They mutate the
:class:`PlannedSlot` list passed in and return nothing (or a scalar result).
"""

from __future__ import annotations

import logging
from datetime import datetime

from custom_components.hsem.const import SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH
from custom_components.hsem.models.planner_inputs import BatteryScheduleInput
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import next_window_start_dt
from custom_components.hsem.utils.recommendations import CHARGE_RECS as _CHARGE_RECS
from custom_components.hsem.utils.recommendations import (
    DISCHARGE_RECS as _DISCHARGE_RECS,
)
from custom_components.hsem.utils.recommendations import Recommendations

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Charge scheduling
# ---------------------------------------------------------------------------


def apply_charge_schedules(
    slots: list[PlannedSlot],
    battery_schedules: list[BatteryScheduleInput],
    now: datetime,
    max_charge_per_interval: float,
    *,
    current_kwh: float = 0.0,
    usable_kwh: float = 0.0,
    cycle_cost_per_kwh: float = 0.0,
    recommended_threshold: float = 0.0,
) -> None:
    """Assign charge recommendations to slots before each discharge window.

    Three-priority ordering:

    1. Negative import price (free/paid-to-charge)
    2. Solar surplus (``estimated_net_consumption < SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH``)
    3. Cheapest remaining grid hours (guarded by depreciation threshold + cycle cost)

    A cross-occurrence battery capacity limit is enforced: once the total
    planned charge across all discharge-window occurrences reaches the
    battery's remaining capacity (``usable_kwh - current_kwh``), no further
    charge slots are assigned.  This prevents the scheduler from marking
    dozens of slots as ``batteries_charge_grid`` when the battery can only
    hold 14 kWh.

    Args:
        slots: Mutable list of planned slots.
        battery_schedules: Schedule configurations.
        now: Timezone-aware current datetime.
        max_charge_per_interval: Maximum energy (kWh) chargeable per slot.
        current_kwh: Current battery energy above the discharge floor (kWh).
            Used to cap total charge across all occurrences.
        usable_kwh: Maximum usable battery capacity (kWh).  Used together
            with *current_kwh* to derive remaining capacity.
        cycle_cost_per_kwh: Additional per-kWh cycle wear cost.
        recommended_threshold: Depreciation-derived price floor passed to
            ``_apply_grid_charge`` to guard profitability.
    """
    if max_charge_per_interval <= 0:
        log_planner(
            "debug",
            "[chg] apply_charge_schedules  skipped — max_charge_per_interval <= 0",
        )
        return

    # Cross-occurrence capacity cap: only enforce when usable_kwh is
    # provided (> 0).  When both current_kwh and usable_kwh are 0.0
    # (backward-compatible default), no cap is applied.
    remaining_capacity = (
        max(usable_kwh - current_kwh, 0.0) if usable_kwh > 0 else float("inf")
    )
    log_planner(
        "debug",
        "[chg] apply_charge_schedules  schedules=%d  remaining_cap=%.3f  "
        "max_charge/slot=%.3f  current=%.3f  usable=%.3f",
        len(battery_schedules),
        remaining_capacity,
        max_charge_per_interval,
        current_kwh,
        usable_kwh,
    )
    total_charged = 0.0

    for sched in battery_schedules:
        if not sched.enabled:
            continue

        # Iterate each occurrence of the discharge window independently.
        # Each occurrence needs its own pre-charge budget so day-2 discharge
        # windows get their own cheap-hours charge allocation.
        occurrences: list[tuple[datetime, datetime, float, float]] = getattr(
            sched, "_occurrences", []
        )
        if not occurrences:
            # Fallback for callers that didn't go through apply_discharge_schedules
            needed_fb: float = getattr(sched, "_needed_capacity", 0.0)
            avg_price_fb: float = getattr(sched, "_avg_import_price", 0.0)
            if needed_fb > 0:
                occurrences = [
                    (
                        next_window_start_dt(now, sched.start),
                        next_window_start_dt(now, sched.start),
                        needed_fb,
                        avg_price_fb,
                    )
                ]

        for (
            window_start_abs,
            _window_end_abs,
            needed,
            avg_discharge_price,
        ) in occurrences:
            if needed <= 0:
                continue

            # Cross-occurrence capacity cap: don't charge more than the
            # battery can hold across all discharge-window occurrences.
            if total_charged >= remaining_capacity - 1e-9:
                break
            occurrence_budget = min(needed, remaining_capacity - total_charged)

            # Eligible charge slots: future, unassigned, and ending before
            # this specific occurrence's window start.
            eligible = [
                s
                for s in slots
                if as_tz(s.end, now.tzinfo) > now
                and as_tz(s.end, now.tzinfo) <= window_start_abs
                and s.recommendation is None
            ]

            charged = 0.0

            # Priority 1: negative import price
            for s in sorted(
                (e for e in eligible if e.price.import_price < 0.0),
                key=lambda x: (x.price.import_price, x.start),
            ):
                if charged >= occurrence_budget:
                    break
                energy = min(max_charge_per_interval, occurrence_budget - charged)
                if energy > 0:
                    s.recommendation = Recommendations.BatteriesChargeGrid.value
                    s.batteries_charged_kwh = round(energy, 3)
                    charged += energy

            # Priority 2: solar surplus
            if charged < occurrence_budget:
                for s in sorted(
                    (
                        e
                        for e in eligible
                        if e.estimated_net_consumption_kwh
                        < SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH
                        and e.recommendation is None
                    ),
                    # NOTE: SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH is negative, so this
                    # selects slots where net consumption is sufficiently negative
                    # (i.e., there is a meaningful solar surplus to charge from).
                    key=lambda x: (x.estimated_net_consumption_kwh, x.start),
                ):
                    if charged >= occurrence_budget:
                        break
                    available_solar = abs(s.estimated_net_consumption_kwh)
                    energy = min(
                        max_charge_per_interval,
                        occurrence_budget - charged,
                        available_solar,
                    )
                    if energy > 0:
                        s.recommendation = Recommendations.BatteriesChargeSolar.value
                        s.batteries_charged_kwh = round(energy, 3)
                        charged += energy

            # Priority 3: cheapest grid hours (depreciation threshold + cycle cost guard)
            if charged < occurrence_budget:
                grid_charged = _apply_grid_charge(
                    eligible,
                    sched,
                    occurrence_budget,
                    charged,
                    max_charge_per_interval,
                    avg_discharge_price,
                    cycle_cost_per_kwh=cycle_cost_per_kwh,
                    recommended_threshold=recommended_threshold,
                )
                charged += grid_charged

            total_charged += charged


def _apply_grid_charge(
    eligible: list[PlannedSlot],
    sched: BatteryScheduleInput,
    needed: float,
    charged_so_far: float,
    max_charge_per_interval: float,
    avg_discharge_price: float,
    cycle_cost_per_kwh: float = 0.0,
    recommended_threshold: float = 0.0,
) -> float:
    """Apply cheapest-grid-hour charging with depreciation + cycle-cost guard.

    The combined profitability condition is:

        avg_discharge_price − avg_charge_price ≥ recommended_threshold + cycle_cost_per_kwh

    where ``recommended_threshold`` is the depreciation-derived price floor.

    Args:
        eligible: Pre-filtered candidate slots.
        sched: The battery schedule being filled.
        needed: Total energy to charge in kWh.
        charged_so_far: Energy already charged by higher-priority sources.
        max_charge_per_interval: Maximum energy per slot in kWh.
        avg_discharge_price: Average import price during the discharge window.
        cycle_cost_per_kwh: Per-kWh battery wear cost added to the guard.
            Defaults to 0.0 (backwards compatible).
        recommended_threshold: Depreciation + loss derived price floor.

    Returns:
        The total energy (kWh) assigned to grid charging by this call.
    """
    grid_candidates = sorted(
        (e for e in eligible if e.recommendation is None),
        key=lambda x: (x.price.import_price, x.start),
    )

    # First pass: estimate average charge price
    tentative_charged = 0.0
    tentative_count = 0
    tentative_price_sum = 0.0
    for s in grid_candidates:
        if tentative_charged >= needed - charged_so_far:
            break
        available_solar = (
            abs(s.estimated_net_consumption_kwh)
            if s.estimated_net_consumption_kwh < 0
            else 0
        )
        grid_needed = min(
            max_charge_per_interval - available_solar,
            needed - charged_so_far - tentative_charged - available_solar,
        )
        energy = available_solar + grid_needed
        if energy > 0:
            tentative_count += 1
            tentative_price_sum += s.price.import_price
            tentative_charged += energy

    avg_charge_price = (
        tentative_price_sum / tentative_count if tentative_count > 0 else 0.0
    )
    price_diff = avg_discharge_price - avg_charge_price
    # Combined threshold: depreciation-derived price floor + per-kWh wear cost.
    # Both must be covered by the price spread for grid charging to be profitable.
    min_diff = recommended_threshold + cycle_cost_per_kwh

    if abs(min_diff) > 1e-9 and price_diff < min_diff:
        return 0.0  # Price spread does not cover loss + cycle wear cost

    # Second pass: actually assign recommendations
    charged = charged_so_far
    grid_assigned = 0.0
    for s in grid_candidates:
        if charged >= needed:
            break
        available_solar = (
            abs(s.estimated_net_consumption_kwh)
            if s.estimated_net_consumption_kwh < 0
            else 0
        )
        grid_needed = min(
            max_charge_per_interval - available_solar,
            needed - charged - available_solar,
        )
        energy = available_solar + grid_needed
        if energy > 0:
            s.recommendation = Recommendations.BatteriesChargeGrid.value
            s.batteries_charged_kwh = round(energy, 3)
            charged += energy
            grid_assigned += energy

    return grid_assigned


# ---------------------------------------------------------------------------
# Helper: sum of already-planned charge energy
# ---------------------------------------------------------------------------


def _already_planned_charge_kwh(slots: list[PlannedSlot]) -> float:
    """Return the sum of ``batteries_charged_kwh`` across all charge-type slots.

    Used by downstream charge passes (opportunistic, arbitrage) to avoid
    exceeding the battery's remaining capacity when ``apply_charge_schedules``
    has already assigned energy.

    Args:
        slots: The mutable slot list to scan.

    Returns:
        Total kWh of charge energy already planned.
    """
    return sum(
        s.batteries_charged_kwh for s in slots if s.recommendation in _CHARGE_RECS
    )


# ---------------------------------------------------------------------------
# Opportunistic grid charging (A2/H28/H29)
# ---------------------------------------------------------------------------


def apply_opportunistic_charge(
    slots: list[PlannedSlot],
    now: datetime,
    current_capacity: float,
    usable_capacity: float,
    max_charge_per_interval: float,
    depreciation_threshold: float,
    cycle_cost_per_kwh: float = 0.0,
) -> None:
    """Charge the battery opportunistically when import prices are very low.

    This is a *schedule-independent* charge pass: it runs even when no
    discharge window is configured.  It covers two cases:

    1. **Negative import price** — the grid pays the consumer.  Every
       negative-price future slot is eligible regardless of the battery level.
    2. **Below-(depreciation − cycle cost) import price** — import is cheap
       enough that charging is economically sound.  The effective ceiling is
       ``max(depreciation_threshold − cycle_cost_per_kwh, 0)`` so that battery
       wear *reduces* the eligible price window — the planner only charges
       opportunistically when the price is low enough to cover both
       depreciation and cycle wear.

    Slots already assigned (by schedule pre-charge or prior passes) are
    skipped.  Energy is limited to what the battery can still absorb.

    Args:
        slots: Mutable list of planned slots (modified in-place).
        now: Timezone-aware current datetime.
        current_capacity: Current available battery energy in kWh.
        usable_capacity: Maximum usable battery energy in kWh.
        max_charge_per_interval: Maximum energy chargeable per slot (kWh).
        depreciation_threshold: Price ceiling below which grid charging is
            considered economically justified (local currency / kWh).
            Typically the depreciation + conversion-loss threshold from
            :func:`~custom_components.hsem.utils.misc.calculate_recommended_threshold`.
        cycle_cost_per_kwh: Additional per-kWh wear cost *subtracted* from the
            depreciation threshold.  Only slots with import price below
            ``max(depreciation_threshold - cycle_cost_per_kwh, 0)`` are eligible.
            Defaults to 0.0 (backwards compatible).
    """
    log_planner(
        "debug",
        "[chg] apply_opportunistic_charge  current=%.3f  usable=%.3f  "
        "depr_threshold=%.4f  cycle_cost=%.4f",
        current_capacity,
        usable_capacity,
        depreciation_threshold,
        cycle_cost_per_kwh,
    )
    if max_charge_per_interval <= 0:
        return

    already_planned = _already_planned_charge_kwh(slots)
    remaining_capacity = max(usable_capacity - current_capacity - already_planned, 0.0)
    if remaining_capacity <= 0:
        log_planner(
            "debug",
            "[chg] apply_opportunistic_charge  skipped — no remaining capacity "
            "(already_planned=%.3f)",
            already_planned,
        )
        return

    charged = 0.0

    # Priority 1: negative import price — charge as much as possible
    for s in sorted(
        (
            slot
            for slot in slots
            if as_tz(slot.end, now.tzinfo) > now
            and slot.recommendation is None
            and slot.price.import_price < 0
        ),
        key=lambda x: (x.price.import_price, x.start),
    ):
        if charged >= remaining_capacity:
            break
        energy = min(max_charge_per_interval, remaining_capacity - charged)
        if energy > 0:
            s.recommendation = Recommendations.BatteriesChargeGrid.value
            s.batteries_charged_kwh = round(energy, 3)
            charged += energy

    # Priority 2: below-(depreciation − cycle cost) price
    # Cycle wear cost is subtracted from the depreciation threshold to make
    # the planner more conservative: the effective ceiling is reduced so that
    # only prices that are cheap enough to justify *both* the depreciation and
    # the wear cost qualify for opportunistic charging.
    effective_threshold = max(depreciation_threshold - cycle_cost_per_kwh, 0.0)
    if abs(effective_threshold) > 1e-9:
        for s in sorted(
            (
                slot
                for slot in slots
                if as_tz(slot.end, now.tzinfo) > now
                and slot.recommendation is None
                and 0 <= slot.price.import_price < effective_threshold
            ),
            key=lambda x: (x.price.import_price, x.start),
        ):
            if charged >= remaining_capacity:
                break
            energy = min(max_charge_per_interval, remaining_capacity - charged)
            if energy > 0:
                s.recommendation = Recommendations.BatteriesChargeGrid.value
                s.batteries_charged_kwh = round(energy, 3)
                charged += energy


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
        _LOGGER.debug("arbitrage: max_charge_per_interval <= 0, skipping")
        return

    enabled = [s for s in battery_schedules if s.enabled]
    if not enabled:
        _LOGGER.debug("arbitrage: no enabled battery schedule — grid charge disabled")
        return

    remaining_capacity = max(usable_capacity - current_capacity, 0.0)
    already_planned = _already_planned_charge_kwh(slots)
    remaining_capacity = max(remaining_capacity - already_planned, 0.0)
    if remaining_capacity <= 1e-9:
        _LOGGER.debug(
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
        _LOGGER.debug(
            "arbitrage: no future positive-net-consumption slots — nothing to offset"
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
        _LOGGER.debug("arbitrage: no unassigned future slots")
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
            _LOGGER.debug(
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
            _LOGGER.debug(
                "arbitrage: no profitable future slot for candidate at %s "
                "(price=%.4f, required spread=%.4f)",
                cand.start.isoformat(),
                cand_price,
                min_required_spread,
            )

    if chosen_any:
        _LOGGER.debug(
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
        _LOGGER.debug(
            "arbitrage: no slot scheduled — price spread did not cover "
            "min_price_difference(%.4f) + cycle_cost(%.4f) + conversion loss",
            sched_min_diff,
            cycle_cost_per_kwh,
        )


# ---------------------------------------------------------------------------
# Window-level hysteresis — prevent rapid charge↔discharge toggles
# ---------------------------------------------------------------------------


def apply_window_hysteresis(
    slots: list[PlannedSlot],
    now: datetime,
    *,
    window_hysteresis_minutes: int,
    previous_current_recommendation: str | None,
    previous_current_slot_start: datetime | None,
) -> tuple[str | None, datetime | None]:
    """Apply minimum hold time before allowing charge↔discharge transitions.

    Prevents rapid toggling between charge and discharge behaviour near the
    boundary of schedule windows.  If the current slot's recommendation
    would flip between charge-type and discharge-type, and the new regime
    has been in place for less than ``window_hysteresis_minutes``, the
    previous recommendation is kept.

    The function considers two broad categories:
    - **Charge-type**: ``batteries_charge_grid``, ``batteries_charge_solar``,
      ``ev_smart_charging``
    - **Discharge-type**: ``batteries_discharge_mode``,
      ``force_batteries_discharge``, ``force_export``
    - **Neutral** (no change needed): ``batteries_wait_mode``,
      ``time_passed``, ``missing_input_entities``, ``None``

    Transitioning within the same category (e.g. grid-charge → solar-charge)
    is always allowed — only cross-category flips are held.

    Args:
        slots:
            Ordered list of planned slots (mutated in place).
        now:
            Timezone-aware current datetime.
        window_hysteresis_minutes:
            Minimum hold time in minutes.  0 disables the feature entirely.
        previous_current_recommendation:
            Recommendation that was active on the current slot during the
            previous planner run.  ``None`` on first run.
        previous_current_slot_start:
            Start time of the slot that carried
            ``previous_current_recommendation``.  ``None`` on first run.

    Returns:
        A ``(updated_recommendation, current_slot_start)`` tuple.
        ``updated_recommendation`` is the (possibly held) recommendation
        for the current slot, and ``current_slot_start`` is the start time
        of the current slot (for persisting across cycles).
    """
    if window_hysteresis_minutes <= 0:
        # Feature disabled — find and return current recommendation unchanged
        for s in slots:
            if as_tz(s.start, now.tzinfo) <= now < as_tz(s.end, now.tzinfo):
                return s.recommendation, s.start
        return None, None

    # Find the current slot
    current_slot: PlannedSlot | None = None
    for s in slots:
        if as_tz(s.start, now.tzinfo) <= now < as_tz(s.end, now.tzinfo):
            current_slot = s
            break

    if current_slot is None:
        return None, None

    new_rec = current_slot.recommendation
    new_start = current_slot.start

    # No previous state — first run, no hysteresis to apply
    if previous_current_recommendation is None or previous_current_slot_start is None:
        return new_rec, new_start

    new_category = _rec_category(new_rec)
    prev_category = _rec_category(previous_current_recommendation)

    # If the recommendation hasn't changed category, no hold needed
    if (
        new_category == prev_category
        or new_category == "neutral"
        or prev_category == "neutral"
    ):
        return new_rec, new_start

    # Category changed — check hold time
    elapsed_minutes = (now - previous_current_slot_start).total_seconds() / 60.0
    if elapsed_minutes < window_hysteresis_minutes:
        # Hold the previous recommendation
        log_planner(
            "debug",
            "[window_hysteresis] Holding previous recommendation '%s' on current "
            "slot (elapsed=%.1f min < hold=%d min). New '%s' suppressed.",
            previous_current_recommendation,
            elapsed_minutes,
            window_hysteresis_minutes,
            new_rec,
        )
        current_slot.recommendation = previous_current_recommendation
        return previous_current_recommendation, previous_current_slot_start

    # Enough time has passed — allow the switch
    log_planner(
        "debug",
        "[window_hysteresis] Allowing transition '%s' → '%s' on current slot "
        "(elapsed=%.1f min >= hold=%d min).",
        previous_current_recommendation,
        new_rec,
        elapsed_minutes,
        window_hysteresis_minutes,
    )
    return new_rec, new_start


def _rec_category(rec: str | None) -> str:
    """Classify a recommendation into a category.

    Returns ``"charge"``, ``"discharge"``, or ``"neutral"``.
    """
    if rec in _CHARGE_RECS:
        return "charge"
    if rec in _DISCHARGE_RECS:
        return "discharge"
    return "neutral"

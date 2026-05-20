"""Battery charge and discharge scheduling for the HSEM planner.

Single responsibility: decide *when* to charge and discharge the battery
based on discharge-window schedules, price signals, and seasonal strategy.

All functions are pure — no I/O, no Home Assistant imports.  They mutate the
:class:`PlannedSlot` list passed in and return nothing (or a scalar result).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from custom_components.hsem.const import (
    NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH,
    SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH,
)
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
# Discharge schedule detection
# ---------------------------------------------------------------------------


def apply_discharge_schedules(
    slots: list[PlannedSlot],
    battery_schedules: list[BatteryScheduleInput],
    now: datetime,
) -> None:
    """Mark slots inside each enabled discharge window as ``BatteriesDischargeMode``.

    Also populates ``_needed_capacity`` and ``_avg_import_price`` as dynamic
    attributes on each :class:`BatteryScheduleInput` so the charge planner can
    read them without an extra pass.

    Args:
        slots: Mutable list of planned slots.
        battery_schedules: Schedule configurations to evaluate.
        now: Timezone-aware current datetime.
    """
    for sched in battery_schedules:
        if not sched.enabled:
            continue

        # Determine the last slot end in the planning horizon so we know how
        # many days to cover.  We apply the discharge window once per calendar
        # day that falls within [now, horizon_end].
        future_slots = [s for s in slots if as_tz(s.end, now.tzinfo) > now]
        if not future_slots:
            continue
        horizon_end = as_tz(future_slots[-1].end, now.tzinfo)

        # Collect all occurrences of this schedule window within the horizon.
        # Start from the first upcoming occurrence and advance one day at a time.
        # Each occurrence is stored so apply_charge_schedules can schedule
        # pre-charge independently per window occurrence.
        window_start_abs = next_window_start_dt(now, sched.start)
        occurrences: list[tuple[datetime, datetime, float, float]] = []
        sched_total_net = 0.0

        while window_start_abs < horizon_end:
            if sched.end > sched.start:
                window_end_abs = datetime.combine(
                    window_start_abs.date(), sched.end
                ).replace(tzinfo=now.tzinfo)
            else:
                # Cross-midnight discharge window
                window_end_abs = datetime.combine(
                    (window_start_abs + timedelta(days=1)).date(), sched.end
                ).replace(tzinfo=now.tzinfo)

            for slot in slots:
                slot_start = as_tz(slot.start, now.tzinfo)
                slot_end = as_tz(slot.end, now.tzinfo)
                if slot_end <= now:
                    continue
                if slot_start >= window_start_abs and slot_end <= window_end_abs:
                    slot.recommendation = Recommendations.BatteriesDischargeMode.value

            # Capture per-occurrence capacity and avg price.
            #
            # Battery-relevant net consumption excludes EV planned load:
            #   battery_net = avg_house_consumption - pv
            #
            # When base_load_includes_ev=False, estimated_net_consumption includes
            # ev_planned_load_kwh.  The EV draws directly from grid/PV, not from
            # the home battery, so including it in occ_needed would over-inflate
            # the pre-charge target and cause the price-spread guard in
            # _apply_grid_charge to reject otherwise profitable charge slots.
            #
            # ev_accounted_load_kwh is already captured in avg_house_consumption
            # (base_load_includes_ev=True), so no correction is needed for that
            # case — the battery must cover it.
            occ_net = 0.0
            occ_prices: list[float] = []
            for s in slots:
                s_start = as_tz(s.start, now.tzinfo)
                s_end = as_tz(s.end, now.tzinfo)
                if (
                    s.recommendation == Recommendations.BatteriesDischargeMode.value
                    and s_start >= window_start_abs
                    and s_end <= window_end_abs
                ):
                    # Subtract extra EV load (injected, base_load_includes_ev=False)
                    # so the battery only targets house coverage.
                    battery_net = (
                        s.estimated_net_consumption_kwh - s.ev_planned_load_kwh
                    )
                    occ_net += battery_net
                    occ_prices.append(s.price.import_price)

            occ_needed = max(occ_net, 0.0)
            occ_avg_price = (
                round(sum(occ_prices) / len(occ_prices), 3) if occ_prices else 0.0
            )
            # Store: (window_start, window_end, needed_kwh, avg_discharge_price)
            occurrences.append(
                (window_start_abs, window_end_abs, occ_needed, occ_avg_price)
            )
            sched_total_net += occ_net

            # Advance to the same window start on the following calendar day
            window_start_abs += timedelta(days=1)

        # _occurrences: per-day data consumed by apply_charge_schedules
        sched._occurrences = occurrences  # type: ignore[attr-defined]
        # _needed_capacity: aggregate across all occurrences (used by coordinator)
        sched._needed_capacity = max(sched_total_net, 0.0)  # type: ignore[attr-defined]
        # _avg_import_price: average across all occurrences
        all_occ_prices = [avg for _, _, _, avg in occurrences if avg > 0]
        sched._avg_import_price = (  # type: ignore[attr-defined]
            round(sum(all_occ_prices) / len(all_occ_prices), 3)
            if all_occ_prices
            else 0.0
        )


def _avg_price_in_window(
    slots: list[PlannedSlot],
    window_start: datetime,
    window_end: datetime,
    now: datetime,
) -> float:
    """Return the average import price for slots inside a discharge window."""
    prices = [
        s.price.import_price
        for s in slots
        if as_tz(s.start, now.tzinfo) >= window_start
        and as_tz(s.end, now.tzinfo) <= window_end
        and as_tz(s.end, now.tzinfo) > now
    ]
    return round(sum(prices) / len(prices), 3) if prices else 0.0


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
        return

    # Cross-occurrence capacity cap: only enforce when usable_kwh is
    # provided (> 0).  When both current_kwh and usable_kwh are 0.0
    # (backward-compatible default), no cap is applied.
    remaining_capacity = (
        max(usable_kwh - current_kwh, 0.0) if usable_kwh > 0 else float("inf")
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
                _apply_grid_charge(
                    eligible,
                    sched,
                    occurrence_budget,
                    charged,
                    max_charge_per_interval,
                    avg_discharge_price,
                    cycle_cost_per_kwh=cycle_cost_per_kwh,
                    recommended_threshold=recommended_threshold,
                )

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
) -> None:
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
        return  # Price spread does not cover loss + cycle wear cost

    # Second pass: actually assign recommendations
    charged = charged_so_far
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


# ---------------------------------------------------------------------------
# Excess export
# ---------------------------------------------------------------------------


def calculate_required_battery_until_solar(
    slots: list[PlannedSlot],
    now: datetime,
    usable_capacity: float,
    discharge_buffer_pct: float,
) -> float:
    """Estimate battery capacity needed until the first solar surplus slot.

    Scans forward from *now* and accumulates positive net-consumption until
    a slot with negative net-consumption (solar surplus) is found.

    Args:
        slots: List of planned slots.
        now: Timezone-aware current datetime.
        usable_capacity: Maximum usable battery energy in kWh.
        discharge_buffer_pct: Safety buffer as a percentage of usable capacity.

    Returns:
        Required battery capacity in kWh (including safety buffer).
    """
    required = 0.0
    for slot in slots:
        if as_tz(slot.start, now.tzinfo) < now:
            continue
        if slot.estimated_net_consumption_kwh < 0:
            break
        if slot.estimated_net_consumption_kwh > 0:
            required += slot.estimated_net_consumption_kwh

    buffer_kwh = usable_capacity * (discharge_buffer_pct / 100)
    return round(required + buffer_kwh, 3)


def apply_excess_export(
    slots: list[PlannedSlot],
    now: datetime,
    current_capacity: float,
    required_capacity: float,
    export_price_threshold: float,
    warnings: list[str],
) -> None:
    """Mark high-export-price future slots for forced battery discharge.

    Only triggered when the battery holds more energy than needed until
    the next solar surplus.  Grid-charged batteries require a minimum price
    difference; solar-charged batteries export opportunistically.

    Args:
        slots: Mutable list of planned slots.
        now: Timezone-aware current datetime.
        current_capacity: Current available battery energy in kWh.
        required_capacity: Energy needed until next solar surplus (kWh).
        export_price_threshold: Minimum export-minus-import price delta for
            grid-charged batteries.
        warnings: Mutable list to append diagnostic messages to.
    """
    # battery_discharge_budget_kwh is the kWh the battery can export beyond what is
    # already needed to cover future house load.  Solar surplus in a slot does NOT
    # add to this budget: solar is a separate energy flow and is already accounted for
    # in estimated_net_consumption.  Only positive net consumption (house load > solar)
    # draws down the battery, so we drain the budget by max(net, 0) per slot.
    battery_discharge_budget_kwh = current_capacity - required_capacity
    if battery_discharge_budget_kwh <= 0:
        return

    # D16 fix: track actual solar vs grid fractions rather than a coarse flag.
    # We compute the total kWh scheduled to be charged from solar vs from the
    # grid within this planning run.  The solar fraction determines how much of
    # the battery is considered "free" (solar-charged) vs paid-for (grid-charged).
    # When the solar fraction exceeds 50 % of total planned charging we treat
    # the battery as predominantly solar-charged and bypass the price threshold;
    # otherwise the full price-difference guard applies.
    solar_charged_kwh = sum(
        s.batteries_charged_kwh
        for s in slots
        if s.recommendation == Recommendations.BatteriesChargeSolar.value
    )
    grid_charged_kwh = sum(
        s.batteries_charged_kwh
        for s in slots
        if s.recommendation == Recommendations.BatteriesChargeGrid.value
    )
    total_planned_charge_kwh = solar_charged_kwh + grid_charged_kwh
    # battery_is_solar_charged is True only when solar charging is the
    # dominant (> 50 %) planned source, or when no grid charging is planned.
    battery_is_solar_charged = (
        total_planned_charge_kwh < 1e-9
        or solar_charged_kwh / total_planned_charge_kwh > 0.5
    )

    candidates = sorted(
        (
            s
            for s in slots
            if as_tz(s.start, now.tzinfo) >= now
            and s.recommendation is None
            and (
                battery_is_solar_charged
                or (
                    s.price.export_price - s.price.import_price
                    >= export_price_threshold
                )
            )
            and s.price.export_price > 0
        ),
        key=lambda x: x.price.export_price,
        reverse=True,
    )

    for s in candidates:
        if battery_discharge_budget_kwh <= 0:
            break
        s.recommendation = Recommendations.ForceBatteriesDischarge.value
        warnings.append(
            f"ForceBatteriesDischarge at {s.start.isoformat()}: export={s.price.export_price}"
        )
        # Only positive net consumption (house load > solar) draws from the battery
        # discharge budget.  Solar-surplus slots (net < 0) contribute 0 drain because
        # the surplus is handled by solar, not by the battery.
        battery_discharge_budget_kwh -= max(s.estimated_net_consumption_kwh, 0.0)


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
    if max_charge_per_interval <= 0:
        return

    remaining_capacity = max(usable_capacity - current_capacity, 0.0)
    if remaining_capacity <= 0:
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
    if max_charge_per_interval <= 0:
        _LOGGER.debug("arbitrage: max_charge_per_interval <= 0, skipping")
        return

    enabled = [s for s in battery_schedules if s.enabled]
    if not enabled:
        _LOGGER.debug("arbitrage: no enabled battery schedule — grid charge disabled")
        return

    remaining_capacity = max(usable_capacity - current_capacity, 0.0)
    if remaining_capacity <= 1e-9:
        _LOGGER.debug(
            "arbitrage: battery effectively full (remaining=%.3f kWh)",
            remaining_capacity,
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
# Discharge concentration — avoid wasting battery on cheap slots
# ---------------------------------------------------------------------------


def concentrate_discharge_on_expensive_slots(
    slots: list[PlannedSlot],
    now: datetime,
    current_kwh: float,
    usable_kwh: float,
    max_discharge_per_slot: float | None,
    discharge_efficiency_pct: float = 100.0,
) -> None:
    """Clear cheap discharge slots the battery cannot fully serve.

    ``apply_discharge_schedules`` and ``apply_optimization_strategy`` mark
    *every* slot in a discharge window as ``BatteriesDischargeMode``, but
    the battery can only cover a fraction of them.  Without concentration
    the SoC simulation greedily discharges in the *first* (cheapest) slots
    and runs out before the most expensive ones.

    This function ranks all ``BatteriesDischargeMode`` slots by import price
    (descending) and clears the recommendation on the cheapest slots that
    exceed the battery's discharge capacity, turning them into grid-import
    slots (marked ``BatteriesWaitMode``).  The most expensive slots keep
    their discharge recommendation.

    The estimate is conservative: it assumes the battery starts at full
    and there is no incoming charge between discharge slots.

    Args:
        slots: Mutable list of planned slots.
        now: Timezone-aware current datetime.
        current_kwh: Energy currently stored above the discharge floor (kWh).
        usable_kwh: Maximum usable energy above the discharge floor (kWh).
        max_discharge_per_slot: Maximum energy dischargeable per slot (kWh).
            ``None`` means unlimited (inverter default).
        discharge_efficiency_pct: Discharge-side efficiency (0-100 %).
    """
    discharge_eff = max(min(discharge_efficiency_pct, 100.0), 1.0) / 100.0

    # Collect all future discharge slots (both BatteriesDischargeMode and
    # ForceBatteriesDischarge — issue #425 Bug I fix).
    discharge_slots = [
        s
        for s in slots
        if s.recommendation in _DISCHARGE_RECS and as_tz(s.end, now.tzinfo) > now
    ]
    if not discharge_slots:
        return

    # Sort by import price descending — most expensive first
    discharge_slots.sort(key=lambda s: s.price.import_price, reverse=True)

    # Calculate cumulative battery energy needed for each slot (most expensive first)
    # and keep only as many as the battery can serve.
    # Use usable_kwh because the battery will be charged by the scheduler
    # before the discharge window starts — current_kwh is only the starting
    # state and does not reflect the available capacity at discharge time.
    total_battery_kwh = usable_kwh
    keep_set: set[int] = set()
    for s in discharge_slots:
        # Energy the battery must release to cover net_demand
        slot_demand = max(s.estimated_net_consumption_kwh, 0.0)
        battery_needed = slot_demand / discharge_eff if discharge_eff > 1e-9 else 0.0
        # Respect the inverter's per-slot discharge power limit — the SoC
        # simulation caps at max_discharge_per_slot, so the concentration
        # estimate must match to avoid over-counting how many slots fit.
        if max_discharge_per_slot is not None:
            battery_needed = min(battery_needed, max_discharge_per_slot)

        if battery_needed <= total_battery_kwh:
            total_battery_kwh -= battery_needed
            keep_set.add(id(s))
        else:
            # Not enough battery — this and all cheaper slots get cleared
            break

    for s in discharge_slots:
        if id(s) not in keep_set:
            _LOGGER.debug(
                "concentrate: clearing discharge at %s→%s  price=%.4f  "
                "(battery can only cover %d of %d slots)",
                s.start.strftime("%d %H:%M"),
                s.end.strftime("%H:%M"),
                s.price.import_price,
                len(keep_set),
                len(discharge_slots),
            )
            # Use BatteriesWaitMode so the fill pass in engine.py does NOT
            # re-mark this as BatteriesDischargeMode.
            s.recommendation = Recommendations.BatteriesWaitMode.value
            s.batteries_charged_kwh = 0.0


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


# ---------------------------------------------------------------------------
# Seasonal optimization
# ---------------------------------------------------------------------------


def apply_optimization_strategy(
    slots: list[PlannedSlot],
    now: datetime,
    current_capacity: float,
    usable_capacity: float,
    required_capacity: float,
    months_winter: list[int],
    warnings: list[str],
    export_min_price: float = 0.0,
) -> None:
    """Apply seasonal optimization logic to remaining unassigned slots.

    Decision priority per unassigned slot:

    1. Export price > import price **and** export price ≥ ``export_min_price``
       → ``ForceExport``
    2. Solar surplus → ``BatteriesChargeSolar`` (until battery full)
    3. Future forced export pending and battery above required → ``BatteriesWaitMode``
    4. Winter month → ``BatteriesWaitMode``
    5. Summer month with solar → ``BatteriesChargeSolar``; else ``BatteriesDischargeMode``

    Args:
        slots: Mutable list of planned slots.
        now: Timezone-aware current datetime.
        current_capacity: Current available battery energy in kWh.
        usable_capacity: Maximum usable battery energy in kWh.
        required_capacity: Energy required until next solar surplus (kWh).
        months_winter: List of month integers (1-12) treated as winter.
        warnings: Mutable list for diagnostic messages (currently unused here).
        export_min_price: Minimum export price required to trigger
            ``ForceExport``.  Slots where export price is below this
            threshold are not marked for export even if export > import.
            Defaults to ``0.0`` (any positive export price qualifies).
    """
    current_month = now.month
    months_summer = [m for m in range(1, 13) if m not in months_winter]

    # ForceExport when export > import AND export >= export_min_price (A3 fix)
    for rec in slots:
        if (
            rec.price.export_price > rec.price.import_price
            and rec.price.export_price >= export_min_price
            and rec.recommendation is None
        ):
            rec.recommendation = Recommendations.ForceExport.value

    # Solar charging until battery full — across the full planning horizon
    # (not limited to today; a 48-hour window should charge from solar on
    # both day 1 and day 2).
    batteries_needed_charge = max(usable_capacity - current_capacity, 0.0)
    charged = 0.0

    for rec in sorted(
        (
            s
            for s in slots
            if s.recommendation is None and as_tz(s.start, now.tzinfo) >= now
        ),
        key=lambda x: x.price.export_price,
    ):
        if charged >= batteries_needed_charge:
            break
        # v5.1.0 threshold: <= NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH
        # (charge even near-zero-consumption slots)
        if rec.estimated_net_consumption_kwh <= NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH:
            # Per-slot energy: how much this individual slot contributes, capped at
            # what is still needed.  Store the per-slot value so that summing across
            # slots in engine.py / total_charged_energy_kwh() is not double-counted.
            slot_solar = abs(rec.estimated_net_consumption_kwh)
            slot_energy = min(slot_solar, batteries_needed_charge - charged)
            charged += slot_energy
            rec.recommendation = Recommendations.BatteriesChargeSolar.value
            rec.batteries_charged_kwh = round(slot_energy, 3)

    # Seasonal fill for remaining unassigned slots
    for rec in slots:
        if rec.recommendation is not None:
            continue

        has_future_forced_export = any(
            r.recommendation == Recommendations.ForceBatteriesDischarge.value
            and r.start > rec.start
            for r in slots
        )
        if has_future_forced_export and current_capacity > required_capacity:
            rec.recommendation = Recommendations.BatteriesWaitMode.value
            continue

        if current_month in months_winter:
            rec.recommendation = Recommendations.BatteriesWaitMode.value
        elif current_month in months_summer:
            if rec.estimated_net_consumption_kwh <= NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH:
                rec.recommendation = Recommendations.BatteriesChargeSolar.value
            else:
                rec.recommendation = Recommendations.BatteriesDischargeMode.value

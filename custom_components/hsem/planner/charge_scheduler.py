"""Battery charge and discharge scheduling for the HSEM planner.

Single responsibility: decide *when* to charge and discharge the battery
based on discharge-window schedules, price signals, and seasonal strategy.

All functions are pure — no I/O, no Home Assistant imports.  They mutate the
:class:`PlannedSlot` list passed in and return nothing (or a scalar result).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.hsem.const import (
    NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH,
    SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH,
)
from custom_components.hsem.datetime_utils import as_tz
from custom_components.hsem.models.planner_inputs import BatteryScheduleInput
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.utils.misc import next_window_start_dt
from custom_components.hsem.utils.recommendations import Recommendations

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
                    battery_net = s.estimated_net_consumption - s.ev_planned_load_kwh
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
    cycle_cost_per_kwh: float = 0.0,
) -> None:
    """Assign charge recommendations to slots before each discharge window.

    Three-priority ordering (mirrors ``_async_find_best_time_to_charge_battery_schedule``):

    1. Negative import price (free/paid-to-charge)
    2. Solar surplus (``estimated_net_consumption < SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH``)
    3. Cheapest remaining grid hours (guarded by min price difference + cycle cost)

    Args:
        slots: Mutable list of planned slots.
        battery_schedules: Schedule configurations (must have ``_needed_capacity``
            and ``_avg_import_price`` set by :func:`apply_discharge_schedules`).
        now: Timezone-aware current datetime.
        max_charge_per_interval: Maximum energy (kWh) chargeable per slot.
        cycle_cost_per_kwh: Additional per-kWh cycle wear cost added to the
            price-difference guard.  When > 0 the planner only charges from
            the grid when ``discharge_price - charge_price ≥
            min_price_difference + cycle_cost_per_kwh``.  Defaults to 0.0
            (no extra guard beyond the schedule's min_price_difference).
    """
    if max_charge_per_interval <= 0:
        return

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
                if charged >= needed:
                    break
                energy = min(max_charge_per_interval, needed - charged)
                if energy > 0:
                    s.recommendation = Recommendations.BatteriesChargeGrid.value
                    s.batteries_charged = round(energy, 3)
                    charged += energy

            # Priority 2: solar surplus
            if charged < needed:
                for s in sorted(
                    (
                        e
                        for e in eligible
                        if e.estimated_net_consumption
                        < SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH
                        and e.recommendation is None
                    ),
                    # NOTE: SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH is negative, so this
                    # selects slots where net consumption is sufficiently negative
                    # (i.e., there is a meaningful solar surplus to charge from).
                    key=lambda x: (x.estimated_net_consumption, x.start),
                ):
                    if charged >= needed:
                        break
                    available_solar = abs(s.estimated_net_consumption)
                    energy = min(
                        max_charge_per_interval, needed - charged, available_solar
                    )
                    if energy > 0:
                        s.recommendation = Recommendations.BatteriesChargeSolar.value
                        s.batteries_charged = round(energy, 3)
                        charged += energy

            # Priority 3: cheapest grid hours (min price difference + cycle cost guard)
            if charged < needed:
                _apply_grid_charge(
                    eligible,
                    sched,
                    needed,
                    charged,
                    max_charge_per_interval,
                    avg_discharge_price,
                    cycle_cost_per_kwh=cycle_cost_per_kwh,
                )


def _apply_grid_charge(
    eligible: list[PlannedSlot],
    sched: BatteryScheduleInput,
    needed: float,
    charged_so_far: float,
    max_charge_per_interval: float,
    avg_discharge_price: float,
    cycle_cost_per_kwh: float = 0.0,
) -> None:
    """Apply cheapest-grid-hour charging with min-price-difference + cycle-cost guard.

    The combined profitability condition is:

        avg_discharge_price − avg_charge_price ≥ min_price_difference + cycle_cost_per_kwh

    Both ``min_price_difference`` (from the battery schedule) and
    ``cycle_cost_per_kwh`` (from the planner input) must be covered by the
    price spread before grid charging is approved.

    Args:
        eligible: Pre-filtered candidate slots.
        sched: The battery schedule being filled.
        needed: Total energy to charge in kWh.
        charged_so_far: Energy already charged by higher-priority sources.
        max_charge_per_interval: Maximum energy per slot in kWh.
        avg_discharge_price: Average import price during the discharge window.
        cycle_cost_per_kwh: Per-kWh battery wear cost added to the guard.
            Defaults to 0.0 (backwards compatible).
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
            abs(s.estimated_net_consumption) if s.estimated_net_consumption < 0 else 0
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
    # Combined threshold: user-configured schedule minimum + per-kWh wear cost.
    # Both must be covered by the price spread for grid charging to be profitable.
    min_diff = sched.min_price_difference + cycle_cost_per_kwh

    if abs(min_diff) > 1e-9 and price_diff < min_diff:
        return  # Price spread does not cover loss + cycle wear cost

    # Second pass: actually assign recommendations
    charged = charged_so_far
    for s in grid_candidates:
        if charged >= needed:
            break
        available_solar = (
            abs(s.estimated_net_consumption) if s.estimated_net_consumption < 0 else 0
        )
        grid_needed = min(
            max_charge_per_interval - available_solar,
            needed - charged - available_solar,
        )
        energy = available_solar + grid_needed
        if energy > 0:
            s.recommendation = Recommendations.BatteriesChargeGrid.value
            s.batteries_charged = round(energy, 3)
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
        if slot.estimated_net_consumption < 0:
            break
        if slot.estimated_net_consumption > 0:
            required += slot.estimated_net_consumption

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
        s.batteries_charged
        for s in slots
        if s.recommendation == Recommendations.BatteriesChargeSolar.value
    )
    grid_charged_kwh = sum(
        s.batteries_charged
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
        battery_discharge_budget_kwh -= max(s.estimated_net_consumption, 0.0)


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
            s.batteries_charged = round(energy, 3)
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
                s.batteries_charged = round(energy, 3)
                charged += energy


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
        if rec.estimated_net_consumption <= NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH:
            # Per-slot energy: how much this individual slot contributes, capped at
            # what is still needed.  Store the per-slot value so that summing across
            # slots in engine.py / total_charged_energy_kwh() is not double-counted.
            slot_solar = abs(rec.estimated_net_consumption)
            slot_energy = min(slot_solar, batteries_needed_charge - charged)
            charged += slot_energy
            rec.recommendation = Recommendations.BatteriesChargeSolar.value
            rec.batteries_charged = round(slot_energy, 3)

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
            if rec.estimated_net_consumption <= NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH:
                rec.recommendation = Recommendations.BatteriesChargeSolar.value
            else:
                rec.recommendation = Recommendations.BatteriesDischargeMode.value
